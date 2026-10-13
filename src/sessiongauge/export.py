"""安全な履歴エクスポート用の独立ヘルパー。"""

import csv
import json
import os
import tempfile
from pathlib import Path

FIELDS = (
    "task_id",
    "task_title",
    "pane_ids",
    "project",
    "profile",
    "profile_id",
    "session_ids",
    "interval_start",
    "interval_end",
    "baseline_timestamp",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
    "estimated_cost_usd",
    "actual_cost_usd",
    "model",
    "model_provenance",
    "mapping_status",
    "data_source",
)


def _safe(value):
    if not isinstance(value, str):
        return value
    clean = "".join(c for c in value if c >= " " and c != "\x7f")
    index = next((i for i, c in enumerate(clean) if c not in " \t\r\n"), len(clean))
    return "'" + clean if index < len(clean) and clean[index] in "=+-@" else clean


def _protected(path, protected_paths):
    if path.is_symlink():
        return True
    target = path.resolve(strict=False)
    for item in protected_paths:
        source = Path(item).expanduser()
        if target == source.resolve(strict=False):
            return True
        if path.exists() and source.exists():
            try:
                if os.path.samefile(path, source):
                    return True
            except OSError:
                pass
    return False


def export_rows(data, path, format="csv", bom=False, force=False, protected_paths=()):
    """出力先を原子的に作成し、既存ファイルはforceなしで上書きしない。"""
    if format not in ("csv", "json"):
        raise ValueError("format must be csv or json")
    path = Path(path).expanduser()
    protected = [
        Path(p)
        for source in protected_paths
        for p in (
            source,
            Path(str(source) + "-wal"),
            Path(str(source) + "-shm"),
            Path(str(source) + "-journal"),
        )
    ]
    if _protected(path, protected):
        raise ValueError("output aliases a protected database or is a symlink")
    if path.exists() and not force:
        raise ValueError("output exists; use --force")
    existed = path.parent.exists()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not existed:
        os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".sessiongauge-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as stream:
            if format == "json":
                json.dump(data, stream, ensure_ascii=False)
            else:
                writer = csv.DictWriter(stream, fieldnames=FIELDS)
                writer.writeheader()
                for item in data:
                    row = {}
                    for key in FIELDS:
                        value = item.get(key)
                        if isinstance(value, (list, tuple)):
                            value = json.dumps([_safe(part) for part in value], ensure_ascii=False)
                        row[key] = _safe(value)
                    writer.writerow(row)
        os.chmod(temporary, 0o600)
        if force:
            if path.is_symlink():
                raise ValueError("output must not be a symlink")
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path
