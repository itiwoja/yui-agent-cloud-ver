"""auth.py — トークン検証の純ロジックテスト。"""
from auth import is_authorized


def test_open_when_no_token_configured():
    # 開発時: YUI_APP_TOKEN 未設定なら素通し（従来挙動を壊さない）
    assert is_authorized("", "anything") is True
    assert is_authorized("", "") is True
    assert is_authorized("   ", "x") is True  # 空白のみも未設定扱い


def test_requires_match_when_configured():
    assert is_authorized("s3cret", "s3cret") is True
    assert is_authorized("s3cret", "wrong") is False


def test_missing_token_is_rejected_when_configured():
    assert is_authorized("s3cret", "") is False
    assert is_authorized("s3cret", "   ") is False


def test_whitespace_is_trimmed_both_sides():
    assert is_authorized("s3cret", "  s3cret  ") is True
