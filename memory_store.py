"""Firestoreでタスク言及履歴を記憶し、過去言及との突合で優先度を昇格する。"""
import os
from datetime import datetime, timezone

from google.cloud import firestore

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "yui-agent-2026")
COLLECTION = "task_mentions"
PROMOTION_STEP = 1
MAX_PRIORITY = 5


def _client() -> firestore.Client:
    return firestore.Client(project=PROJECT_ID)


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
        promoted_priority = min(MAX_PRIORITY, data.get("priority", priority) + PROMOTION_STEP)
        was_promoted = promoted_priority > priority

        doc.reference.update(
            {
                "priority": max(promoted_priority, priority),
                "reason": reason,
                "mention_count": mention_count,
                "last_mentioned_at": now,
            }
        )

        return {
            "title": title,
            "priority": max(promoted_priority, priority),
            "reason": reason,
            "mention_count": mention_count,
            "promoted": was_promoted,
            "previous_priority": data.get("priority", priority),
        }

    tasks_ref.add(
        {
            "title": title,
            "priority": priority,
            "reason": reason,
            "mention_count": 1,
            "first_mentioned_at": now,
            "last_mentioned_at": now,
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
