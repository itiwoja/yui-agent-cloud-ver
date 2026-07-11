"""ユーザーの指示なしに放置タスクの優先度を見直し、高優先度タスクには裏どり調査を添付する自律バックグラウンドジョブ。

Cloud Scheduler から定期的に叩かれることを想定。
"""
import os
from datetime import datetime, timedelta, timezone


import obs
from clients import firestore_client, gemini_client
from priority import MAX_PRIORITY, promote
from research import research as _research
from tasks_client import upsert_task

COLLECTION = "task_mentions"
STALENESS_HOURS = float(os.environ.get("STALENESS_HOURS", "6"))
# システムが自律的に上げられる上限。既定は MAX（従来挙動）。下げれば🔴を人間の緊急に残せる。
SYSTEM_ESCALATION_CEILING = int(os.environ.get("SYSTEM_ESCALATION_CEILING", str(MAX_PRIORITY)))
RESEARCH_PRIORITY_THRESHOLD = 4
REVIEW_LIMIT = int(os.environ.get("YUI_REVIEW_LIMIT", "500"))


_client = gemini_client
_db = firestore_client


def run_autonomous_review() -> dict:
    db = _db()
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(hours=STALENESS_HOURS)

    # status の追加絞り込みは複合インデックスを要求し、本番を壊すおそれがあるため避ける。
    # 現行のインデックス不要なクエリのまま、無制限スキャンだけを防ぐ。
    docs = db.collection(COLLECTION).where("priority", "<", MAX_PRIORITY).limit(
        REVIEW_LIMIT
    ).get()

    escalated = []
    researched = []

    for doc in docs:
        try:
            data = doc.to_dict()
            last_mentioned = data.get("last_mentioned_at")
            if last_mentioned is None or last_mentioned > stale_cutoff:
                continue

            # 滞留期間ごとに最大1回だけ昇格する。last_reviewed_at で直近の昇格を
            # ガードしないと、30分毎のスケジューラ実行で毎回+1され、数時間で全部が
            # 最上位(🔴)に張り付いてしまう（優先度が無意味化する飽和バグ）。
            last_reviewed = data.get("last_reviewed_at")
            if last_reviewed is not None and last_reviewed > stale_cutoff:
                continue

            old_priority = data.get("priority", 1)
            new_priority = promote(old_priority, ceiling=SYSTEM_ESCALATION_CEILING)
            if new_priority == old_priority:
                continue
            reason = data.get("reason", "")
            update = {
                "priority": new_priority,
                "escalated_by_system": True,
                "last_reviewed_at": now,
            }

            if new_priority >= RESEARCH_PRIORITY_THRESHOLD:
                note = _research(data["title"], reason)
                update["research_note"] = note
                reason = f"{reason}\n\n[ゆいが自動で裏どり] {note}"
                researched.append({"title": data["title"], "research_note": note})

            doc.reference.update(update)
            upsert_task(data["title"], new_priority, reason)
            escalated.append(
                {
                    "title": data["title"],
                    "old_priority": old_priority,
                    "new_priority": new_priority,
                }
            )
        except Exception as exc:
            obs.error(
                "autonomous review item failed",
                api="autonomous_review",
                doc_id=doc.id,
                detail=str(exc),
                exc_type=type(exc).__name__,
            )
            continue

    return {"escalated": escalated, "researched": researched, "reviewed_at": now.isoformat()}
