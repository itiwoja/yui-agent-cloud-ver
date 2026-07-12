import json
import os
import uuid
from types import SimpleNamespace

import pytest

os.environ["YUI_APP_TOKEN"] = "test-token"

pytest.importorskip("httpx")
try:
    from fastapi.testclient import TestClient

    import main
except Exception as exc:
    pytest.skip(f"main import unavailable: {exc}", allow_module_level=True)


HEADERS = {"X-Yui-Token": "test-token"}
HEADERS_AUDIO = {**HEADERS, "Content-Type": "audio/wav"}


def test_converse_streams_transcript_audio_and_done(monkeypatch):
    # prefetch_context は実 Firestore/Calendar クライアントに触るため必ずモックする
    # （CI には ADC が無く DefaultCredentialsError になる）。
    monkeypatch.setattr(
        main,
        "prefetch_context",
        lambda _session_id: {"history": [], "today_events": [], "open_tasks": []},
    )
    monkeypatch.setattr(main, "transcribe_audio", lambda _audio: "hello")
    monkeypatch.setattr(main, "stream_reply", lambda *_args: iter(["Hello world."]))
    monkeypatch.setattr(main, "stream_synthesize", lambda _text: iter([b"pcm"]))
    monkeypatch.setattr(main, "enqueue_finalize_turn", lambda *_args, **_kwargs: True)

    client = TestClient(main.app)
    response = client.post("/converse?session_id=session", content=b"audio", headers=HEADERS_AUDIO)
    events = [json.loads(line) for line in response.text.splitlines()]

    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert [event["type"] for event in events] == ["transcript", "pcm", "done"]
    assert events[1] == {
        "type": "pcm",
        "rate": 24000,
        "data": "cGNt",
        "text": "Hello world.",
    }
    assert events[-1]["reply"] == "Hello world."


def test_converse_passes_prefetched_context_to_stream_reply(monkeypatch):
    context = {"history": [], "today_events": [], "open_tasks": []}
    received = []
    monkeypatch.setattr(main, "prefetch_context", lambda _session_id: context)
    monkeypatch.setattr(main, "transcribe_audio", lambda _audio: "hello")
    monkeypatch.setattr(
        main,
        "stream_reply",
        lambda _session_id, _user_text, supplied_context: received.append(supplied_context)
        or iter(["Hello."]),
    )
    monkeypatch.setattr(main, "stream_synthesize", lambda _text: iter([b"pcm"]))
    monkeypatch.setattr(main, "enqueue_finalize_turn", lambda *_args, **_kwargs: True)

    client = TestClient(main.app)
    response = client.post("/converse?session_id=session", content=b"audio", headers=HEADERS_AUDIO)

    assert response.status_code == 200
    assert received == [context]


def test_converse_falls_back_to_mp3_when_streaming_tts_fails(monkeypatch):
    monkeypatch.setattr(
        main,
        "prefetch_context",
        lambda _session_id: {"history": [], "today_events": [], "open_tasks": []},
    )
    monkeypatch.setattr(main, "transcribe_audio", lambda _audio: "hello")
    monkeypatch.setattr(main, "stream_reply", lambda *_args: iter(["Hello world."]))

    def fail_streaming_tts(_text):
        raise RuntimeError("stream unavailable")

    monkeypatch.setattr(main, "stream_synthesize", fail_streaming_tts)
    monkeypatch.setattr(main, "synthesize_speech", lambda _text: b"mp3")
    monkeypatch.setattr(main, "enqueue_finalize_turn", lambda *_args, **_kwargs: True)

    client = TestClient(main.app)
    response = client.post("/converse?session_id=session", content=b"audio", headers=HEADERS_AUDIO)
    events = [json.loads(line) for line in response.text.splitlines()]

    assert [event["type"] for event in events] == ["transcript", "audio", "done"]
    assert events[1]["data"] == "bXAz"


