"""dedup.py — 重複質問判定の純ロジックテスト。"""
from dedup import is_duplicate, normalize_text


def test_normalize_collapses_case_and_space():
    assert normalize_text("  いつ まで ？ ") == "いつまで？"
    assert normalize_text("HELLO world") == "helloworld"


def test_duplicate_detected_across_whitespace():
    seen = ["締め切りはいつ？"]
    assert is_duplicate("締め切りは いつ ？", seen) is True


def test_non_duplicate():
    seen = ["締め切りはいつ？"]
    assert is_duplicate("予算はいくら？", seen) is False


def test_empty_is_never_duplicate():
    assert is_duplicate("", ["何か"]) is False
    assert is_duplicate("  ", ["  "]) is False


def test_empty_seen_list():
    assert is_duplicate("質問", []) is False
