"""Yui Cloud Agent — 「入力されなかったタスク」を発見する対話エージェント。

MVP パイプライン:
    対話入力 → Gemini(タスク抽出・優先度・理由) → Firestore(記憶・優先度昇格) → Google Tasks
"""
from fastapi import FastAPI
from pydantic import BaseModel

from extraction import extract_tasks
from memory_store import record_and_resolve

app = FastAPI(title="Yui Cloud Agent")

APP_VERSION = "0.1.0"


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "agent": "yui", "version": APP_VERSION}


class UtteranceRequest(BaseModel):
    text: str


@app.post("/process")
def process(request: UtteranceRequest) -> dict:
    extracted = extract_tasks(request.text)
    resolved = [
        record_and_resolve(task.title, task.priority, task.reason)
        for task in extracted.tasks
    ]
    return {"tasks": resolved}