def test_converse_skips_mp3_after_streaming_tts_partial_success(monkeypatch):
    monkeypatch.setattr(
        main,
        "prefetch_context",
        lambda _session_id: {"history": [], "today_events": [], "open_tasks": []},
    )
    monkeypatch.setattr(main, "transcribe_audio", lambda _audio: "hello")
    monkeypatch.setattr(main, "stream_reply", lambda *_args: iter(["Hello world."]))

    def partial_stream(_text):
        yield b"pcm"
        raise RuntimeError("stream interrupted")

    monkeypatch.setattr(main, "stream_synthesize", partial_stream)
    monkeypatch.setattr(main, "synthesize_speech", pytest.fail)
    monkeypatch.setattr(main, "enqueue_finalize_turn", lambda *_args, **_kwargs: True)

    response = TestClient(main.app).post(
        "/converse?session_id=session", content=b"audio", headers=HEADERS_AUDIO
    )
    events = [json.loads(line) for line in response.text.splitlines()]

    assert [event["type"] for event in events] == ["transcript", "pcm", "done"]


def test_converse_emits_empty_for_empty_transcript(monkeypatch):
    monkeypatch.setattr(
        main,
        "prefetch_context",
        lambda _session_id: {"history": [], "today_events": [], "open_tasks": []},
    )
    monkeypatch.setattr(main, "transcribe_audio", lambda _audio: "  ")

    client = TestClient(main.app)
    response = client.post("/converse", content=b"audio", headers=HEADERS_AUDIO)

    assert [json.loads(line) for line in response.text.splitlines()] == [{"type": "empty"}]


def test_finalize_converse_applies_matching_pending_question_answer(monkeypatch):
    answered = []
    monkeypatch.setattr(main, "append_chat_history", lambda *_args: None)
    monkeypatch.setattr(main, "get_recent_titles", lambda: [])
    monkeypatch.setattr(
        main,
        "find_pending_questions",
        lambda: [
            {
                "id": "task-1",
                "title": "Submit report",
                "pending_question": "Who is the reviewer?",
            }
        ],
    )
    monkeypatch.setattr(
        main,
        "extract_dialog_actions",
        lambda *_args: (
            [],
            [],
            [
                type(
                    "Answer",
                    (),
                    {"task_title": "Submit report", "answer": "Maya"},
                )()
            ],
        ),
    )
    monkeypatch.setattr(main, "find_open_tasks", lambda: [])
    monkeypatch.setattr(
        main, "answer_question", lambda doc_id, answer: answered.append((doc_id, answer))
    )

    main.finalize_turn("session", "Maya is the reviewer.", "Thanks.")

    assert answered == [("task-1", "Maya")]


def test_finalize_converse_continues_after_question_answer_failure(monkeypatch):
    answered = []
    errors = []
    monkeypatch.setattr(main, "append_chat_history", lambda *_args: None)
    monkeypatch.setattr(main, "get_recent_titles", lambda: [])
    monkeypatch.setattr(
        main,
        "find_pending_questions",
        lambda: [
            {"id": "first", "title": "First task", "pending_question": "First?"},
            {"id": "second", "title": "Second task", "pending_question": "Second?"},
        ],
    )
    monkeypatch.setattr(
        main,
        "extract_dialog_actions",
        lambda *_args: (
            [],
            [],
            [
                type("Answer", (), {"task_title": "First task", "answer": "one"})(),
                type("Answer", (), {"task_title": "Second task", "answer": "two"})(),
            ],
        ),
    )
    monkeypatch.setattr(main, "find_open_tasks", lambda: [])

    def answer(doc_id, answer):
        if doc_id == "first":
            raise RuntimeError("temporary failure")
        answered.append((doc_id, answer))

    monkeypatch.setattr(main, "answer_question", answer)
    monkeypatch.setattr(main.obs, "error", lambda *args, **kwargs: errors.append(args[0]))

    main.finalize_turn("session", "answers", "Thanks.")

    assert answered == [("second", "two")]
    assert "converse question answer failed" in errors


