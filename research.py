"""Google Search grounding を使った共通のタスク調査。"""
from google.genai import types

from clients import DEFAULT_MODEL, gemini_client
from retry import call_with_retry

_client = gemini_client


def research(title: str, reason: str) -> str:
    """Google Search groundingで、タスクに関連する最新情報を裏どりする。"""
    client = _client()
    prompt = (
        f"タスク「{title}」（背景: {reason}）に取り組むうえで役立つ、"
        "最新かつ具体的な情報を日本語で2〜3文にまとめてください。"
    )
    response = call_with_retry(
        lambda: client.models.generate_content(
            model=DEFAULT_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0,
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
    )
    return response.text.strip()
