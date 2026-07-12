"""Firestoreでタスク言及履歴を記憶し、過去言及との突合で優先度を昇格する。"""
import os
from datetime import datetime, timezone

from google.cloud import firestore

import obs
from clients import firestore_client
from embeddings import cosine_similarity, embed_text, is_semantic_match_enabled
from priority import promote

COLLECTION = "task_mentions"
PROMOTION_STEP = 1

_client = firestore_client


def _semantic_threshold() -> float:
    """Read the semantic-match threshold, falling back safely for invalid values."""
    try:
        return float(os.environ.get("YUI_SEMANTIC_THRESHOLD", "0.80"))
    except ValueError:
        return 0.80


def _resolve_remention(
    data: dict, reference, title: str, priority: int, reason: str, now: datetime
) -> dict:
    """Apply the established re-mention update and return its public result."""
    mention_count = data.get("mention_count", 1) + 1
    previous_priority = data.get("priority", priority)
    new_priority = max(promote(previous_priority, PROMOTION_STEP), priority)
    was_promoted = new_priority > priority

    reference.update(
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


def find_pending_questions(limit: int = 5) -> list[dict]:
    """Return the most recent task questions that need a user response."""
    docs = (
        _client()
        .collection(COLLECTION)
        .where("status", "==", "needs_input")
        .limit(limit)
        .get()
    )
    return [
        {
            "id": doc.id,
            "title": data.get("title", ""),
            "pending_question": data.get("pending_question", ""),
        }
        for doc in docs
        for data in [doc.to_dict()]
    ]


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


def record_and_resolve(
    title: str,
    priority: int,
    reason: str,
    open_tasks: list[dict] | None = None,
) -> dict:
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
        return _resolve_remention(data, doc.reference, title, priority, reason, now)

    embedding = None
    if is_semantic_match_enabled():
        try:
            embedding = embed_text(title)
        except Exception as exc:
            obs.warning(
                "semantic matching failed",
                api="embeddings",
                detail=str(exc),
                exc_type=type(exc).__name__,
            )
        else:
            # Callers that already loaded open tasks can share that snapshot and
            # avoid a Firestore read for every extracted task.
            candidates = find_open_tasks() if open_tasks is None else open_tasks
            for task in candidates:
                task_embedding = task.get("embedding")
                if not task_embedding:
                    continue

                similarity = cosine_similarity(embedding, task_embedding)
                if similarity >= _semantic_threshold():
                    matched_title = task.get("title", "")
                    obs.info(
                        "semantic re-mention detected",
                        similarity=similarity,
                        matched_title=matched_title,
                    )
                    # タイトルは既存の表記を維持する。新しい言い回しのまま返すと
                    # Google Tasks の upsert が titles_match で既存エントリに一致せず
                    # 重複タスクを作ってしまう。
                    return _resolve_remention(
                        task,
                        tasks_ref.document(task["id"]),
                        matched_title or title,
                        priority,
                        reason,
                        now,
                    )

    task = {
        "title": title,
        "priority": priority,
        "reason": reason,
        "mention_count": 1,
        "first_mentioned_at": now,
        "last_mentioned_at": now,
        "status": "open",
    }
    if embedding is not None:
        task["embedding"] = embedding
    tasks_ref.add(task)

    return {
        "title": title,
        "priority": priority,
        "reason": reason,
        "mention_count": 1,
        "promoted": False,
        "previous_priority": priority,
    }
