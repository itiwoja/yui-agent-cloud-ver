"""Google Tasksへのアクセスを一度だけ認可し、refresh tokenをSecret Managerに保存するセットアップスクリプト。

このプロジェクトを所有するGoogleアカウント本人がブラウザで一度だけ「許可」をクリックする必要がある。
ローカルでのみ実行する（Cloud Run上では実行しない）。

使い方:
    python scripts/setup_google_tasks_auth.py
"""
import os
import subprocess
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

PROJECT_ID = "yui-agent-2026"
SCOPES = ["https://www.googleapis.com/auth/tasks"]
GCLOUD = (
    r"C:\Users\1kkim\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
)

# Windows上のgcloudはstdoutが非コンソールの場合、システムのANSIコードページ(cp932等)に
# フォールバックし、自身のバナー等に含まれるBOM文字でクラッシュすることがあるため明示的にUTF-8を強制する。
_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def _read_secret(name: str) -> str:
    result = subprocess.run(
        [GCLOUD, "secrets", "versions", "access", "latest", "--secret", name, "--project", PROJECT_ID],
        capture_output=True, text=True, check=True, encoding="utf-8", env=_ENV,
    )
    return result.stdout.strip().lstrip("﻿")


def _store_refresh_token(token: str) -> None:
    exists = subprocess.run(
        [GCLOUD, "secrets", "describe", "google-tasks-refresh-token", "--project", PROJECT_ID],
        capture_output=True, text=True, env=_ENV,
    ).returncode == 0

    args = [GCLOUD, "secrets"]
    if exists:
        args += ["versions", "add", "google-tasks-refresh-token", "--project", PROJECT_ID, "--data-file=-"]
    else:
        args += ["create", "google-tasks-refresh-token", "--project", PROJECT_ID,
                  "--data-file=-", "--replication-policy=automatic"]

    subprocess.run(args, input=token, text=True, check=True, encoding="utf-8", env=_ENV)


def main() -> None:
    client_id = _read_secret("google-oauth-client-id")
    client_secret = _read_secret("google-oauth-client-secret")

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(
        port=0, open_browser=False,
        authorization_prompt_message="AUTH_URL_START{url}AUTH_URL_END",
    )

    if not creds.refresh_token:
        print("refresh_tokenが取得できませんでした。既に認可済みの場合は "
              "Googleアカウントの「サードパーティ アプリのアクセス権」から一度アクセスを取り消してから再実行してください。",
              file=sys.stderr)
        sys.exit(1)

    _store_refresh_token(creds.refresh_token)
    print("refresh tokenをSecret Managerに保存しました。")


if __name__ == "__main__":
    main()
