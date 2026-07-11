from types import SimpleNamespace

import tts


def test_stream_synthesize_sends_config_then_input_and_yields_pcm(monkeypatch):
    class Client:
        requests = None

        def streaming_synthesize(self, requests):
            self.requests = list(requests)
            return iter(
                [
                    SimpleNamespace(audio_content=b"first"),
                    SimpleNamespace(audio_content=b""),
                    SimpleNamespace(audio_content=b"second"),
                ]
            )

    client = Client()
    monkeypatch.setattr(tts, "_get_client", lambda: client)

    assert list(tts.stream_synthesize("こんにちは。")) == [b"first", b"second"]

    config_request, input_request = client.requests
    assert config_request.streaming_config.voice.name == tts.VOICE_NAME
    assert config_request.streaming_config.voice.language_code == tts.LANGUAGE_CODE
    assert (
        config_request.streaming_config.streaming_audio_config.audio_encoding
        == tts.texttospeech.AudioEncoding.PCM
    )
    assert config_request.streaming_config.streaming_audio_config.sample_rate_hertz == 24000
    assert input_request.input.text == "こんにちは。"
