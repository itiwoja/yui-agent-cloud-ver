"""エンドポイント保護。

未認証の公開URL（`--allow-unauthenticated`）のままだと、URLを知った第三者が
`/chat` `/process` `/autonomous-review` を叩いて**本人のGoogle Tasksにタスクを
捏造**したり、**Geminiを無制限に消費（コストDoS）**できてしまう。これを塞ぐ。

方式（デモの「URLを開いて話す」を壊さない現実解）:
- `YUI_APP_TOKEN` が設定されていれば、保護対象エンドポイントは
  `X-Yui-Token` ヘッダ（WS/リンク用に `?token=` も可）に一致を要求する。
- Cloud Scheduler は同じ `X-Yui-Token` ヘッダを付けて `/autonomous-review` を叩く。
- **未設定なら素通し**（ローカル開発）。本番デプロイでは Secret Manager 経由で
  必須にする（cicd-setup.md 参照）。トークン未設定＝fail open なので、
  「本番でトークンを入れ忘れる」と穴が残る点に注意（デプロイ手順で担保する）。
"""
import os
import secrets

from fastapi import HTTPException, Request

APP_TOKEN_ENV = "YUI_APP_TOKEN"


def is_authorized(expected: str, provided: str) -> bool:
    """トークン検証の純ロジック。expected 未設定なら常に許可（dev）。"""
    expected = (expected or "").strip()
    if not expected:
        return True
    provided = (provided or "").strip()
    if not provided:
        return False
    return secrets.compare_digest(provided, expected)


def _expected_token() -> str:
    return os.environ.get(APP_TOKEN_ENV, "")


def require_app_token(request: Request) -> None:
    """FastAPI 依存関数。保護対象ルートに Depends で挿す。"""
    provided = request.headers.get("x-yui-token", "") or request.query_params.get("token", "")
    if not is_authorized(_expected_token(), provided):
        raise HTTPException(status_code=401, detail="unauthorized")
