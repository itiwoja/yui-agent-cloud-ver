"""Yui Cloud Agent — 「入力されなかったタスク」を発見する対話エージェント。

MVP パイプライン:
    対話入力 → Gemini(タスク抽出・優先度・理由) → Firestore(記憶・優先度昇格) → Google Tasks
"""
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent_loop import answer_question, list_tasks, run_agent_loop
from autonomous_review import run_autonomous_review
from chat import chat_turn
from extraction import extract_tasks
from memory_store import complete_task, find_open_tasks, get_recent_titles, record_and_resolve
from speech_to_text import transcribe_audio
from tasks_client import complete_google_task, upsert_task
from tts import synthesize_speech

app = FastAPI(title="Yui Cloud Agent")

APP_VERSION = "0.7.0"


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
    open_tasks = find_open_tasks()
    known_titles = [task["title"] for task in open_tasks if task.get("title")]
    result = chat_turn(request.session_id, request.message, known_titles)
    resolved = [
        record_and_resolve(task.title, task.priority, task.reason)
        for task in result.tasks
    ]
    for task in resolved:
        upsert_task(task["title"], task["priority"], task["reason"])

    completed = []
    matched_ids = set()
    for candidate in result.completed_task_titles:
        normalized_candidate = "".join(candidate.lower().split())
        for task in open_tasks:
            normalized_title = "".join(task.get("title", "").lower().split())
            if (
                task["id"] not in matched_ids
                and normalized_candidate
                and normalized_title
                and (
                    normalized_candidate in normalized_title
                    or normalized_title in normalized_candidate
                )
            ):
                completed_task = complete_task(task["id"])
                complete_google_task(task["title"])
                completed.append(completed_task)
                matched_ids.add(task["id"])
                break

    return {"reply": result.reply, "tasks": resolved, "completed_tasks": completed}


class SpeechRequest(BaseModel):
    text: str


@app.post("/tts")
def tts(request: SpeechRequest) -> Response:
    audio = synthesize_speech(request.text)
    return Response(content=audio, media_type="audio/mpeg")


@app.post("/transcribe")
async def transcribe(request: Request) -> dict:
    audio_bytes = await request.body()
    text = transcribe_audio(audio_bytes)
    print(f"[transcribe] {text!r}")
    return {"text": text}


@app.post("/autonomous-review")
def autonomous_review() -> dict:
    """Cloud Scheduler から定期的に叩かれ、ユーザーの指示なしに放置タスクを見直す。"""
    review_result = run_autonomous_review()
    agent_result = run_agent_loop()
    return {**review_result, **agent_result}


@app.get("/tasks")
def get_tasks() -> dict:
    """ダッシュボード用: 全タスクの状態一覧。"""
    return {"tasks": list_tasks()}


class AnswerRequest(BaseModel):
    answer: str


@app.post("/tasks/{doc_id}/answer")
def post_answer(doc_id: str, request: AnswerRequest) -> dict:
    """ゆいからの質問にユーザーが回答し、タスクを前進させる。"""
    return answer_question(doc_id, request.answer)


@app.post("/tasks/{doc_id}/complete")
def post_complete(doc_id: str) -> dict:
    """指定したタスクをFirestoreとGoogle Tasksの両方で完了にする。"""
    task = complete_task(doc_id)
    if "error" in task:
        return task
    google_task_id = complete_google_task(task["title"])
    return {**task, "google_task_id": google_task_id}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
