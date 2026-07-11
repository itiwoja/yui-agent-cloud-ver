"""Google Tasksへタスクを登録する。認証情報はSecret Managerに保存したrefresh tokenを使う。"""
import os
import threading

from google.cloud import secretmanager
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from matching import titles_match

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


def _find_matching_task(service, tasklist_id: str, title: str) -> dict | None:
    """未完了タスクを一度だけ取得し、タイトル一致するものを返す。"""
    existing = service.tasks().list(tasklist=tasklist_id, showCompleted=False).execute()
    for task in existing.get("items", []):
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
        return updated["id"]

    created = service.tasks().insert(
        tasklist=tasklist_id,
        body={"title": task_title, "notes": reason},
    ).execute()
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
        return completed["id"]
    return None


def delete_google_task(title: str) -> str | None:
    """Yuiリスト内の同名タスクをGoogle Tasksから削除する（誤タスクの取り消し）。"""
    service = _service()
    tasklist_id = _get_or_create_tasklist_id(service)
    existing = _find_matching_task(service, tasklist_id, title)
    if existing:
        service.tasks().delete(tasklist=tasklist_id, task=existing["id"]).execute()
        return existing["id"]
    return None
