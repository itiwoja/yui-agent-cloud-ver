"""obs.py — 構造化ログのテスト（stdout をキャプチャして JSON を検証）。"""
import json

from obs import error, info


def test_info_emits_json_with_severity(capsys):
    info("hello", route="/chat")
    line = capsys.readouterr().out.strip()
    entry = json.loads(line)
    assert entry["severity"] == "INFO"
    assert entry["message"] == "hello"
    assert entry["route"] == "/chat"


def test_error_severity_and_japanese_is_not_escaped(capsys):
    error("失敗した", detail="タイムアウト")
    entry = json.loads(capsys.readouterr().out.strip())
    assert entry["severity"] == "ERROR"
    assert entry["message"] == "失敗した"
    assert entry["detail"] == "タイムアウト"
