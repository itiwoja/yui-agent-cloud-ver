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

import obs

APP_TOKEN_ENV = "YUI_APP_TOKEN"


def _clean(value: str) -> str:
    """先頭BOM（﻿）と前後空白を除去する。

    WindowsでSecret Managerに値を入れると、PowerShellのパイプ等でUTF-8 BOMが
    先頭に混入することがある（既存の google-tasks-refresh-token でも lstrip("﻿")
    しているのと同じ罠）。str.strip() はBOMを空白扱いしないため明示的に落とす。
    """
    return (value or "").lstrip("﻿").strip()


def is_authorized(expected: str, provided: str) -> bool:
    """トークン検証の純ロジック。expected 未設定なら常に許可（dev）。"""
    expected = _clean(expected)
    if not expected:
        return True
    provided = _clean(provided)
    if not provided:
        return False
    return secrets.compare_digest(provided, expected)


def _expected_token() -> str:
    return os.environ.get(APP_TOKEN_ENV, "")


def assert_token_configured() -> None:
    """Cloud Run ではアプリケーショントークン未設定の起動を拒否する。

    ローカル開発では従来どおり認証なしで動作させるが、Cloud Run を示す
    ``K_SERVICE`` がある環境で fail-open になることは許可しない。
    """
    if os.environ.get("K_SERVICE") and not _clean(_expected_token()):
        raise RuntimeError(
            "YUI_APP_TOKEN must be configured when running on Cloud Run"
        )


def require_app_token(request: Request) -> None:
    """FastAPI 依存関数。保護対象ルートに Depends で挿す。"""
    provided = request.headers.get("x-yui-token", "") or request.query_params.get("token", "")
    if not is_authorized(_expected_token(), provided):
        obs.warning(
            "auth rejected",
            path=request.url.path,
            has_token=bool(provided),
        )
        raise HTTPException(status_code=401, detail="unauthorized")
