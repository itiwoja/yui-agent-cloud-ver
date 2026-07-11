"""構造化ログ（Cloud Logging 用・外部依存なし）。

Cloud Run は stdout を取り込み、JSON に `severity` フィールドがあれば
ログレベルとして解釈する。`print` の垂れ流しではなく severity 付きの
1行JSONで出すことで、Cloud Logging 上で重大度フィルタ・アラートが張れる。
"""
import json
import sys


def log(severity: str, message: str, **fields) -> None:
    entry = {"severity": severity, "message": message}
    entry.update(fields)
    print(json.dumps(entry, ensure_ascii=False), file=sys.stdout, flush=True)


def info(message: str, **fields) -> None:
    log("INFO", message, **fields)


def warning(message: str, **fields) -> None:
    log("WARNING", message, **fields)


def error(message: str, **fields) -> None:
    log("ERROR", message, **fields)
