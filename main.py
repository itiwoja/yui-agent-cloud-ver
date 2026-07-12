"""Yui Cloud Agent — 「入力されなかったタスク」を発見する対話エージェント。

MVP パイプライン:
    対話入力 → Gemini(タスク抽出・優先度・理由) → Firestore(記憶・優先度昇格) → Google Tasks
"""
import asyncio
import base64
import json
import os
import queue
import threading
import time
import uuid

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from google.api_core.exceptions import AlreadyExists
from pydantic import UUID4, BaseModel, Field

from agent_loop import answer_question, list_tasks, run_agent_loop
from auth import assert_token_configured, require_app_token
from autonomous_review import run_autonomous_review
from background_queue import enqueue_finalize_turn
from chat import ContextBundle, append_chat_history, chat_turn, prefetch_context, stream_reply
from clients import firestore_client
from confidence import filter_confident
from dialog_actions import extract_dialog_actions
from extraction import extract_tasks
from matching import titles_match
import obs
from rate_limit import require_rate_limit
from memory_store import (
    complete_task,
    delete_task,
    find_pending_questions,
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
from tracing import setup_tracing, span

app = FastAPI(title="Yui Cloud Agent")
setup_tracing(app)

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
            title=title,
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
            title=title,
            detail=str(exc),
            exc_type=type(exc).__name__,
        )


def _record_and_upsert_task_background(
    title: str, priority: int, reason: str, route: str
) -> None:
    """Persist one extracted task without allowing a failure to stop its peers."""
    try:
        resolved = record_and_resolve(title, priority, reason)
        _upsert_task_background(
            resolved["title"], resolved["priority"], resolved["reason"]
        )
    except Exception as exc:
        obs.error(
            "record_and_resolve failed",
            route=route,
            title=title,
            detail=str(exc),
            exc_type=type(exc).__name__,
        )


def _enqueue_or_finalize_turn_background(
    session_id: str,
    user_text: str,
    reply: str,
    turn_id: str,
    request_id: str,
) -> None:
    """Enqueue durable finalization without holding the response stream open."""
    try:
        if enqueue_finalize_turn(
            session_id,
            user_text,
            reply,
            turn_id=turn_id,
            request_id=request_id,
        ):
            obs.info(
                "converse finalization enqueued",
                route="/converse",
                request_id=request_id,
                session_id=session_id,
            )
            return
    except Exception as exc:
        obs.error(
            "converse finalization enqueue failed",
            route="/converse",
            request_id=request_id,
            session_id=session_id,
            detail=str(exc),
            exc_type=type(exc).__name__,
        )

    try:
        finalize_turn(session_id, user_text, reply, turn_id)
    except Exception as exc:
        obs.error(
            "converse local finalization failed",
            route="/converse",
            request_id=request_id,
            session_id=session_id,
            detail=str(exc),
            exc_type=type(exc).__name__,
        )


def _claim_finalization(turn_id: str) -> bool:
    """Atomically claim a turn so a Cloud Tasks retry cannot repeat its effects."""
    try:
        firestore_client().collection("finalized_turns").document(turn_id).create(
            {"created_at": time.time()}
        )
    except AlreadyExists:
        obs.info("finalize turn deduped", turn_id=turn_id)
        return False
    return True


def finalize_turn(
    session_id: str, user_text: str, reply: str, turn_id: str | None = None
) -> None:
    """Persist a completed conversation turn and apply its task actions."""
    if turn_id and not _claim_finalization(turn_id):
        return
    append_chat_history(session_id, user_text, reply)
    try:
        known_titles = get_recent_titles()
        pending_questions = find_pending_questions()
        extracted, completed_titles, question_answers = extract_dialog_actions(
            user_text, known_titles, pending_questions
        )
        open_tasks = find_open_tasks()
        for task in filter_confident(extracted, CONFIDENCE_THRESHOLD):
            try:
                resolved = record_and_resolve(
                    task.title, task.priority, task.reason, open_tasks=open_tasks
                )
                _upsert_task_background(
                    resolved["title"], resolved["priority"], resolved["reason"]
                )
            except Exception as exc:
                obs.error(
                    "dialog action item failed",
                    stage="record_and_resolve",
                    title=task.title,
                    session_id=session_id,
                    detail=str(exc),
                    exc_type=type(exc).__name__,
                )
        matched_ids = set()
        for candidate in completed_titles:
            for task in open_tasks:
                if task["id"] in matched_ids:
                    continue
                if titles_match(candidate, task.get("title", "")):
                    try:
                        complete_task(task["id"])
                        _complete_google_task_background(task["title"])
                        matched_ids.add(task["id"])
                    except Exception as exc:
                        obs.error(
                            "dialog action item failed",
                            stage="complete_task",
                            title=task.get("title", candidate),
                            session_id=session_id,
                            detail=str(exc),
                            exc_type=type(exc).__name__,
                        )
                    break

        matched_question_ids = set()
        for question_answer in question_answers:
            for question in pending_questions:
                if question["id"] in matched_question_ids:
                    continue
                if titles_match(question_answer.task_title, question.get("title", "")):
                    try:
                        answer_question(question["id"], question_answer.answer)
                    except Exception as exc:
                        obs.error(
                            "converse question answer failed",
                            route="/converse",
                            session_id=session_id,
                            detail=str(exc),
                            exc_type=type(exc).__name__,
                        )
                    matched_question_ids.add(question["id"])
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


