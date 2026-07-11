"""雑な対話テキストからタスクを抽出する — Gemini構造化出力（Vertex AI, ADC認証）。"""
import os

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from retry import call_with_retry

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "yui-agent-2026")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "asia-northeast1")
MODEL = "gemini-2.5-flash"

SYSTEM_INSTRUCTION = """あなたはユーザーの雑な独り言・思いつき・メモから、実行すべきタスクを発見する秘書エージェント「ゆい」です。
この発言はマイクで拾った独り言であり、聞き返して確認することはできません。
発言に実際に含まれている内容だけを根拠にタスクを抽出してください。憶測や創作、話を膨らませることは禁止です。
音声認識の誤りで意味が通らない・断片的すぎる・具体性に欠けて何をすべきか判断できない場合は、
無理にタスク化せず何も抽出しないでください（空配列を返す）。確信が持てる時だけ抽出してください。
優先度は 1（低）〜5（緊急）の整数で、そう判断した理由を必ず添えてください。
各タスクに確信度 confidence(0-1) を付けてください。独り言は聞き取り誤りを含むため、
少しでも曖昧なら低め（0.6未満）にしてください。

「既存タスク一覧」が渡された場合、今回の発言が既存タスクと同じ内容を指しているなら、
titleは新しく作らず既存タスクの表記を一字一句そのまま使ってください（言い回しが違っても同じ用件なら同一タスク扱い）。
既存のどれとも異なる新しいタスクの場合のみ、新しい簡潔なtitleを作ってください。"""


class ExtractedTask(BaseModel):
    title: str = Field(description="タスクの内容を簡潔に表す短い文")
    priority: int = Field(ge=1, le=5, description="優先度 1(低)〜5(緊急)")
    reason: str = Field(description="この優先度をつけた理由")
    confidence: float = Field(ge=0.0, le=1.0, description="このタスク抽出の確信度(0-1)")


class ExtractionResult(BaseModel):
    tasks: list[ExtractedTask]


def _client() -> genai.Client:
    return genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)


def extract_tasks(utterance: str, known_titles: list[str] | None = None) -> ExtractionResult:
    client = _client()
    contents = utterance
    if known_titles:
        titles_block = "\n".join(f"- {t}" for t in known_titles)
        contents = f"既存タスク一覧:\n{titles_block}\n\n今回の発言:\n{utterance}"

    response = call_with_retry(
        lambda: client.models.generate_content(
            model=MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0,
                # 優先度判断の質を上げるため動的思考を明示的に有効化する（裏で動くのでレイテンシは許容）。
                thinking_config=types.ThinkingConfig(thinking_budget=-1),
                response_mime_type="application/json",
                response_schema=ExtractionResult,
            ),
        )
    )
    return ExtractionResult.model_validate_json(response.text)
