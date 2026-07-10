"""Google Cloud Text-to-Speechでゆいの返答を音声合成する。"""
import os

from google.cloud import texttospeech

VOICE_NAME = os.environ.get("YUI_VOICE_NAME", "ja-JP-Chirp3-HD-Leda")
LANGUAGE_CODE = "ja-JP"

_client = None


def _get_client() -> texttospeech.TextToSpeechClient:
    global _client
    if _client is None:
        _client = texttospeech.TextToSpeechClient()
    return _client


def synthesize_speech(text: str) -> bytes:
    client = _get_client()
    response = client.synthesize_speech(
        input=texttospeech.SynthesisInput(text=text),
        voice=texttospeech.VoiceSelectionParams(
            language_code=LANGUAGE_CODE,
            name=VOICE_NAME,
        ),
        audio_config=texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
        ),
    )
    return response.audio_content
