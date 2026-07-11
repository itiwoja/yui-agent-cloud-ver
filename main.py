"""Yui Cloud Agent — 「入力されなかったタスク」を発見する対話エージェント。

MVP パイプライン:
    対話入力 → Gemini(タスク抽出・優先度・理由) → Firestore(記憶・優先度昇格) → Google Tasks
"""
import asyncio
import base64
import json
import os
import time
import uuid

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent_loop import answer_question, list_tasks, run_agent_loop
from auth import assert_token_configured, require_app_token
from autonomous_review import run_autonomous_review
from chat import append_chat_history, chat_turn, prefetch_context, stream_reply
from confidence import filter_confident
from dialog_actions import extract_dialog_actions
from extraction import extract_tasks
from matching import titles_match
import obs
from rate_limit import require_rate_limit
from memory_store import (
    complete_task,
    delete_task,
    find_open_tasks,
    get_recent_titles,
    record_and_resolve,
)
from speech_to_text import LOCATION as SPEECH_LOCATION
from speech_to_text import MODEL as SPEECH_MODEL
from speech_to_text import transcribe_audio
from tasks_client import complete_google_task, delete_google_task, upsert_task
from tts import stream_synthesize, synthesize_speech
from sentence_split import split_sentences

app = FastAPI(title="Yui Cloud Agent")

APP_VERSION = "0.7.0"
CONFIDENCE_THRESHOLD = float(os.environ.get("YUI_CONFIDENCE_THRESHOLD", "0.6"))
MAX_AUDIO_BYTES = 10 * 1024 * 1024


def _upsert_task_background(title: str, priority: int, reason: str) -> None:
    """Google Tasks の同期をレスポンス送信後に行う。"""
    try:
        upsert_task(title, priority, reason)
    except Exception as exc:
        obs.error(
            "upsert_task failed",
            api="google_tasks",
            detail=str(exc),
            exc_type=type(exc).__name__,
        )


def _complete_google_task_background(title: str) -> None:
    """Google Tasks の完了同期をレスポンス送信後に行う。"""
    try:
        complete_google_task(title)
    except Exception as exc:
        obs.error(
            "complete_google_task failed",
            api="google_tasks",
            detail=str(exc),
            exc_type=type(exc).__name__,
        )


def _finalize_converse_background(
    session_id: str,
    user_text: str,
    reply_parts: list[str],
    completed: list[bool],
) -> None:
    """Persist a successful streamed turn and apply its task actions."""
    if not completed:
        return
    reply = "".join(reply_parts)
    if not reply:
        return

    append_chat_history(session_id, user_text, reply)
    try:
        known_titles = get_recent_titles()
        extracted, completed_titles = extract_dialog_actions(user_text, known_titles)
        for task in filter_confident(extracted, CONFIDENCE_THRESHOLD):
            resolved = record_and_resolve(task.title, task.priority, task.reason)
            _upsert_task_background(
                resolved["title"], resolved["priority"], resolved["reason"]
            )

        open_tasks = find_open_tasks()
        matched_ids = set()
        for candidate in completed_titles:
            for task in open_tasks:
                if task["id"] in matched_ids:
                    continue
                if titles_match(candidate, task.get("title", "")):
                    complete_task(task["id"])
                    _complete_google_task_background(task["title"])
                    matched_ids.add(task["id"])
                    break
    except Exception as exc:
        obs.error(
            "converse dialog actions failed",
            route="/converse",
            api="gemini",
            session_id=session_id,
            detail=str(exc),
            exc_type=type(exc).__name__,
        )


def _ndjson_event(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False) + "\n"


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """リクエスト ID を設定し、レスポンスにも返す。"""
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


@app.on_event("startup")
def verify_runtime_configuration() -> None:
    """本番起動時に fail-open の認証設定を防ぐ。"""
    assert_token_configured()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "agent": "yui", "version": APP_VERSION}


class UtteranceRequest(BaseModel):
    text: str = Field(max_length=4000)


@app.post(
    "/process", dependencies=[Depends(require_app_token), Depends(require_rate_limit)]
)
def process(request: UtteranceRequest, background_tasks: BackgroundTasks) -> dict:
    known_titles = get_recent_titles()
    try:
        extracted = extract_tasks(request.text, known_titles=known_titles)
    except Exception as exc:
        # 独り言の抽出はベストエフォート。失敗しても待機を止めない（空で返す）。
        obs.error("extract_tasks failed", route="/process", detail=str(exc))
        return {"tasks": []}
    confident_tasks = filter_confident(extracted.tasks, CONFIDENCE_THRESHOLD)
    resolved = [
        record_and_resolve(task.title, task.priority, task.reason)
        for task in confident_tasks
    ]
    for task in resolved:
        background_tasks.add_task(
            _upsert_task_background,
            task["title"],
            task["priority"],
            task["reason"],
        )
    return {"tasks": resolved}


class ChatRequest(BaseModel):
    session_id: str = Field(max_length=128)
    message: str = Field(max_length=4000)


