"""Cloud Tasks helpers for durable background work."""
import json
import os
import threading

from google.cloud import tasks_v2

from clients import PROJECT_ID
import obs


_tasks_client: tasks_v2.CloudTasksClient | None = None
_tasks_lock = threading.Lock()


def _client() -> tasks_v2.CloudTasksClient:
    """Return the shared Cloud Tasks client, creating it only when needed."""
    global _tasks_client
    if _tasks_client is None:
        with _tasks_lock:
            if _tasks_client is None:
                _tasks_client = tasks_v2.CloudTasksClient()
    return _tasks_client


def enqueue_finalize_turn(
    session_id: str, user_text: str, reply: str, request_id: str | None = None
) -> bool:
    """Enqueue finalization of a streamed conversation, if Cloud Tasks is enabled."""
    service_url = os.environ.get("YUI_SERVICE_URL", "").rstrip("/")
    if not service_url:
        obs.error("cloud tasks misconfigured", missing="YUI_SERVICE_URL")
        return False
    app_token = os.environ.get("YUI_APP_TOKEN", "")
    if not app_token:
        obs.error("cloud tasks misconfigured", missing="YUI_APP_TOKEN")
        return False

    queue = os.environ.get("YUI_TASKS_QUEUE", "yui-background")
    location = os.environ.get("YUI_TASKS_LOCATION", "asia-northeast1")
    try:
        client = _client()
        parent = client.queue_path(PROJECT_ID, location, queue)
        task = tasks_v2.Task(
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=f"{service_url}/internal/finalize-turn",
                headers={
                    "Content-Type": "application/json",
                    "X-Yui-Token": app_token,
                    **({"X-Request-Id": request_id} if request_id else {}),
                },
                body=json.dumps(
                    {
                        "session_id": session_id,
                        "user_text": user_text,
                        "reply": reply,
                    }
                ).encode("utf-8"),
            )
        )
        client.create_task(request={"parent": parent, "task": task})
        return True
    except Exception as exc:
        obs.warning(
            "enqueue finalize turn failed",
            api="cloud_tasks",
            detail=str(exc),
            exc_type=type(exc).__name__,
        )
        return False
