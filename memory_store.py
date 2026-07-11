"""Firestoreでタスク言及履歴を記憶し、過去言及との突合で優先度を昇格する。"""
from datetime import datetime, timezone

from google.cloud import firestore

from clients import firestore_client
from priority import promote

COLLECTION = "task_mentions"
PROMOTION_STEP = 1

_client = firestore_client


def get_recent_titles(limit: int = 30) -> list[str]:
    """直近に記録されたタスクタイトルを返す。抽出時にGeminiへ渡し、同一タスクの表記揺れを吸収させる。"""
    db = _client()
    docs = (
        db.collection(COLLECTION)
        .order_by("last_mentioned_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .get()
    )
    seen = []
    for doc in docs:
        title = doc.to_dict().get("title")
        if title and title not in seen:
            seen.append(title)
    return seen


def find_open_tasks(limit: int = 100) -> list[dict]:
    """会話から完了対象を照合するため、done以外のタスクを返す。"""
    docs = (
        _client()
        .collection(COLLECTION)
        .order_by("last_mentioned_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .get()
    )
    tasks = []
    for doc in docs:
        data = doc.to_dict()
        if data.get("status", "open") == "done":
            continue
        tasks.append({"id": doc.id, **data})
    return tasks


def complete_task(doc_id: str) -> dict:
    """Firestore上のタスクを完了にし、更新後の主要フィールドを返す。"""
    ref = _client().collection(COLLECTION).document(doc_id)
    snapshot = ref.get()
    if not snapshot.exists:
        return {"error": "task not found"}

    completed_at = datetime.now(timezone.utc)
    ref.update({"status": "done", "completed_at": completed_at})
    data = snapshot.to_dict()
    return {
        "id": doc_id,
        "title": data.get("title", ""),
        "status": "done",
        "completed_at": completed_at,
    }


def delete_task(doc_id: str) -> dict:
    """Firestore上のタスクを物理削除する（誤って拾われたタスクの取り消し）。"""
    ref = _client().collection(COLLECTION).document(doc_id)
    snapshot = ref.get()
    if not snapshot.exists:
        return {"error": "task not found"}
    title = snapshot.to_dict().get("title", "")
    ref.delete()
    return {"id": doc_id, "title": title, "status": "dismissed"}


def record_and_resolve(title: str, priority: int, reason: str) -> dict:
    """タスク言及を記録し、過去に同じタスクの言及があれば優先度を昇格して返す。"""
    db = _client()
    tasks_ref = db.collection(COLLECTION)

    previous = (
        tasks_ref.where("title", "==", title)
        .order_by("last_mentioned_at", direction=firestore.Query.DESCENDING)
        .limit(1)
        .get()
    )

    now = datetime.now(timezone.utc)

    if previous:
        doc = previous[0]
        data = doc.to_dict()
        mention_count = data.get("mention_count", 1) + 1
        previous_priority = data.get("priority", priority)
        # 再言及はユーザー由来なので上限は MAX（🔴 まで許す）。新規抽出側が
        # より高い優先度を付けていればそれも尊重する。
        new_priority = max(promote(previous_priority, PROMOTION_STEP), priority)
        was_promoted = new_priority > priority

        doc.reference.update(
            {
                "priority": new_priority,
                "reason": reason,
                "mention_count": mention_count,
                "last_mentioned_at": now,
            }
        )

        return {
            "title": title,
            "priority": new_priority,
            "reason": reason,
            "mention_count": mention_count,
            "promoted": was_promoted,
            "previous_priority": previous_priority,
        }

    tasks_ref.add(
        {
            "title": title,
            "priority": priority,
            "reason": reason,
            "mention_count": 1,
            "first_mentioned_at": now,
            "last_mentioned_at": now,
            "status": "open",
        }
    )

    return {
        "title": title,
        "priority": priority,
        "reason": reason,
        "mention_count": 1,
        "promoted": False,
        "previous_priority": priority,
    }