@app.post(
    "/chat", dependencies=[Depends(require_app_token), Depends(require_rate_limit)]
)
def chat(
    request: ChatRequest,
    http_request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    started_at = time.perf_counter()
    try:
        result, open_tasks = chat_turn(request.session_id, request.message)
    except Exception as exc:
        # モデル/Firestore障害でも、音声UIが無言にならないようキャラ内で謝って返す。
        obs.error(
            "chat_turn failed",
            route="/chat",
            request_id=http_request.state.request_id,
            session_id=request.session_id,
            detail=str(exc),
            exc_type=type(exc).__name__,
        )
        return {
            "reply": "ごめんね、いまうまく聞き取れなかったみたい。もう一度言ってくれる？",
            "tasks": [],
            "completed_tasks": [],
        }
    background_tasks.add_task(
        append_chat_history, request.session_id, request.message, result.reply
    )
    resolved = [
        record_and_resolve(task.title, task.priority, task.reason)
        for task in result.tasks
    ]
    for task in resolved:
        background_tasks.add_task(
            _upsert_task_background,
            task["title"],
            task["priority"],
            task["reason"],
        )

    completed = []
    matched_ids = set()
    for candidate in result.completed_task_titles:
        for task in open_tasks:
            if task["id"] in matched_ids:
                continue
            if titles_match(candidate, task.get("title", "")):
                completed_task = complete_task(task["id"])
                background_tasks.add_task(_complete_google_task_background, task["title"])
                completed.append(completed_task)
                matched_ids.add(task["id"])
                break

    obs.info(
        "chat request completed",
        route="/chat",
        request_id=http_request.state.request_id,
        session_id=request.session_id,
        tasks=len(resolved),
        completed=len(completed),
        duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
    )
    return {"reply": result.reply, "tasks": resolved, "completed_tasks": completed}


class SpeechRequest(BaseModel):
    text: str = Field(max_length=1000)


@app.post(
    "/tts", dependencies=[Depends(require_app_token), Depends(require_rate_limit)]
)
def tts(request: SpeechRequest, http_request: Request) -> Response:
    started_at = time.perf_counter()
    try:
        audio = synthesize_speech(request.text)
    except Exception as exc:
        obs.error(
            "synthesize_speech failed",
            route="/tts",
            api="texttospeech",
            request_id=http_request.state.request_id,
            chars=len(request.text),
            duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
            detail=str(exc),
            exc_type=type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="text-to-speech unavailable") from exc
    obs.info(
        "speech synthesized",
        route="/tts",
        api="texttospeech",
        request_id=http_request.state.request_id,
        chars=len(request.text),
        duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
    )
    return Response(content=audio, media_type="audio/mpeg")


@app.post(
    "/transcribe", dependencies=[Depends(require_app_token), Depends(require_rate_limit)]
)
async def transcribe(request: Request) -> dict:
    started_at = time.perf_counter()
    audio_bytes = await request.body()
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="audio payload too large")
    try:
        text = await asyncio.to_thread(transcribe_audio, audio_bytes)
    except Exception as exc:
        obs.error(
            "transcribe_audio failed",
            route="/transcribe",
            api="speech_v2",
            request_id=request.state.request_id,
            bytes_in=len(audio_bytes),
            duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
            detail=str(exc),
            exc_type=type(exc).__name__,
            location=SPEECH_LOCATION,
            model=SPEECH_MODEL,
        )
        raise HTTPException(status_code=502, detail="speech-to-text unavailable") from exc
    obs.info(
        "transcribed",
        route="/transcribe",
        api="speech_v2",
        request_id=request.state.request_id,
        bytes_in=len(audio_bytes),
        chars=len(text),
        duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
        location=SPEECH_LOCATION,
        model=SPEECH_MODEL,
    )
    return {"text": text}


