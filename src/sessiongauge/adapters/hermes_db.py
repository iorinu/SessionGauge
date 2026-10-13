import math
import sqlite3
from pathlib import Path
from urllib.parse import quote

from sessiongauge.domain import Counters, Session

SAFE_COLUMNS = {
    "id",
    "source",
    "model",
    "parent_session_id",
    "started_at",
    "ended_at",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "estimated_cost_usd",
    "actual_cost_usd",
    "title",
    "cwd",
    "git_repo_root",
    "last_activity_at",
    "end_reason",
}
COUNTER_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


class HermesDB:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.connection: sqlite3.Connection | None = None
        self.columns: set[str] = set()
        self.schema_version: int | None = None
        self.user_version: int | None = None
        self.journal_mode: str | None = None
        self.last_query = ""

    def __enter__(self):
        if not self.path.is_file():
            raise FileNotFoundError("Hermes database is unavailable")
        uri = f"file:{quote(str(self.path.resolve()), safe='/')}?mode=ro"
        self.connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA query_only=ON")
        self.columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(sessions)")
        }
        self.user_version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        self.journal_mode = self.connection.execute("PRAGMA journal_mode").fetchone()[0]
        version_table = {
            r["name"] for r in self.connection.execute("PRAGMA table_info(schema_version)")
        }
        version_table |= {
            r["name"] for r in self.connection.execute("PRAGMA table_info(schema_version_table)")
        }
        if version_table:
            try:
                table = "schema_version" if "version" in version_table else "schema_version_table"
                column = "version" if "version" in version_table else "schema_version"
                row = self.connection.execute(
                    f'SELECT "{column}" FROM "{table}" LIMIT 1'
                ).fetchone()
                self.schema_version = row[0] if row else None
            except sqlite3.Error:
                self.schema_version = None
        return self

    def __exit__(self, *_):
        if self.connection:
            self.connection.close()
            self.connection = None

    def sessions(self) -> list[Session]:
        if self.connection is None:
            raise RuntimeError("database is not open")
        if "id" not in self.columns:
            return []
        columns = ["id"] + sorted((self.columns & SAFE_COLUMNS) - {"id"})
        self.last_query = (
            "SELECT " + ", ".join(f'"{column}"' for column in columns) + ' FROM "sessions"'
        )
        result = []
        for row in self.connection.execute(self.last_query):
            values = dict(row)
            counters = {name: _nonnegative(values.get(name)) for name in COUNTER_COLUMNS}
            result.append(
                Session(
                    id=str(values["id"]),
                    source=values.get("source"),
                    model=values.get("model"),
                    parent_session_id=values.get("parent_session_id"),
                    counters=Counters(**counters),
                    estimated_cost_usd=_nonnegative_float(values.get("estimated_cost_usd")),
                    actual_cost_usd=_nonnegative_float(values.get("actual_cost_usd")),
                    started_at=_timestamp(values.get("started_at")),
                    ended_at=_timestamp(values.get("ended_at")),
                    title=values.get("title"),
                    cwd=values.get("cwd"),
                    git_repo_root=values.get("git_repo_root"),
                    last_activity_at=_timestamp(values.get("last_activity_at")),
                    end_reason=values.get("end_reason"),
                )
            )
        return result


def _nonnegative(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        if math.isfinite(number) and number >= 0 and number.is_integer():
            return int(number)
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _nonnegative_float(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _timestamp(value):
    if value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None
