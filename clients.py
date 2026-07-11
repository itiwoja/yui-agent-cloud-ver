"""Google Cloud クライアントと共通設定を共有する。"""
import os
import threading

from google import genai
from google.cloud import firestore

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "yui-agent-2026")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "asia-northeast1")
DEFAULT_MODEL = "gemini-2.5-flash"

_gemini_client: genai.Client | None = None
_firestore_client: firestore.Client | None = None
_gemini_lock = threading.Lock()
_firestore_lock = threading.Lock()


def gemini_client() -> genai.Client:
    """Vertex AI Gemini クライアントをプロセス内で再利用する。"""
    global _gemini_client
    if _gemini_client is None:
        with _gemini_lock:
            if _gemini_client is None:
                _gemini_client = genai.Client(
                    vertexai=True, project=PROJECT_ID, location=LOCATION
                )
    return _gemini_client


def firestore_client() -> firestore.Client:
    """Firestore クライアントをプロセス内で再利用する。"""
    global _firestore_client
    if _firestore_client is None:
        with _firestore_lock:
            if _firestore_client is None:
                _firestore_client = firestore.Client(project=PROJECT_ID)
    return _firestore_client
