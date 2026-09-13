"""纠错暂存（胶纠）：本地 JSONL 追加，供管理员导出回灌。"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path


def save_correction(data_dir: Path, *, scope: str, sender: str, text: str) -> Path:
    """追加一条纠错建议，返回文件路径。任何字段缺失也尽量落盘。"""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "corrections.jsonl"
    record = {
        "time": _dt.datetime.now().astimezone().replace(microsecond=0).isoformat(),
        "scope": str(scope or ""),
        "sender": str(sender or ""),
        "text": str(text or "").strip()[:500],
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path
