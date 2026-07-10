"""ユーザーの指示なしに放置タスクの優先度を見直し、高優先度タスクには裏どり調査を添付する自律バックグラウンドジョブ。

Cloud Scheduler から定期的に叩かれることを想定。
"""
import os
from datetime import datetime, timedelta, timezone

from google import genai
from google.genai import types
from google.cloud import firestore

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "yui-agent-2026")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "asia-northeast1")
MODEL = "gemini-2.5-flash"
COLLECTION = "task_mentions"
STALENESS_HOURS = float(os.environ.get("STALENESS_HOURS", "6"))
MAX_PRIORITY = 5
RESEARCH_PRIORITY_THRESHOLD = 4


def _client() -> genai.Client:
    return genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)


def _db() -> firestore.Client:
    return firestore.Client(project=PROJECT_ID)


def _research(title: str, reason: str) -> str:
    """Google Search groundingで、タスクに関連する最新情報を裏どりする。"""
    client = _client()
    prompt = (
        f"タスク「{title}」（背景: {reason}）に取り組むうえで役立つ、"
        "最新かつ具体的な情報を日本語で2〜3文にまとめてください。"
    )
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )
    return response.text.strip()


def run_autonomous_review() -> dict:
    db = _db()
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(hours=STALENESS_HOURS)

    docs = db.collection(COLLECTION).where("priority", "<", MAX_PRIORITY).get()

    escalated = []
    researched = []

    for doc in docs:
        data = doc.to_dict()
        last_mentioned = data.get("last_mentioned_at")
        if last_mentioned is None or last_mentioned > stale_cutoff:
            continue

        old_priority = data.get("priority", 1)
        new_priority = min(MAX_PRIORITY, old_priority + 1)
        update = {
            "priority": new_priority,
            "escalated_by_system": True,
            "last_reviewed_at": now,
        }

        if new_priority >= RESEARCH_PRIORITY_THRESHOLD:
            note = _research(data["title"], data.get("reason", ""))
            update["research_note"] = note
            researched.append({"title": data["title"], "research_note": note})

        doc.reference.update(update)
        escalated.append(
            {"title": data["title"], "old_priority": old_priority, "new_priority": new_priority}
        )

    return {"escalated": escalated, "researched": researched, "reviewed_at": now.isoformat()}
