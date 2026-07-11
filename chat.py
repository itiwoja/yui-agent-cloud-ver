"""複数ターンの会話をFirestoreに保持しつつ、Geminiで会話応答とタスク抽出を同時に行う。"""
import os
from datetime import datetime, timezone

from google import genai
from google.genai import types
from google.cloud import firestore
from pydantic import BaseModel, Field

import obs
from calendar_client import get_today_events
from extraction import ExtractedTask
from retry import call_with_retry

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "yui-agent-2026")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "asia-northeast1")
MODEL = "gemini-2.5-flash"
CONVERSATIONS_COLLECTION = "conversations"
HISTORY_LIMIT = 20

CHAT_SYSTEM_INSTRUCTION = """あなたは「ゆい」という名前の対話型AI秘書です。ユーザーの雑談・相談・思いつきに、
親しみやすく簡潔な口語で応答してください。ユーザーの発言は音声認識を通しているため、聞き取りミスや
言い淀みが含まれることがあります。

ユーザーが企画・計画・アイデアについて考えを話している場合は、壁打ち相手として振る舞ってください。
発言の意図を汲み取り、具体的な視点・論点・たたき台を返答に含めてください。
「〇〇をどう結び付けるのでしょうか」のように相手の言葉をただ疑問形で反射するだけの、
考えていない返答は禁止です。

タスクを見つけたと判断するのは、発言の内容が具体的で何をすべきか明確な場合だけにしてください。
発言そのものが音声認識の誤りで意味が通らない・文が途切れているなど、聞き取れていない場合にだけ、
勝手に内容を推測して補完せず、replyで「それってどんな内容？」のように聞き返してください。
（内容が複雑・専門的であること自体は聞き返す理由にしないでください。）
この場合tasksは空配列のままにしてください。
次のユーザーの発言で詳細が分かったら、その時点で改めてタスク化してください。
確信を持てる時だけtasksフィールドに構造化して返してください。
「既存タスク一覧」に同じ用件があれば、titleはその表記をそのまま使ってください。
タスクを見つけたことをreplyの中でわざとらしく宣言する必要はありません、自然な会話の流れで触れる程度にしてください。"""

COMPLETION_INSTRUCTION = """
ユーザーが既存タスクを終えたと明確に報告した場合は、該当する名前をcompleted_task_titlesに入れてください。
名前は「既存の未完了タスク一覧」の表記をそのまま使い、推測で完了扱いにしないでください。
完了報告には、replyで短く自然にねぎらってください。同じタスクをtasksへ追加し直さないでください。"""

CALENDAR_INSTRUCTION = """
「今日の予定」がある場合は、必要に応じて予定時刻と未完了タスクを合わせ、取り組む順番や時間の使い方を
具体的に提案してください。予定がない、または取得できない場合は、予定がないと断定せず会話を続けてください。"""


class ChatResult(BaseModel):
    reply: str = Field(description="ゆいとしてユーザーへ返す会話的な応答文")
    tasks: list[ExtractedTask] = Field(default_factory=list)
    completed_task_titles: list[str] = Field(default_factory=list)


def _client() -> genai.Client:
    return genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)


def _db() -> firestore.Client:
    return firestore.Client(project=PROJECT_ID)


def _history_ref(session_id: str):
    return _db().collection(CONVERSATIONS_COLLECTION).document(session_id).collection("messages")


def get_history(session_id: str, limit: int = HISTORY_LIMIT) -> list[dict]:
    docs = (
        _history_ref(session_id)
        .order_by("created_at", direction=firestore.Query.ASCENDING)
        .limit_to_last(limit)
        .get()
    )
    return [doc.to_dict() for doc in docs]


def _append_message(session_id: str, role: str, text: str) -> None:
    _history_ref(session_id).add(
        {"role": role, "text": text, "created_at": datetime.now(timezone.utc)}
    )


def chat_turn(session_id: str, user_text: str, known_titles: list[str]) -> ChatResult:
    history = get_history(session_id)

    try:
        today_events = get_today_events()
    except Exception as exc:
        obs.warning("failed to get today's events", detail=str(exc))
        today_events = []

    contents = [
        types.Content(role=msg["role"], parts=[types.Part(text=msg["text"])])
        for msg in history
    ]

    titles_block = "\n".join(f"- {t}" for t in known_titles) if known_titles else "（なし）"
    events_block = (
        "\n".join(
            f"- {event['summary']}: {event['start']} ～ {event['end']}"
            for event in today_events
        )
        if today_events
        else "（取得できた予定なし）"
    )
    user_message = (
        f"既存の未完了タスク一覧:\n{titles_block}\n\n"
        f"今日の予定（JST）:\n{events_block}\n\n発言:\n{user_text}"
    )
    contents.append(types.Content(role="user", parts=[types.Part(text=user_message)]))

    client = _client()
    response = call_with_retry(
        lambda: client.models.generate_content(
            model=MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=(
                    CHAT_SYSTEM_INSTRUCTION + COMPLETION_INSTRUCTION + CALENDAR_INSTRUCTION
                ),
                temperature=0.4,
                # 明示しないとVertex AI側のデフォルト任せになり、複雑な相談でも
                # 考え抜かず浅い返答になりがちなため、動的思考を明示的に有効化する。
                thinking_config=types.ThinkingConfig(thinking_budget=-1),
                response_mime_type="application/json",
                response_schema=ChatResult,
            ),
        )
    )
    result = ChatResult.model_validate_json(response.text)

    _append_message(session_id, "user", user_text)
    _append_message(session_id, "model", result.reply)

    return result
