"""プロセス単位のインメモリ固定窓レート制限。"""
import os
import threading
import time

from fastapi import HTTPException, Request

import obs

DEFAULT_LIMIT = 30
DEFAULT_WINDOW_SECONDS = 60.0
_histories: dict[str, list[float]] = {}
_lock = threading.Lock()


def is_allowed(history: list[float], now: float, limit: int, window: float) -> bool:
    """固定窓内の履歴を整理し、許可時だけ現在のリクエストを記録する。"""
    history[:] = [timestamp for timestamp in history if now - timestamp < window]
    if len(history) >= limit:
        return False
    history.append(now)
    return True


def _configured_limit() -> int:
    try:
        value = int(os.environ.get("YUI_RATE_LIMIT", str(DEFAULT_LIMIT)))
    except ValueError:
        return DEFAULT_LIMIT
    return value if value > 0 else DEFAULT_LIMIT


def _configured_window() -> float:
    try:
        value = float(
            os.environ.get("YUI_RATE_WINDOW_SECONDS", str(DEFAULT_WINDOW_SECONDS))
        )
    except ValueError:
        return DEFAULT_WINDOW_SECONDS
    return value if value > 0 else DEFAULT_WINDOW_SECONDS


def _client_key(request: Request) -> str:
    token = request.headers.get("x-yui-token", "") or request.query_params.get(
        "token", ""
    )
    if token:
        return f"token:{token[:8]}"
    client_host = request.client.host if request.client else "unknown"
    return f"ip:{client_host}"


def require_rate_limit(request: Request) -> None:
    """クライアントごとに短時間の過剰な API 呼び出しを拒否する。

    Cloud Run の複数インスタンス間では状態を共有しないため、これは高コスト
    エンドポイントを保護する補助的な制限である。
    """
    with _lock:
        client_key = _client_key(request)
        history = _histories.setdefault(client_key, [])
        allowed = is_allowed(
            history,
            time.monotonic(),
            _configured_limit(),
            _configured_window(),
        )
    if not allowed:
        obs.warning("rate limited", client_key=client_key)
        raise HTTPException(status_code=429, detail="rate limit exceeded")


def clear_rate_limits() -> None:
    """テスト用に記録済みのリクエスト履歴を消去する。"""
    with _lock:
        _histories.clear()
