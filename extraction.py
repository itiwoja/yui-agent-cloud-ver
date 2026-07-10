"""雑な対話テキストからタスクを抽出する — Gemini構造化出力（Vertex AI, ADC認証）。"""
import os

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "yui-agent-2026")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "asia-northeast1")
MODEL = "gemini-2.5-flash"

SYSTEM_INSTRUCTION = """あなたはユーザーの雑な独り言・思いつき・メモから、実行すべきタスクを発見する秘書エージェント「ゆい」です。
明示的な指示だけでなく、文脈から「これはやらないといけない」と読み取れる暗黙のタスクも拾ってください。
タスクが1つも無ければ空配列を返してください。優先度は 1（低）〜5（緊急）の整数で、そう判断した理由を必ず添えてください。"""


class ExtractedTask(BaseModel):
    title: str = Field(description="タスクの内容を簡潔に表す短い文")
    priority: int = Field(ge=1, le=5, description="優先度 1(低)〜5(緊急)")
    reason: str = Field(description="この優先度をつけた理由")


class ExtractionResult(BaseModel):
    tasks: list[ExtractedTask]


def _client() -> genai.Client:
    return genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)


def extract_tasks(utterance: str) -> ExtractionResult:
    client = _client()
    response = client.models.generate_content(
        model=MODEL,
        contents=utterance,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0,
            response_mime_type="application/json",
            response_schema=ExtractionResult,
        ),
    )
    return ExtractionResult.model_validate_json(response.text)
