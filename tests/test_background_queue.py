import json

import background_queue


class FakeClient:
    def __init__(self, error=None):
        self.requests = []
        self.error = error

    def queue_path(self, project, location, queue):
        return f"projects/{project}/locations/{location}/queues/{queue}"

    def create_task(self, request):
        if self.error:
            raise self.error
        self.requests.append(request)


def test_enqueue_finalize_turn_returns_false_without_service_url(monkeypatch):
    monkeypatch.delenv("YUI_SERVICE_URL", raising=False)
    monkeypatch.setattr(
        background_queue.tasks_v2,
        "CloudTasksClient",
        lambda: (_ for _ in ()).throw(AssertionError("client must not be created")),
    )

    assert background_queue.enqueue_finalize_turn("session", "hello", "reply") is False


def test_enqueue_finalize_turn_creates_authenticated_http_task(monkeypatch):
    client = FakeClient()
    monkeypatch.setenv("YUI_SERVICE_URL", "https://service.example/")
    monkeypatch.setenv("YUI_APP_TOKEN", "task-token")
    monkeypatch.setenv("YUI_TASKS_QUEUE", "queue")
    monkeypatch.setenv("YUI_TASKS_LOCATION", "location")
    monkeypatch.setattr(background_queue, "_tasks_client", None)
    monkeypatch.setattr(background_queue.tasks_v2, "CloudTasksClient", lambda: client)

    assert (
        background_queue.enqueue_finalize_turn(
            "session", "hello", "reply", request_id="request-123"
        )
        is True
    )

    request = client.requests[0]
    http_request = request["task"].http_request
    assert request["parent"].endswith("/locations/location/queues/queue")
    assert http_request.url == "https://service.example/internal/finalize-turn"
    assert http_request.headers["X-Yui-Token"] == "task-token"
    assert http_request.headers["X-Request-Id"] == "request-123"
    assert http_request.headers["Content-Type"] == "application/json"
    assert json.loads(http_request.body) == {
        "session_id": "session",
        "user_text": "hello",
        "reply": "reply",
    }


def test_enqueue_finalize_turn_logs_and_returns_false_on_failure(monkeypatch):
    warnings = []
    monkeypatch.setenv("YUI_SERVICE_URL", "https://service.example")
    monkeypatch.setenv("YUI_APP_TOKEN", "task-token")
    monkeypatch.setattr(background_queue, "_tasks_client", None)
    monkeypatch.setattr(
        background_queue.tasks_v2,
        "CloudTasksClient",
        lambda: FakeClient(RuntimeError("permission denied")),
    )
    monkeypatch.setattr(
        background_queue.obs, "warning", lambda *args, **kwargs: warnings.append((args, kwargs))
    )

    assert background_queue.enqueue_finalize_turn("session", "hello", "reply") is False
    assert warnings[0][1]["api"] == "cloud_tasks"


def test_enqueue_finalize_turn_logs_client_creation_failure(monkeypatch):
    warnings = []
    monkeypatch.setenv("YUI_SERVICE_URL", "https://service.example")
    monkeypatch.setenv("YUI_APP_TOKEN", "task-token")
    monkeypatch.setattr(background_queue, "_tasks_client", None)
    monkeypatch.setattr(
        background_queue.tasks_v2,
        "CloudTasksClient",
        lambda: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    monkeypatch.setattr(
        background_queue.obs, "warning", lambda *args, **kwargs: warnings.append((args, kwargs))
    )

    assert background_queue.enqueue_finalize_turn("session", "hello", "reply") is False
    assert warnings[0][1]["api"] == "cloud_tasks"


def test_enqueue_finalize_turn_logs_missing_service_url_as_misconfiguration(monkeypatch):
    errors = []
    monkeypatch.delenv("YUI_SERVICE_URL", raising=False)
    monkeypatch.setattr(
        background_queue.obs, "error", lambda *args, **kwargs: errors.append((args, kwargs))
    )

    assert background_queue.enqueue_finalize_turn("session", "hello", "reply") is False
    assert errors[0][0] == ("cloud tasks misconfigured",)
    assert errors[0][1] == {"missing": "YUI_SERVICE_URL"}


def test_enqueue_finalize_turn_logs_missing_app_token_as_misconfiguration(monkeypatch):
    errors = []
    monkeypatch.setenv("YUI_SERVICE_URL", "https://service.example")
    monkeypatch.delenv("YUI_APP_TOKEN", raising=False)
    monkeypatch.setattr(
        background_queue.obs, "error", lambda *args, **kwargs: errors.append((args, kwargs))
    )

    assert background_queue.enqueue_finalize_turn("session", "hello", "reply") is False
    assert errors[0][0] == ("cloud tasks misconfigured",)
    assert errors[0][1] == {"missing": "YUI_APP_TOKEN"}
