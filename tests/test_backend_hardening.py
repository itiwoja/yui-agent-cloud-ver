"""Regression tests for the backend hardening work in brief 16."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import auth
import embeddings
import rate_limit


def _request(path: str = "/test", headers: list[tuple[bytes, bytes]] | None = None):
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": headers or [],
            "query_string": b"",
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )


def test_embed_text_uses_retry_wrapper(monkeypatch):
    calls = []

    class Models:
        def embed_content(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                embeddings=[SimpleNamespace(values=[0.1, 0.2])]
            )

    monkeypatch.setattr(
        embeddings, "call_with_retry", lambda operation: operation()
    )
    monkeypatch.setattr(
        embeddings.clients, "gemini_client", lambda: SimpleNamespace(models=Models())
    )

    assert embeddings.embed_text("hello") == [0.1, 0.2]
    assert calls[0]["model"] == embeddings.EMBEDDING_MODEL


def test_cosine_similarity_warns_for_dimension_mismatch(monkeypatch):
    warnings = []
    monkeypatch.setattr(
        embeddings.obs, "warning", lambda *args, **kwargs: warnings.append((args, kwargs))
    )

    assert embeddings.cosine_similarity([1.0], [1.0, 0.0]) == 0.0
    assert warnings == [
        (("embedding dimension mismatch",), {"len_a": 1, "len_b": 2})
    ]


def test_auth_rejection_logs_path_without_token(monkeypatch):
    warnings = []
    monkeypatch.setattr(auth, "_expected_token", lambda: "expected")
    monkeypatch.setattr(
        auth.obs, "warning", lambda *args, **kwargs: warnings.append((args, kwargs))
    )

    with pytest.raises(HTTPException, match="unauthorized"):
        auth.require_app_token(_request("/protected"))

    assert warnings == [
        (("auth rejected",), {"path": "/protected", "has_token": False})
    ]


def test_rate_limit_rejection_logs_client_key(monkeypatch):
    warnings = []
    rate_limit.clear_rate_limits()
    monkeypatch.setattr(rate_limit, "_configured_limit", lambda: 1)
    monkeypatch.setattr(rate_limit, "_configured_window", lambda: 60.0)
    monkeypatch.setattr(
        rate_limit.obs, "warning", lambda *args, **kwargs: warnings.append((args, kwargs))
    )
    request = _request(headers=[(b"x-yui-token", b"abcdefgh-token")])

    rate_limit.require_rate_limit(request)
    with pytest.raises(HTTPException, match="rate limit exceeded"):
        rate_limit.require_rate_limit(request)

    assert warnings == [
        (("rate limited",), {"client_key": "token:abcdefgh"})
    ]