def test_converse_uses_local_background_finalizer_when_enqueue_fails(monkeypatch):
    finalized = []
    monkeypatch.setattr(
        main,
        "prefetch_context",
        lambda _session_id: {"history": [], "today_events": [], "open_tasks": []},
    )
    monkeypatch.setattr(main, "transcribe_audio", lambda _audio: "hello")
    monkeypatch.setattr(main, "stream_reply", lambda *_args: iter(["Hello."]))
    monkeypatch.setattr(main, "stream_synthesize", lambda _text: iter([b"pcm"]))
    monkeypatch.setattr(main, "enqueue_finalize_turn", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(main, "finalize_turn", lambda *args: finalized.append(args))

    response = TestClient(main.app).post(
        "/converse?session_id=session", content=b"audio", headers=HEADERS_AUDIO
    )

    assert response.status_code == 200
    assert finalized[0][:3] == ("session", "hello", "Hello.")
    assert uuid.UUID(finalized[0][3]).version == 4


def test_converse_does_not_run_local_finalizer_when_enqueue_succeeds(monkeypatch):
    monkeypatch.setattr(
        main,
        "prefetch_context",
        lambda _session_id: {"history": [], "today_events": [], "open_tasks": []},
    )
    monkeypatch.setattr(main, "transcribe_audio", lambda _audio: "hello")
    monkeypatch.setattr(main, "stream_reply", lambda *_args: iter(["Hello."]))
    monkeypatch.setattr(main, "stream_synthesize", lambda _text: iter([b"pcm"]))
    monkeypatch.setattr(main, "enqueue_finalize_turn", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        main,
        "finalize_turn",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not run locally")),
    )

    response = TestClient(main.app).post(
        "/converse?session_id=session", content=b"audio", headers=HEADERS_AUDIO
    )

    assert response.status_code == 200


def test_converse_enqueues_a_uuid4_turn_id(monkeypatch):
    enqueued = []
    monkeypatch.setattr(
        main,
        "prefetch_context",
        lambda _session_id: {"history": [], "today_events": [], "open_tasks": []},
    )
    monkeypatch.setattr(main, "transcribe_audio", lambda _audio: "hello")
    monkeypatch.setattr(main, "stream_reply", lambda *_args: iter(["Hello."]))
    monkeypatch.setattr(main, "stream_synthesize", lambda _text: iter([b"pcm"]))
    monkeypatch.setattr(
        main,
        "enqueue_finalize_turn",
        lambda *args, **kwargs: enqueued.append((args, kwargs)) or True,
    )

    response = TestClient(main.app).post(
        "/converse?session_id=session", content=b"audio", headers=HEADERS_AUDIO
    )

    assert response.status_code == 200
    assert uuid.UUID(enqueued[0][1]["turn_id"]).version == 4


def test_finalize_turn_deduplicates_an_existing_turn(monkeypatch):
    from google.api_core.exceptions import AlreadyExists

    finalized = []

    class Document:
        claimed = False

        def create(self, _data):
            if self.claimed:
                raise AlreadyExists("already finalized")
            self.claimed = True

    document = Document()

    class Collection:
        def document(self, _turn_id):
            return document

    class Database:
        def collection(self, name):
            assert name == "finalized_turns"
            return Collection()

    monkeypatch.setattr(main, "firestore_client", lambda: Database())
    monkeypatch.setattr(main, "append_chat_history", lambda *args: finalized.append(args))
    monkeypatch.setattr(main, "get_recent_titles", lambda: [])
    monkeypatch.setattr(main, "find_pending_questions", lambda: [])
    monkeypatch.setattr(main, "extract_dialog_actions", lambda *_args: ([], [], []))
    monkeypatch.setattr(main, "find_open_tasks", lambda: [])

    main.finalize_turn("session", "hello", "reply", turn_id="turn-123")
    main.finalize_turn("session", "hello", "reply", turn_id="turn-123")

    assert finalized == [("session", "hello", "reply")]


def test_converse_does_not_finalize_an_empty_reply(monkeypatch):
    enqueued = []
    monkeypatch.setattr(
        main,
        "prefetch_context",
        lambda _session_id: {"history": [], "today_events": [], "open_tasks": []},
    )
    monkeypatch.setattr(main, "transcribe_audio", lambda _audio: "hello")
    monkeypatch.setattr(main, "stream_reply", lambda *_args: iter(()))
    monkeypatch.setattr(
        main, "enqueue_finalize_turn", lambda *args, **_kwargs: enqueued.append(args)
    )
    monkeypatch.setattr(
        main,
        "finalize_turn",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not finalize")),
    )

    response = TestClient(main.app).post("/converse", content=b"audio", headers=HEADERS_AUDIO)

    assert response.status_code == 200
    assert enqueued == []


def test_internal_finalize_turn_requires_token_and_runs_finalizer(monkeypatch):
    finalized = []
    monkeypatch.setattr(main, "finalize_turn", lambda *args: finalized.append(args))
    client = TestClient(main.app)

    unauthorized = client.post(
        "/internal/finalize-turn",
        json={"session_id": "session", "user_text": "hello", "reply": "reply"},
    )
    response = client.post(
        "/internal/finalize-turn",
        json={"session_id": "session", "user_text": "hello", "reply": "reply"},
        headers=HEADERS,
    )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert finalized == [("session", "hello", "reply")]


def test_internal_finalize_turn_validates_and_returns_500_on_failure(monkeypatch):
    monkeypatch.setattr(
        main,
        "finalize_turn",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    client = TestClient(main.app, raise_server_exceptions=False)

    too_long = client.post(
        "/internal/finalize-turn",
        json={"session_id": "s" * 129, "user_text": "hello", "reply": "reply"},
        headers=HEADERS,
    )
    failed = client.post(
        "/internal/finalize-turn",
        json={"session_id": "session", "user_text": "hello", "reply": "reply"},
        headers=HEADERS,
    )

    assert too_long.status_code == 422
    assert failed.status_code == 500


def test_converse_degrades_when_context_prefetch_fails(monkeypatch):
    errors = []
    monkeypatch.setattr(
        main,
        "prefetch_context",
        lambda _session_id: (_ for _ in ()).throw(RuntimeError("firestore unavailable")),
    )
    monkeypatch.setattr(main, "transcribe_audio", lambda _audio: "hello")
    monkeypatch.setattr(main, "stream_reply", lambda *_args: iter(["Hello."]))
    monkeypatch.setattr(main, "stream_synthesize", lambda _text: iter([b"pcm"]))
    monkeypatch.setattr(main, "enqueue_finalize_turn", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(main.obs, "error", lambda *args, **_kwargs: errors.append(args))

    response = TestClient(main.app).post(
        "/converse?session_id=session", content=b"audio", headers=HEADERS_AUDIO
    )

    assert response.status_code == 200
    assert [json.loads(line)["type"] for line in response.text.splitlines()] == [
        "transcript",
        "pcm",
        "done",
    ]
    assert errors == []


def test_process_returns_success_when_background_task_persistence_fails(monkeypatch):
    from extraction import ExtractedTask, ExtractionResult

    errors = []
    task = ExtractedTask(title="First", priority=2, reason="test", confidence=0.9)
    monkeypatch.setattr(main, "get_recent_titles", lambda: [])
    monkeypatch.setattr(
        main, "extract_tasks", lambda *_args, **_kwargs: ExtractionResult(tasks=[task])
    )
    monkeypatch.setattr(main, "filter_confident", lambda tasks, _threshold: tasks)
    monkeypatch.setattr(
        main,
        "record_and_resolve",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("firestore unavailable")),
    )
    monkeypatch.setattr(main.obs, "error", lambda *args, **kwargs: errors.append((args, kwargs)))

    response = TestClient(main.app).post(
        "/process", json={"text": "first"}, headers=HEADERS
    )

    assert response.status_code == 200
    assert errors[0][0] == ("record_and_resolve failed",)
    assert errors[0][1]["route"] == "/process"


def test_chat_returns_success_when_background_task_persistence_fails(monkeypatch):
    from extraction import ExtractedTask

    errors = []
    task = ExtractedTask(title="First", priority=2, reason="test", confidence=0.9)
    result = SimpleNamespace(reply="ok", tasks=[task], completed_task_titles=[])
    monkeypatch.setattr(main, "chat_turn", lambda *_args: (result, []))
    monkeypatch.setattr(
        main,
        "record_and_resolve",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("firestore unavailable")),
    )
    monkeypatch.setattr(main.obs, "error", lambda *args, **kwargs: errors.append((args, kwargs)))

    response = TestClient(main.app).post(
        "/chat", json={"session_id": "session", "message": "first"}, headers=HEADERS
    )

    assert response.status_code == 200
    assert errors[0][0] == ("record_and_resolve failed",)
    assert errors[0][1]["route"] == "/chat"


def test_finalize_turn_continues_after_one_task_persistence_failure(monkeypatch):
    persisted = []
    errors = []
    first = SimpleNamespace(title="First", priority=2, reason="test")
    second = SimpleNamespace(title="Second", priority=2, reason="test")
    monkeypatch.setattr(main, "append_chat_history", lambda *_args: None)
    monkeypatch.setattr(main, "get_recent_titles", lambda: [])
    monkeypatch.setattr(main, "find_pending_questions", lambda: [])
    monkeypatch.setattr(main, "find_open_tasks", lambda: [])
    monkeypatch.setattr(main, "extract_dialog_actions", lambda *_args: ([first, second], [], []))
    monkeypatch.setattr(main, "filter_confident", lambda tasks, _threshold: tasks)

    def record(title, *_args, **_kwargs):
        if title == "First":
            raise RuntimeError("first failed")
        persisted.append(title)
        return {"title": title, "priority": 2, "reason": "test"}

    monkeypatch.setattr(main, "record_and_resolve", record)
    monkeypatch.setattr(main, "_upsert_task_background", lambda *_args: None)
    monkeypatch.setattr(main.obs, "error", lambda *args, **kwargs: errors.append((args, kwargs)))

    main.finalize_turn("session", "hello", "reply")

    assert persisted == ["Second"]
    assert errors[0][0] == ("dialog action item failed",)
    assert errors[0][1]["stage"] == "record_and_resolve"


def test_finalize_turn_fetches_open_tasks_once_and_reuses_them(monkeypatch):
    open_tasks = [{"id": "task-1", "title": "Existing task"}]
    fetched = []
    received = []
    extracted = [SimpleNamespace(title="New task", priority=2, reason="test")]
    monkeypatch.setattr(main, "append_chat_history", lambda *_args: None)
    monkeypatch.setattr(main, "get_recent_titles", lambda: [])
    monkeypatch.setattr(main, "find_pending_questions", lambda: [])
    monkeypatch.setattr(
        main, "extract_dialog_actions", lambda *_args: (extracted, [], [])
    )
    monkeypatch.setattr(main, "filter_confident", lambda tasks, _threshold: tasks)
    monkeypatch.setattr(
        main, "find_open_tasks", lambda: fetched.append(True) or open_tasks
    )
    monkeypatch.setattr(
        main,
        "record_and_resolve",
        lambda *_args, open_tasks=None: received.append(open_tasks)
        or {"title": "New task", "priority": 2, "reason": "test"},
    )
    monkeypatch.setattr(main, "_upsert_task_background", lambda *_args: None)

    main.finalize_turn("session", "hello", "reply")

    assert fetched == [True]
    assert received == [open_tasks]
