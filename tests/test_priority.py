"""priority.py — 優先度昇格の純ロジックテスト。"""
from priority import MAX_PRIORITY, promote


def test_promote_increments_by_step():
    assert promote(1) == 2
    assert promote(3, step=1) == 4


def test_promote_saturates_at_max():
    assert promote(5) == 5
    assert promote(4) == 5
    assert promote(5, step=3) == 5


def test_promote_respects_lower_ceiling():
    # システム自律昇格は ceiling で頭打ちにできる（🔴 を人間の緊急に残す）
    assert promote(3, ceiling=4) == 4
    assert promote(4, ceiling=4) == 4  # 据え置き（全部🔴にしない）
    assert promote(2, step=1, ceiling=4) == 3


def test_promote_at_or_above_ceiling_is_unchanged():
    assert promote(5, ceiling=4) == 5  # 既に上ならユーザー由来の高優先度を潰さない


def test_max_priority_constant():
    assert MAX_PRIORITY == 5
