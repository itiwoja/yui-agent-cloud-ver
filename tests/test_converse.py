import json
import os

import pytest

os.environ["YUI_APP_TOKEN"] = "test-token"

pytest.importorskip("httpx")
try:
    from fastapi.testclient import TestClient

    import main
except Exception as exc:
    pytest.skip(f"main import unavailable: {exc}", allow_module_level=True)


HEADERS = {"X-Yui-Token": "test-token"}


def test_converse_streams_transcript_audio_and_done(monkeypatch):
    monkeypatch.setattr(main, "transcribe_audio", lambda _audio: "hello")
    monkeypatch.setattr(main, "stream_reply", lambda *_args: iter(["Hello world."]))
    monkeypatch.setattr(main, "stream_synthesize", lambda _text: iter([b"pcm"]))
    monkeypatch.setattr(main, "_finalize_converse_background", lambda *_args: None)

    client = TestClient(main.app)
    response = client.post("/converse?session_id=session", content=b"audio", headers=HEADERS)
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
    monkeypatch.setattr(main, "_finalize_converse_background", lambda *_args: None)

    client = TestClient(main.app)
    response = client.post("/converse?session_id=session", content=b"audio", headers=HEADERS)

    assert response.status_code == 200
    assert received == [context]


def test_converse_falls_back_to_mp3_when_streaming_tts_fails(monkeypatch):
    monkeypatch.setattr(main, "transcribe_audio", lambda _audio: "hello")
    monkeypatch.setattr(main, "stream_reply", lambda *_args: iter(["Hello world."]))

    def fail_streaming_tts(_text):
        raise RuntimeError("stream unavailable")

    monkeypatch.setattr(main, "stream_synthesize", fail_streaming_tts)
    monkeypatch.setattr(main, "synthesize_speech", lambda _text: b"mp3")
    monkeypatch.setattr(main, "_finalize_converse_background", lambda *_args: None)

    client = TestClient(main.app)
    response = client.post("/converse?session_id=session", content=b"audio", headers=HEADERS)
    events = [json.loads(line) for line in response.text.splitlines()]

    assert [event["type"] for event in events] == ["transcript", "audio", "done"]
    assert events[1]["data"] == "bXAz"


def test_converse_emits_empty_for_empty_transcript(monkeypatch):
    monkeypatch.setattr(main, "transcribe_audio", lambda _audio: "  ")

    client = TestClient(main.app)
    response = client.post("/converse", content=b"audio", headers=HEADERS)

    assert [json.loads(line) for line in response.text.splitlines()] == [{"type": "empty"}]
