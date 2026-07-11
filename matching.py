"""タスクタイトルの正規化と突合（純ロジック・外部依存なし）。

Google Tasks 側は優先度ラベル絵文字を prefix に付けて保存する
（例: "🟡 会議資料作成"）。突合する前にラベル・空白・大文字小文字を
落として正規化する。

以前は `stored_title.endswith(logical_title)` で部分一致していたが、これは
「資料作成」と「会議資料作成」のような**別タスクを誤マッチ**させる地雷だった
（endswith("資料作成") がどちらにも真になる）。upsert 側は必ず
`"{label} {title}"` の形で保存するので、正規化後の**完全一致**で過不足なく
突合できる。完了フローは「間違って別タスクをdoneにする」方が実害が大きいので、
誤検出を避ける完全一致に倒す。
"""

PRIORITY_LABELS = ("🔴", "🟠", "🟡", "🟢", "⚪")


def normalize_title(title: str) -> str:
    """優先度ラベル・空白・大文字小文字を落として比較用のキーにする。"""
    if not title:
        return ""
    text = title
    for label in PRIORITY_LABELS:
        text = text.replace(label, "")
    return "".join(text.lower().split())


def titles_match(a: str, b: str) -> bool:
    """2つのタイトルが（正規化後）同一タスクを指すか。空文字は常に不一致。"""
    normalized_a = normalize_title(a)
    normalized_b = normalize_title(b)
    return bool(normalized_a) and normalized_a == normalized_b