@app.post(
    "/converse", dependencies=[Depends(require_app_token), Depends(require_rate_limit)]
)
async def converse(
    request: Request,
    background_tasks: BackgroundTasks,
    session_id: str = Query("default", max_length=128),
) -> StreamingResponse:
    """Stream STT, Gemini, and sentence-level TTS as NDJSON."""
    started_at = time.perf_counter()
    audio_bytes = await request.body()
    prefetch_started_at = time.perf_counter()
    context_future = asyncio.get_event_loop().run_in_executor(
        None, prefetch_context, session_id
    )
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        try:
            await context_future
        except Exception:
            pass
        raise HTTPException(status_code=413, detail="audio payload too large")
    try:
        user_text = await asyncio.to_thread(transcribe_audio, audio_bytes)
    except Exception as exc:
        try:
            await context_future
        except Exception as context_exc:
            obs.warning(
                "converse context prefetch failed",
                route="/converse",
                request_id=request.state.request_id,
                session_id=session_id,
                detail=str(context_exc),
                exc_type=type(context_exc).__name__,
            )
        obs.error(
            "transcribe_audio failed",
            route="/converse",
            api="speech_v2",
            request_id=request.state.request_id,
            bytes_in=len(audio_bytes),
            detail=str(exc),
            exc_type=type(exc).__name__,
        )
        return StreamingResponse(
            iter([_ndjson_event({"type": "error", "message": "speech-to-text unavailable"})]),
            media_type="application/x-ndjson",
        )

    stt_ms = round((time.perf_counter() - started_at) * 1000, 1)
    user_text = user_text.strip()
    if not user_text:
        try:
            await context_future
        except Exception as context_exc:
            obs.warning(
                "converse context prefetch failed",
                route="/converse",
                request_id=request.state.request_id,
                session_id=session_id,
                detail=str(context_exc),
                exc_type=type(context_exc).__name__,
            )
        return StreamingResponse(
            iter([_ndjson_event({"type": "empty"})]),
            media_type="application/x-ndjson",
        )

    context = await context_future
    prefetch_ms = round((time.perf_counter() - prefetch_started_at) * 1000, 1)

    reply_parts: list[str] = []
    completed: list[bool] = []
    background_tasks.add_task(
        _finalize_converse_background,
        session_id,
        user_text,
        reply_parts,
        completed,
    )

    def event_stream():
        buffer = ""
        sentences = 0
        first_sentence_ms: float | None = None
        first_audio_ms: float | None = None

        def sentence_audio_events(sentence: str):
            nonlocal first_audio_ms
            try:
                for pcm in stream_synthesize(sentence):
                    if first_audio_ms is None:
                        first_audio_ms = round(
                            (time.perf_counter() - started_at) * 1000, 1
                        )
                    yield _ndjson_event(
                        {
                            "type": "pcm",
                            "rate": 24000,
                            "data": base64.b64encode(pcm).decode("ascii"),
                            "text": sentence,
                        }
                    )
                return
            except Exception as exc:
                obs.warning(
                    "stream_synthesize failed; falling back to synthesize_speech",
                    route="/converse",
                    api="texttospeech",
                    request_id=request.state.request_id,
                    session_id=session_id,
                    chars=len(sentence),
                    detail=str(exc),
                    exc_type=type(exc).__name__,
                )

            audio = synthesize_speech(sentence)
            if first_audio_ms is None:
                first_audio_ms = round(
                    (time.perf_counter() - started_at) * 1000, 1
                )
            yield _ndjson_event(
                {
                    "type": "audio",
                    "data": base64.b64encode(audio).decode("ascii"),
                    "text": sentence,
                }
            )

        try:
            yield _ndjson_event({"type": "transcript", "text": user_text})
            for chunk in stream_reply(session_id, user_text, context):
                reply_parts.append(chunk)
                buffer += chunk
                ready, buffer = split_sentences(buffer)
                for sentence in ready:
                    if first_sentence_ms is None:
                        first_sentence_ms = round(
                            (time.perf_counter() - started_at) * 1000, 1
                        )
                    sentences += 1
                    yield from sentence_audio_events(sentence)
            if buffer.strip():
                sentence = buffer.strip()
                if first_sentence_ms is None:
                    first_sentence_ms = round(
                        (time.perf_counter() - started_at) * 1000, 1
                    )
                sentences += 1
                yield from sentence_audio_events(sentence)
            reply = "".join(reply_parts)
            completed.append(True)
            yield _ndjson_event({"type": "done", "reply": reply})
            obs.info(
                "converse request completed",
                route="/converse",
                request_id=request.state.request_id,
                session_id=session_id,
                stt_ms=stt_ms,
                prefetch_ms=prefetch_ms,
                first_sentence_ms=first_sentence_ms,
                first_audio_ms=first_audio_ms,
                total_ms=round((time.perf_counter() - started_at) * 1000, 1),
                sentences=sentences,
            )
        except Exception as exc:
            obs.error(
                "converse stream failed",
                route="/converse",
                api="gemini_or_texttospeech",
                request_id=request.state.request_id,
                session_id=session_id,
                detail=str(exc),
                exc_type=type(exc).__name__,
            )
            yield _ndjson_event({"type": "error", "message": "converse unavailable"})

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


@app.post("/autonomous-review", dependencies=[Depends(require_app_token)])
def autonomous_review(request: Request) -> dict:
    """Cloud Scheduler から定期的に叩かれ、ユーザーの指示なしに放置タスクを見直す。"""
    started_at = time.perf_counter()
    try:
        review_result = run_autonomous_review()
        agent_result = run_agent_loop()
    except Exception as exc:
        obs.error(
            "autonomous review failed",
            route="/autonomous-review",
            request_id=request.state.request_id,
            duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
            detail=str(exc),
            exc_type=type(exc).__name__,
        )
        raise
    obs.info(
        "autonomous review completed",
        route="/autonomous-review",
        request_id=request.state.request_id,
        escalated=len(review_result.get("escalated", [])),
        researched=len(review_result.get("researched", [])),
        progressed=len(agent_result.get("progressed", [])),
        asked=len(agent_result.get("asked", [])),
        duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
    )
    return {**review_result, **agent_result}


@app.get("/tasks", dependencies=[Depends(require_app_token)])
def get_tasks() -> dict:
    """ダッシュボード用: 全タスクの状態一覧。"""
    return {"tasks": list_tasks()}


class AnswerRequest(BaseModel):
    answer: str = Field(max_length=2000)


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
