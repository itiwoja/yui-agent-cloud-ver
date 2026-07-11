"""重複テキスト判定（純ロジック・外部依存なし）。

エージェントが「一度した質問」を覚えず毎回同じことを聞き返す退行を防ぐために使う。
大文字小文字・空白のゆらぎを吸収した完全一致で重複を見る。
"""


def normalize_text(text: str) -> str:
    return "".join((text or "").lower().split())


def is_duplicate(text: str, seen: list[str]) -> bool:
    """text が seen のいずれかと（正規化後）同一なら True。空文字は常に False。"""
    normalized = normalize_text(text)
    if not normalized:
        return False
    return any(normalize_text(s) == normalized for s in seen)
