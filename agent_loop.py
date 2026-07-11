"""AGI-lite: Sense → Diagnose → Act / Ask のループ。

autonomous_review.py が優先度の見直しを担うのに対し、こちらはタスクそのものを
前に進める。ゆいが自分で進められることは実際にやり（下書き作成・裏どり調査）、
判断が必要なことは具体的な質問にして止める。「読む/調べる/下書きするのは自律、
ユーザーの判断が要ることは必ず聞く」という線引き。
"""
import os

from google.genai import types
from google.cloud import firestore
from pydantic import BaseModel, Field

import obs
from agent_verify import plan_after_verification
from clients import DEFAULT_MODEL, firestore_client, gemini_client
from dedup import is_duplicate
from research import research as _research
from retry import call_with_retry
from tasks_client import upsert_task

MODEL = DEFAULT_MODEL
COLLECTION = "task_mentions"
LIST_LIMIT = int(os.environ.get("YUI_LIST_LIMIT", "200"))
AGENT_LOOP_LIMIT = int(os.environ.get("YUI_AGENT_LOOP_LIMIT", "50"))

DIAGNOSE_INSTRUCTION = """あなたはタスク管理秘書「ゆい」です。1つのタスクについて、
次にどう動くべきかを判断してください。

- research: ゆいが自分でWeb検索して役立つ情報を調べれば前進できる場合
- draft: ゆいが下書き・たたき台・アウトラインを作成すれば前進できる場合
  （例:「資料をまとめる」「メールを書く」等、ゆいが文章の下書きを作れる性質のタスク）
- ask: ユーザー本人にしか分からない判断・好み・事実確認が必要で、ゆいが動くと
  勝手な決めつけになってしまう場合。この場合は具体的で答えやすい質問を1つ作ること
- monitor: 情報が少なすぎてまだ何もできない、様子見でよい場合

不確かな時はresearchやdraftで憶測するより、askで確認する方を優先してください。"""

VERIFY_INSTRUCTION = """エージェントがaction（researchまたはdraft）で作ったnoteが、
このタスクを実際に前進させる具体的で有用な内容かを判定してください。
不十分ならfollowup_actionをaskまたはmonitorにしてください。askの場合は、ユーザーが
答えやすい具体的なquestionを1つだけ返してください。"""


class Diagnosis(BaseModel):
    action: str = Field(description="research, draft, ask, monitor のいずれか")
    question: str = Field(default="", description="action=askの場合の具体的な質問")


class DraftResult(BaseModel):
    content: str = Field(description="下書き・調査結果の本文")


class Verification(BaseModel):
    sufficient: bool
    followup_action: str = Field(default="", description="空文字、ask、monitorのいずれか")
    question: str = Field(default="", description="askの場合の具体的な質問")


_client = gemini_client
_db = firestore_client


def _diagnose(title: str, reason: str) -> Diagnosis:
    client = _client()
    prompt = f"タスク: {title}\n背景: {reason}"
    response = call_with_retry(
        lambda: client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=DIAGNOSE_INSTRUCTION,
                temperature=0,
                response_mime_type="application/json",
                response_schema=Diagnosis,
            ),
        )
    )
    return Diagnosis.model_validate_json(response.text)


def _draft(title: str, reason: str) -> str:
    client = _client()
    prompt = (
        f"タスク「{title}」（背景: {reason}）について、ユーザーがすぐ手を加えられる"
        "たたき台・下書き・アウトラインを日本語で作成してください。長すぎないように。"
    )
    response = call_with_retry(
        lambda: client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                response_mime_type="application/json",
                response_schema=DraftResult,
            ),
        )
    )
    return DraftResult.model_validate_json(response.text).content


def _verify(title: str, reason: str, action: str, note: str) -> Verification:
    client = _client()
    prompt = f"タスク: {title}\n背景: {reason}\naction: {action}\nnote: {note}"
    response = call_with_retry(
        lambda: client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=VERIFY_INSTRUCTION,
                temperature=0,
                response_mime_type="application/json",
                response_schema=Verification,
            ),
        )
    )
    return Verification.model_validate_json(response.text)


def run_agent_loop() -> dict:
    db = _db()
    docs = (
        db.collection(COLLECTION)
        .where("status", "==", "open")
        .limit(AGENT_LOOP_LIMIT)
        .get()
    )

    progressed = []
    asked = []

    for doc in docs:
        try:
            data = doc.to_dict()
            title = data.get("title", "")
            reason = data.get("reason", "")
            priority = data.get("priority", 1)

            asked_questions = data.get("asked_questions", [])
            diagnosis = _diagnose(title, reason)
            update = {}

            if diagnosis.action in {"research", "draft"}:
                action = diagnosis.action
                note = (
                    _research(title, reason)
                    if action == "research"
                    else _draft(title, reason)
                )
                verification = _verify(title, reason, action, note)
                plan = plan_after_verification(
                    note,
                    verification.sufficient,
                    verification.followup_action,
                    verification.question,
                    asked_questions,
                )
                update = plan["update"]
                if plan["outcome"] == "progressed":
                    progressed.append({"title": title, "action": action, "note": note})
                elif plan["outcome"] == "asked":
                    asked.append({"title": title, "question": plan["question"]})

            elif diagnosis.action == "ask":
                question = diagnosis.question or "この件、詳しく教えてもらえますか？"
                # 一度した質問は繰り返さない（回答後に status が open へ戻るため、記憶が
                # ないと同じ質問を無限に聞き返す退行が起きる）。既出なら様子見に倒す。
                if is_duplicate(question, asked_questions):
                    continue
                update = {
                    "status": "needs_input",
                    "pending_question": question,
                    "asked_questions": asked_questions + [question],
                }
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
        except Exception as exc:
            obs.error(
                "agent loop item failed",
                api="agent_loop",
                doc_id=doc.id,
                detail=str(exc),
                exc_type=type(exc).__name__,
            )
            continue

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
    # status のサーバー側絞り込みは複合インデックスを要求し、本番を壊すおそれがある。
    # インデックス不要の現行設計を保ち、取得件数だけを安全に制限する。
    docs = db.collection(COLLECTION).order_by(
        "last_mentioned_at", direction=firestore.Query.DESCENDING
    ).limit(LIST_LIMIT).get()
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