class FinalizeTurnRequest(BaseModel):
    session_id: str = Field(max_length=128)
    user_text: str = Field(max_length=4000)
    reply: str = Field(max_length=4000)
    turn_id: UUID4 | None = None


def _ndjson_event(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False) + "\n"


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """リクエスト ID を設定し、レスポンスにも返す。"""
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@app.on_event("startup")
def verify_runtime_configuration() -> None:
    """本番起動時に fail-open の認証設定を防ぐ。"""
    assert_token_configured()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "agent": "yui", "version": APP_VERSION}


@app.post(
    "/internal/finalize-turn",
    dependencies=[Depends(require_app_token), Depends(require_rate_limit)],
)
def internal_finalize_turn(request: FinalizeTurnRequest, http_request: Request) -> dict:
    """Run a durable Cloud Tasks finalization request."""
    retry_count = http_request.headers.get("x-cloudtasks-taskretrycount")
    try:
        if request.turn_id:
            finalize_turn(
                request.session_id,
                request.user_text,
                request.reply,
                turn_id=str(request.turn_id),
            )
        else:
            finalize_turn(request.session_id, request.user_text, request.reply)
    except Exception as exc:
        obs.error(
            "finalize turn failed",
            route="/internal/finalize-turn",
            session_id=request.session_id,
            retry_count=retry_count,
            detail=str(exc),
            exc_type=type(exc).__name__,
        )
        raise HTTPException(status_code=500, detail="finalize turn failed") from exc
    obs.info(
        "finalize turn completed",
        route="/internal/finalize-turn",
        session_id=request.session_id,
        retry_count=retry_count,
    )
    return {"status": "ok"}


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
    for task in confident_tasks:
        background_tasks.add_task(
            _record_and_upsert_task_background,
            task.title,
            task.priority,
            task.reason,
            "/process",
        )
    return {"tasks": confident_tasks}


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
    for task in result.tasks:
        background_tasks.add_task(
            _record_and_upsert_task_background,
            task.title,
            task.priority,
            task.reason,
            "/chat",
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
        tasks=len(result.tasks),
        completed=len(completed),
        duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
    )
    return {
        "reply": result.reply,
        "tasks": result.tasks,
        "completed_tasks": completed,
    }


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
    if not request.headers.get("content-type", "").lower().startswith("audio/"):
        raise HTTPException(status_code=415, detail="audio content type required")
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
    if not request.headers.get("content-type", "").lower().startswith("audio/"):
        raise HTTPException(status_code=415, detail="audio content type required")
    turn_id = str(uuid.uuid4())
    audio_bytes = await request.body()
    prefetch_started_at = time.perf_counter()
    context_future = asyncio.get_running_loop().run_in_executor(
        None, prefetch_context, session_id
    )
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        try:
            await context_future
        except Exception as exc:
            obs.warning(
                "converse context prefetch failed",
                route="/converse",
                request_id=request.state.request_id,
                session_id=session_id,
                detail=str(exc),
                exc_type=type(exc).__name__,
            )
        raise HTTPException(status_code=413, detail="audio payload too large")
    try:
        with span("stt"):
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

    try:
        context = await context_future
    except Exception as exc:
        obs.warning(
            "converse context prefetch failed",
            route="/converse",
            request_id=request.state.request_id,
            session_id=session_id,
            detail=str(exc),
            exc_type=type(exc).__name__,
        )
        context = ContextBundle(
            history=[], today_events=[], open_tasks=[], pending_questions=[]
        )
    prefetch_ms = round((time.perf_counter() - prefetch_started_at) * 1000, 1)

    def event_stream():
        sentences = 0
        first_sentence_ms: float | None = None
        first_audio_ms: float | None = None

        # 注意: ジェネレータ内で yield を跨ぐ span は OpenTelemetry のコンテキストを
        # 壊す（"was created in a different Context" が毎リクエスト発生）ため使わない。
        # 区間の詳細時間は obs.info の stt_ms/first_audio_ms 等の構造化ログが担う。
        def sentence_audio_events(sentence: str):
            nonlocal first_audio_ms
            emitted_pcm = False
            try:
                for pcm in stream_synthesize(sentence):
                    emitted_pcm = True
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
                if emitted_pcm:
                    obs.warning(
                        "streaming tts failed mid-sentence; skipping fallback",
                        route="/converse",
                        api="texttospeech",
                        request_id=request.state.request_id,
                        session_id=session_id,
                        chars=len(sentence),
                        detail=str(exc),
                        exc_type=type(exc).__name__,
                    )
                    return
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

        events: queue.Queue[tuple[str, object]] = queue.Queue()
        stop_producer = threading.Event()

        def produce_sentences() -> None:
            """Read Gemini continuously so TTS never blocks the next generation."""
            buffer = ""
            reply_parts: list[str] = []
            try:
                for chunk in stream_reply(session_id, user_text, context):
                    if stop_producer.is_set():
                        return
                    reply_parts.append(chunk)
                    buffer += chunk
                    ready, buffer = split_sentences(buffer)
                    for sentence in ready:
                        events.put(("sentence", sentence))
                if buffer.strip():
                    events.put(("sentence", buffer.strip()))
                events.put(("done", "".join(reply_parts)))
            except Exception as exc:
                events.put(("error", exc))

        producer = threading.Thread(
            target=produce_sentences, name="yui-gemini-stream", daemon=True
        )

        try:
            yield _ndjson_event({"type": "transcript", "text": user_text})
            producer.start()
            reply = ""
            while True:
                event_type, payload = events.get()
                if event_type == "error":
                    raise payload
                if event_type == "done":
                    reply = payload
                    break
                sentence = payload
                if first_sentence_ms is None:
                    first_sentence_ms = round(
                        (time.perf_counter() - started_at) * 1000, 1
                    )
                sentences += 1
                yield from sentence_audio_events(sentence)
            finalize_via = "skipped_empty_reply"
            if reply:
                # BackgroundTasks runs this sync work in Starlette's worker pool after
                # the stream completes, so Cloud Tasks latency cannot delay `done`.
                background_tasks.add_task(
                    _enqueue_or_finalize_turn_background,
                    session_id,
                    user_text,
                    reply,
                    turn_id,
                    request.state.request_id,
                )
                finalize_via = "background_enqueue"
            else:
                obs.warning(
                    "converse produced empty reply",
                    route="/converse",
                    request_id=request.state.request_id,
                    session_id=session_id,
                )
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
                finalize_via=finalize_via,
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
        finally:
            stop_producer.set()
            if producer.is_alive():
                producer.join()

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
    try:
        return answer_question(doc_id, request.answer)
    except Exception as exc:
        obs.error(
            "task action failed",
            route="/tasks/{doc_id}/answer",
            doc_id=doc_id,
            detail=str(exc),
            exc_type=type(exc).__name__,
        )
        raise HTTPException(status_code=500, detail="task action failed") from exc


