"""AGI-lite: Sense → Diagnose → Act / Ask のループ。

autonomous_review.py が優先度の見直しを担うのに対し、こちらはタスクそのものを
前に進める。ゆいが自分で進められることは実際にやり（下書き作成・裏どり調査）、
判断が必要なことは具体的な質問にして止める。「読む/調べる/下書きするのは自律、
ユーザーの判断が要ることは必ず聞く」という線引き。
"""
import os

from google import genai
from google.genai import types
from google.cloud import firestore
from pydantic import BaseModel, Field

from tasks_client import upsert_task

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "yui-agent-2026")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "asia-northeast1")
MODEL = "gemini-2.5-flash"
COLLECTION = "task_mentions"

DIAGNOSE_INSTRUCTION = """あなたはタスク管理秘書「ゆい」です。1つのタスクについて、
次にどう動くべきかを判断してください。

- research: ゆいが自分でWeb検索して役立つ情報を調べれば前進できる場合
- draft: ゆいが下書き・たたき台・アウトラインを作成すれば前進できる場合
  （例:「資料をまとめる」「メールを書く」等、ゆいが文章の下書きを作れる性質のタスク）
- ask: ユーザー本人にしか分からない判断・好み・事実確認が必要で、ゆいが動くと
  勝手な決めつけになってしまう場合。この場合は具体的で答えやすい質問を1つ作ること
- monitor: 情報が少なすぎてまだ何もできない、様子見でよい場合

不確かな時はresearchやdraftで憶測するより、askで確認する方を優先してください。"""


class Diagnosis(BaseModel):
    action: str = Field(description="research, draft, ask, monitor のいずれか")
    question: str = Field(default="", description="action=askの場合の具体的な質問")


class DraftResult(BaseModel):
    content: str = Field(description="下書き・調査結果の本文")


def _client() -> genai.Client:
    return genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)


def _db() -> firestore.Client:
    return firestore.Client(project=PROJECT_ID)


def _diagnose(title: str, reason: str) -> Diagnosis:
    client = _client()
    prompt = f"タスク: {title}\n背景: {reason}"
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=DIAGNOSE_INSTRUCTION,
            temperature=0,
            response_mime_type="application/json",
            response_schema=Diagnosis,
        ),
    )
    return Diagnosis.model_validate_json(response.text)


def _research(title: str, reason: str) -> str:
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


def _draft(title: str, reason: str) -> str:
    client = _client()
    prompt = (
        f"タスク「{title}」（背景: {reason}）について、ユーザーがすぐ手を加えられる"
        "たたき台・下書き・アウトラインを日本語で作成してください。長すぎないように。"
    )
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.3,
            response_mime_type="application/json",
            response_schema=DraftResult,
        ),
    )
    return DraftResult.model_validate_json(response.text).content


def run_agent_loop() -> dict:
    db = _db()
    docs = db.collection(COLLECTION).where("status", "==", "open").get()

    progressed = []
    asked = []

    for doc in docs:
        data = doc.to_dict()
        title = data.get("title", "")
        reason = data.get("reason", "")
        priority = data.get("priority", 1)

        diagnosis = _diagnose(title, reason)
        update = {}

        if diagnosis.action == "research":
            note = _research(title, reason)
            update = {"status": "in_progress", "agent_notes": note, "pending_question": None}
            progressed.append({"title": title, "action": "research", "note": note})

        elif diagnosis.action == "draft":
            note = _draft(title, reason)
            update = {"status": "in_progress", "agent_notes": note, "pending_question": None}
            progressed.append({"title": title, "action": "draft", "note": note})

        elif diagnosis.action == "ask":
            question = diagnosis.question or "この件、詳しく教えてもらえますか？"
            update = {"status": "needs_input", "pending_question": question}
            asked.append({"title": title, "question": question})

        else:
            continue

        doc.reference.update(update)

        if update.get("agent_notes"):
            new_reason = f"{reason}\n\n[ゆいが自動で対応] {update['agent_notes']}"
            upsert_task(title, priority, new_reason)
        elif update.get("pending_question"):
            new_reason = f"{reason}\n\n[ゆいからの質問] {update['pending_question']}"
            upsert_task(title, priority, new_reason)

    return {"progressed": progressed, "asked": asked}


def answer_question(doc_id: str, answer: str) -> dict:
    """ダッシュボードから、ゆいの質問への回答を受け取り、タスクを前進させる。"""
    db = _db()
    ref = db.collection(COLLECTION).document(doc_id)
    doc = ref.get()
    if not doc.exists:
        return {"error": "task not found"}

    data = doc.to_dict()
    title = data.get("title", "")
    question = data.get("pending_question", "")

    new_reason = f"{data.get('reason', '')}\n\n[質問] {question}\n[回答] {answer}"
    ref.update({
        "status": "open",
        "pending_question": None,
        "reason": new_reason,
    })
    upsert_task(title, data.get("priority", 1), new_reason)
    return {"title": title, "reason": new_reason}


def list_tasks() -> list[dict]:
    db = _db()
    docs = db.collection(COLLECTION).order_by(
        "last_mentioned_at", direction=firestore.Query.DESCENDING
    ).get()
    tasks = []
    for doc in docs:
        data = doc.to_dict()
        tasks.append({
            "id": doc.id,
            "title": data.get("title"),
            "priority": data.get("priority"),
            "status": data.get("status", "open"),
            "reason": data.get("reason"),
            "agent_notes": data.get("agent_notes"),
            "pending_question": data.get("pending_question"),
            "mention_count": data.get("mention_count", 1),
        })
    return tasks
