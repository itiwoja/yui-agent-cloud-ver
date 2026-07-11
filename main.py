"""Yui Cloud Agent — 「入力されなかったタスク」を発見する対話エージェント。

MVP パイプライン:
    対話入力 → Gemini(タスク抽出・優先度・理由) → Firestore(記憶・優先度昇格) → Google Tasks
"""
from fastapi import Depends, FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent_loop import answer_question, list_tasks, run_agent_loop
from auth import require_app_token
from autonomous_review import run_autonomous_review
from chat import chat_turn
from extraction import extract_tasks
from matching import titles_match
import obs
from memory_store import (
    complete_task,
    delete_task,
    find_open_tasks,
    get_recent_titles,
    record_and_resolve,
)
from speech_to_text import transcribe_audio
from tasks_client import complete_google_task, delete_google_task, upsert_task
from tts import synthesize_speech

app = FastAPI(title="Yui Cloud Agent")

APP_VERSION = "0.7.0"


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "agent": "yui", "version": APP_VERSION}


class UtteranceRequest(BaseModel):
    text: str


@app.post("/process", dependencies=[Depends(require_app_token)])
def process(request: UtteranceRequest) -> dict:
    known_titles = get_recent_titles()
    try:
        extracted = extract_tasks(request.text, known_titles=known_titles)
    except Exception as exc:
        # 独り言の抽出はベストエフォート。失敗しても待機を止めない（空で返す）。
        obs.error("extract_tasks failed", route="/process", detail=str(exc))
        return {"tasks": []}
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


@app.post("/chat", dependencies=[Depends(require_app_token)])
def chat(request: ChatRequest) -> dict:
    open_tasks = find_open_tasks()
    known_titles = [task["title"] for task in open_tasks if task.get("title")]
    try:
        result = chat_turn(request.session_id, request.message, known_titles)
    except Exception as exc:
        # モデル/Firestore障害でも、音声UIが無言にならないようキャラ内で謝って返す。
        obs.error("chat_turn failed", route="/chat", detail=str(exc))
        return {
            "reply": "ごめんね、いまうまく聞き取れなかったみたい。もう一度言ってくれる？",
            "tasks": [],
            "completed_tasks": [],
        }
    resolved = [
        record_and_resolve(task.title, task.priority, task.reason)
        for task in result.tasks
    ]
    for task in resolved:
        upsert_task(task["title"], task["priority"], task["reason"])

    completed = []
    matched_ids = set()
    for candidate in result.completed_task_titles:
        for task in open_tasks:
            if task["id"] in matched_ids:
                continue
            if titles_match(candidate, task.get("title", "")):
                completed_task = complete_task(task["id"])
                complete_google_task(task["title"])
                completed.append(completed_task)
                matched_ids.add(task["id"])
                break

    return {"reply": result.reply, "tasks": resolved, "completed_tasks": completed}


class SpeechRequest(BaseModel):
    text: str


@app.post("/tts", dependencies=[Depends(require_app_token)])
def tts(request: SpeechRequest) -> Response:
    audio = synthesize_speech(request.text)
    return Response(content=audio, media_type="audio/mpeg")


@app.post("/transcribe", dependencies=[Depends(require_app_token)])
async def transcribe(request: Request) -> dict:
    audio_bytes = await request.body()
    text = transcribe_audio(audio_bytes)
    obs.info("transcribed", route="/transcribe", chars=len(text))
    return {"text": text}


@app.post("/autonomous-review", dependencies=[Depends(require_app_token)])
def autonomous_review() -> dict:
    """Cloud Scheduler から定期的に叩かれ、ユーザーの指示なしに放置タスクを見直す。"""
    review_result = run_autonomous_review()
    agent_result = run_agent_loop()
    return {**review_result, **agent_result}


@app.get("/tasks", dependencies=[Depends(require_app_token)])
def get_tasks() -> dict:
    """ダッシュボード用: 全タスクの状態一覧。"""
    return {"tasks": list_tasks()}


class AnswerRequest(BaseModel):
    answer: str


@app.post("/tasks/{doc_id}/answer", dependencies=[Depends(require_app_token)])
def post_answer(doc_id: str, request: AnswerRequest) -> dict:
    """ゆいからの質問にユーザーが回答し、タスクを前進させる。"""
    return answer_question(doc_id, request.answer)


@app.post("/tasks/{doc_id}/complete", dependencies=[Depends(require_app_token)])
def post_complete(doc_id: str) -> dict:
    """指定したタスクをFirestoreとGoogle Tasksの両方で完了にする。"""
    task = complete_task(doc_id)
    if "error" in task:
        return task
    google_task_id = complete_google_task(task["title"])
    return {**task, "google_task_id": google_task_id}


@app.delete("/tasks/{doc_id}", dependencies=[Depends(require_app_token)])
def delete_task_endpoint(doc_id: str) -> dict:
    """誤って拾われたタスクをFirestoreとGoogle Tasksの両方から取り消す。"""
    task = delete_task(doc_id)
    if "error" in task:
        return task
    google_task_id = delete_google_task(task["title"])
    return {**task, "google_task_id": google_task_id}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
