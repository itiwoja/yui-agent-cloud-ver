"""matching.py — タイトル正規化と突合の純ロジックテスト（google依存なし）。"""
from matching import normalize_title, titles_match


def test_normalize_strips_priority_label_and_whitespace():
    assert normalize_title("🟡 会議資料作成") == "会議資料作成"
    assert normalize_title("  Web デザインの課題  ") == "webデザインの課題"


def test_normalize_empty_and_none_like():
    assert normalize_title("") == ""
    assert normalize_title("   ") == ""


def test_exact_match_across_label_prefix():
    # upsert は "{label} {title}" で保存するので、ラベル有無を跨いで一致すべき
    assert titles_match("🔴 請求書を出す", "請求書を出す") is True


def test_substring_is_not_a_match_regression():
    # 以前の endswith / 部分一致バグ: 別タスクを誤マッチさせていた
    assert titles_match("🟡 会議資料作成", "資料作成") is False
    assert titles_match("資料作成", "会議資料作成") is False


def test_empty_never_matches():
    assert titles_match("", "") is False
    assert titles_match("", "何か") is False


def test_case_and_space_insensitive():
    assert titles_match("Slack の返信", "slackの返信") is True
