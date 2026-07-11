"""共有 Google Cloud クライアントのキャッシュを検証する。"""
import clients


def test_gemini_client_is_a_singleton(monkeypatch):
    instance = object()
    monkeypatch.setattr(clients.genai, "Client", lambda **_kwargs: instance)
    monkeypatch.setattr(clients, "_gemini_client", None)

    assert clients.gemini_client() is instance
    assert clients.gemini_client() is instance


def test_firestore_client_is_a_singleton(monkeypatch):
    instance = object()
    monkeypatch.setattr(clients.firestore, "Client", lambda **_kwargs: instance)
    monkeypatch.setattr(clients, "_firestore_client", None)

    assert clients.firestore_client() is instance
    assert clients.firestore_client() is instance