@app.post("/tasks/{doc_id}/complete", dependencies=[Depends(require_app_token)])
def post_complete(doc_id: str) -> dict:
    """指定したタスクをFirestoreとGoogle Tasksの両方で完了にする。"""
    try:
        task = complete_task(doc_id)
        if "error" in task:
            return task
        google_task_id = complete_google_task(task["title"])
        return {**task, "google_task_id": google_task_id}
    except Exception as exc:
        obs.error(
            "task action failed",
            route="/tasks/{doc_id}/complete",
            doc_id=doc_id,
            detail=str(exc),
            exc_type=type(exc).__name__,
        )
        raise HTTPException(status_code=500, detail="task action failed") from exc


@app.delete("/tasks/{doc_id}", dependencies=[Depends(require_app_token)])
def delete_task_endpoint(doc_id: str) -> dict:
    """誤って拾われたタスクをFirestoreとGoogle Tasksの両方から取り消す。"""
    try:
        task = delete_task(doc_id)
        if "error" in task:
            return task
        google_task_id = delete_google_task(task["title"])
        return {**task, "google_task_id": google_task_id}
    except Exception as exc:
        obs.error(
            "task action failed",
            route="/tasks/{doc_id}",
            doc_id=doc_id,
            detail=str(exc),
            exc_type=type(exc).__name__,
        )
        raise HTTPException(status_code=500, detail="task action failed") from exc


app.mount("/", StaticFiles(directory="static", html=True), name="static")
