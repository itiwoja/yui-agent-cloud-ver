"""Yui Cloud Agent — 「入力されなかったタスク」を発見する対話エージェント。

MVP パイプライン:
    対話入力 → Gemini(タスク抽出・優先度・理由) → Firestore(記憶・優先度昇格) → Google Tasks
"""
from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from autonomous_review import run_autonomous_review
from chat import chat_turn
from extraction import extract_tasks
from memory_store import get_recent_titles, record_and_resolve
from tasks_client import upsert_task
from tts import synthesize_speech

app = FastAPI(title="Yui Cloud Agent")

APP_VERSION = "0.5.0"


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "agent": "yui", "version": APP_VERSION}


class UtteranceRequest(BaseModel):
    text: str


@app.post("/process")
def process(request: UtteranceRequest) -> dict:
    known_titles = get_recent_titles()
    extracted = extract_tasks(request.text, known_titles=known_titles)
    resolved = [
        record_and_resolve(task.title, task.priority, task.reason)
        for task in extracted.tasks
    ]
    for task in resolved:
        upsert_task(task["title"], task["priority"], task["reason"])
    return {"tasks": resolved}


class ChatRequest(BaseModel):
    session_id: str
    message: str


@app.post("/chat")
def chat(request: ChatRequest) -> dict:
    known_titles = get_recent_titles()
    result = chat_turn(request.session_id, request.message, known_titles)
    resolved = [
        record_and_resolve(task.title, task.priority, task.reason)
        for task in result.tasks
    ]
    for task in resolved:
        upsert_task(task["title"], task["priority"], task["reason"])
    return {"reply": result.reply, "tasks": resolved}


class SpeechRequest(BaseModel):
    text: str


@app.post("/tts")
def tts(request: SpeechRequest) -> Response:
    audio = synthesize_speech(request.text)
    return Response(content=audio, media_type="audio/mpeg")


@app.post("/autonomous-review")
def autonomous_review() -> dict:
    """Cloud Scheduler から定期的に叩かれ、ユーザーの指示なしに放置タスクを見直す。"""
    return run_autonomous_review()


app.mount("/", StaticFiles(directory="static", html=True), name="static")
