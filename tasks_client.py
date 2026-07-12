"""Google Tasksへタスクを登録する。認証情報はSecret Managerに保存したrefresh tokenを使う。"""
import os
import threading
import time

from google.cloud import secretmanager
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from matching import normalize_title, titles_match

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "yui-agent-2026")
TASKLIST_TITLE = "Yui"
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/calendar.readonly",
]

PRIORITY_LABEL = {5: "🔴", 4: "🟠", 3: "🟡", 2: "🟢", 1: "⚪"}

_credentials = None
_credentials_lock = threading.Lock()
_service_client = None
_service_lock = threading.Lock()
_tasklist_id = None
_task_cache: dict[tuple[int, str], tuple[float, dict[str, dict]]] = {}
_task_cache_lock = threading.Lock()


def _tasks_cache_ttl() -> float:
    try:
        return max(0.0, float(os.environ.get("YUI_TASKS_CACHE_TTL", "60")))
    except ValueError:
        return 60.0


def _access_secret(name: str) -> str:
    client = secretmanager.SecretManagerServiceClient()
    resource = f"projects/{PROJECT_ID}/secrets/{name}/versions/latest"
    response = client.access_secret_version(name=resource)
    return response.payload.data.decode("utf-8").strip().lstrip("﻿")


def get_credentials() -> Credentials:
    global _credentials
    if _credentials is None:
        with _credentials_lock:
            if _credentials is None:
                _credentials = Credentials(
                    token=None,
                    refresh_token=_access_secret("google-tasks-refresh-token"),
                    client_id=_access_secret("google-oauth-client-id"),
                    client_secret=_access_secret("google-oauth-client-secret"),
                    token_uri="https://oauth2.googleapis.com/token",
                    scopes=GOOGLE_SCOPES,
                )
    return _credentials


def _service():
    global _service_client
    if _service_client is None:
        with _service_lock:
            if _service_client is None:
                _service_client = build(
                    "tasks", "v1", credentials=get_credentials(), cache_discovery=False
                )
    return _service_client


def _get_or_create_tasklist_id(service) -> str:
    global _tasklist_id
    if _tasklist_id:
        return _tasklist_id

    result = service.tasklists().list().execute()
    for tasklist in result.get("items", []):
        if tasklist["title"] == TASKLIST_TITLE:
            _tasklist_id = tasklist["id"]
            return _tasklist_id

    created = service.tasklists().insert(body={"title": TASKLIST_TITLE}).execute()
    _tasklist_id = created["id"]
    return _tasklist_id


def _cached_tasks(service, tasklist_id: str) -> dict[str, dict]:
    """Return the current tasklist index, fetching it once per TTL window."""
    key = (id(service), tasklist_id)
    now = time.monotonic()
    with _task_cache_lock:
        cached = _task_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]

        result = service.tasks().list(
            tasklist=tasklist_id, showCompleted=False
        ).execute()
        indexed: dict[str, dict] = {}
        for task in result.get("items", []):
            normalized = normalize_title(task.get("title", ""))
            if normalized:
                indexed.setdefault(normalized, task)
        _task_cache[key] = (now + _tasks_cache_ttl(), indexed)
        return indexed


def _update_cached_task(service, tasklist_id: str, task: dict) -> None:
    key = (id(service), tasklist_id)
    with _task_cache_lock:
        cached = _task_cache.get(key)
        if not cached or cached[0] <= time.monotonic():
            return
        index = cached[1]
        for normalized, cached_task in list(index.items()):
            if cached_task.get("id") == task.get("id"):
                del index[normalized]
        normalized = normalize_title(task.get("title", ""))
        if normalized:
            index[normalized] = task


def _remove_cached_task(service, tasklist_id: str, task_id: str) -> None:
    key = (id(service), tasklist_id)
    with _task_cache_lock:
        cached = _task_cache.get(key)
        if not cached or cached[0] <= time.monotonic():
            return
        for normalized, task in list(cached[1].items()):
            if task.get("id") == task_id:
                del cached[1][normalized]


def _find_matching_task(service, tasklist_id: str, title: str) -> dict | None:
    """未完了タスクを一度だけ取得し、タイトル一致するものを返す。"""
    for task in _cached_tasks(service, tasklist_id).values():
        if titles_match(task.get("title", ""), title):
            return task
    return None


def upsert_task(title: str, priority: int, reason: str) -> str:
    """タイトルが一致する既存タスクがあれば内容を更新し、無ければ新規作成する。タスクIDを返す。"""
    service = _service()
    tasklist_id = _get_or_create_tasklist_id(service)

    label = PRIORITY_LABEL.get(priority, "")
    task_title = f"{label} {title}".strip()

    existing = _find_matching_task(service, tasklist_id, title)
    if existing:
        updated = service.tasks().patch(
            tasklist=tasklist_id, task=existing["id"],
            body={"title": task_title, "notes": reason},
        ).execute()
        _update_cached_task(
            service,
            tasklist_id,
            {**existing, **updated, "title": task_title, "notes": reason},
        )
        return updated["id"]

    created = service.tasks().insert(
        tasklist=tasklist_id,
        body={"title": task_title, "notes": reason},
    ).execute()
    _update_cached_task(
        service, tasklist_id, {**created, "title": task_title, "notes": reason}
    )
    return created["id"]


def complete_google_task(title: str) -> str | None:
    """Yuiリスト内の同名タスクをGoogle Tasks上でも完了にする。"""
    service = _service()
    tasklist_id = _get_or_create_tasklist_id(service)
    existing = _find_matching_task(service, tasklist_id, title)
    if existing:
        completed = service.tasks().patch(
            tasklist=tasklist_id,
            task=existing["id"],
            body={"status": "completed"},
        ).execute()
        _remove_cached_task(service, tasklist_id, existing["id"])
        return completed["id"]
    return None


def delete_google_task(title: str) -> str | None:
    """Yuiリスト内の同名タスクをGoogle Tasksから削除する（誤タスクの取り消し）。"""
    service = _service()
    tasklist_id = _get_or_create_tasklist_id(service)
    existing = _find_matching_task(service, tasklist_id, title)
    if existing:
        service.tasks().delete(tasklist=tasklist_id, task=existing["id"]).execute()
        _remove_cached_task(service, tasklist_id, existing["id"])
        return existing["id"]
    return None
