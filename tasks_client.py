"""Google Tasksへタスクを登録する。認証情報はSecret Managerに保存したrefresh tokenを使う。"""
import os

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
_tasklist_id = None


def _access_secret(name: str) -> str:
    client = secretmanager.SecretManagerServiceClient()
    resource = f"projects/{PROJECT_ID}/secrets/{name}/versions/latest"
    response = client.access_secret_version(name=resource)
    return response.payload.data.decode("utf-8").strip().lstrip("﻿")


def get_credentials() -> Credentials:
    global _credentials
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
    return build("tasks", "v1", credentials=get_credentials(), cache_discovery=False)


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


def upsert_task(title: str, priority: int, reason: str) -> str:
    """タイトルが一致する既存タスクがあれば内容を更新し、無ければ新規作成する。タスクIDを返す。"""
    service = _service()
    tasklist_id = _get_or_create_tasklist_id(service)

    label = PRIORITY_LABEL.get(priority, "")
    task_title = f"{label} {title}".strip()

    existing = service.tasks().list(tasklist=tasklist_id, showCompleted=False).execute()
    for task in existing.get("items", []):
        if titles_match(task.get("title", ""), title):
            updated = service.tasks().patch(
                tasklist=tasklist_id, task=task["id"],
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
    existing = service.tasks().list(tasklist=tasklist_id, showCompleted=False).execute()
    for task in existing.get("items", []):
        if titles_match(task.get("title", ""), title):
            completed = service.tasks().patch(
                tasklist=tasklist_id,
                task=task["id"],
                body={"status": "completed"},
            ).execute()
            return completed["id"]
    return None


def delete_google_task(title: str) -> str | None:
    """Yuiリスト内の同名タスクをGoogle Tasksから削除する（誤タスクの取り消し）。"""
    service = _service()
    tasklist_id = _get_or_create_tasklist_id(service)
    existing = service.tasks().list(tasklist=tasklist_id, showCompleted=False).execute()
    for task in existing.get("items", []):
        if titles_match(task.get("title", ""), title):
            service.tasks().delete(tasklist=tasklist_id, task=task["id"]).execute()
            return task["id"]
    return None
