"""Google Cloud Text-to-Speechでゆいの返答を音声合成する。"""
import os
from collections.abc import Iterator

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


def stream_synthesize(text: str) -> Iterator[bytes]:
    """Yield 24 kHz 16-bit little-endian PCM chunks from Streaming TTS.

    Chirp 3: HD voices require the streaming RPC's configuration request to
    precede every synthesis input request.
    """
    client = _get_client()
    streaming_config = texttospeech.StreamingSynthesizeConfig(
        voice=texttospeech.VoiceSelectionParams(
            language_code=LANGUAGE_CODE,
            name=VOICE_NAME,
        ),
        streaming_audio_config=texttospeech.StreamingAudioConfig(
            audio_encoding=texttospeech.AudioEncoding.PCM,
            sample_rate_hertz=24000,
        ),
    )

    def requests():
        yield texttospeech.StreamingSynthesizeRequest(
            streaming_config=streaming_config
        )
        yield texttospeech.StreamingSynthesizeRequest(
            input=texttospeech.StreamingSynthesisInput(text=text)
        )

    for response in client.streaming_synthesize(requests()):
        if response.audio_content:
            yield response.audio_content
