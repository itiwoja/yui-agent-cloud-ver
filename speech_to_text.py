"""Google Cloud Speech-to-Text (Chirp 2) で音声メモをまとめて文字起こしする。

逐次(ストリーミング)ではなく、録音済みの音声全体を一括で認識する。
文脈を保ったまま解釈させることで、意図通りの文字起こしを狙う。

Chirp 2 は Google Cloud Speech-to-Text の中で最も精度の高いユニバーサル音声認識モデル。
旧 v1 API の `latest_long` (+enhanced) より高精度だが、v1 の speech_contexts
(フレーズ強調)には対応していないため、キーワードのブーストは行わない。
"""
import os

from google.cloud import speech_v2
from google.cloud.speech_v2.types import cloud_speech

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "yui-agent-2026")
# Chirp 2 はリージョン限定なので、リージョン指定不要な global エンドポイントを既定にする。
LOCATION = os.environ.get("GOOGLE_CLOUD_SPEECH_LOCATION", "global")
MODEL = "chirp_2"

_client = None


def _get_client() -> speech_v2.SpeechClient:
    global _client
    if _client is None:
        _client = speech_v2.SpeechClient()
    return _client


def transcribe_audio(audio_bytes: bytes) -> str:
    client = _get_client()
    config = cloud_speech.RecognitionConfig(
        auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
        language_codes=["ja-JP"],
        model=MODEL,
        features=cloud_speech.RecognitionFeatures(enable_automatic_punctuation=True),
    )
    request = cloud_speech.RecognizeRequest(
        recognizer=f"projects/{PROJECT_ID}/locations/{LOCATION}/recognizers/_",
        config=config,
        content=audio_bytes,
    )
    response = client.recognize(request=request)
    return "".join(
        result.alternatives[0].transcript
        for result in response.results
        if result.alternatives
    )
