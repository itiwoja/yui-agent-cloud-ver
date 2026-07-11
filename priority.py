"""優先度の昇格ロジック（純ロジック・外部依存なし）。

優先度は 1（低）〜5（緊急）。再言及や滞留で1段ずつ上がり、上限で飽和する。

自律レビュー（autonomous_review）はユーザーの指示なしに優先度を上げるので、
上限を `ceiling` で絞れるようにしてある。既定は MAX（従来挙動）だが、
`SYSTEM_ESCALATION_CEILING` を下げれば「システムが勝手に全部を最上位(🔴)に
塗る」暴走を防げる（🔴 を人間が明示した緊急にだけ残す運用）。
"""

MIN_PRIORITY = 1
MAX_PRIORITY = 5


def promote(current: int, step: int = 1, ceiling: int = MAX_PRIORITY) -> int:
    """優先度を step だけ上げる。ceiling で頭打ち。既に ceiling 以上なら据え置き。"""
    if current >= ceiling:
        return current
    return min(ceiling, current + step)
