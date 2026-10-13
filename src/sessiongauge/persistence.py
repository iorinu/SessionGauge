"""SessionGauge 専用 SQLite 永続層。Hermes DB には接続しない。"""

import json
import os
import sqlite3
import uuid
from pathlib import Path

from sessiongauge.accounting import counter_deltas
from sessiongauge.domain import Counters


class MonitorDB:
    def __init__(self, path: Path | str, *, source_paths=()):
        self.path = Path(path).expanduser()
        self.source_paths = tuple(Path(p).expanduser() for p in source_paths)
        self.connection = None

    def _guard(self):
        if self.path.is_symlink():
            raise ValueError("monitor database path must not be a symlink")
        target = self.path.resolve(strict=False)
        for source in self.source_paths:
            if source.is_symlink():
                raise ValueError("source database must not be a symlink")
            resolved = source.resolve(strict=False)
            same = target == resolved
            if self.path.exists() and source.exists():
                try:
                    same = same or os.path.samefile(self.path, source)
                except OSError:
                    pass
            if same:
                raise ValueError("monitor database aliases a source database")
            for suffix in ("-wal", "-shm", "-journal"):
                if target == Path(str(resolved) + suffix) or Path(str(target) + suffix) == resolved:
                    raise ValueError("monitor database aliases a source journal")
                monitor_sidecar = Path(str(self.path) + suffix)
                source_sidecar = Path(str(source) + suffix)
                for monitor_candidate in (monitor_sidecar,):
                    for source_candidate in (source, source_sidecar):
                        if monitor_candidate.exists() and source_candidate.exists():
                            try:
                                if os.path.samefile(monitor_candidate, source_candidate):
                                    raise ValueError(
                                        "monitor database sidecar aliases a source database"
                                    )
                            except OSError:
                                pass
                        if monitor_candidate.resolve(strict=False) == source_candidate.resolve(
                            strict=False
                        ):
                            raise ValueError("monitor database sidecar aliases a source database")

    def __enter__(self):
        # 検査は必ずディレクトリ作成・権限変更・SQLite接続より先に行う。
        self._guard()
        parent_existed = self.path.parent.exists()
        if parent_existed and self.path.parent.stat().st_mode & 0o022:
            raise ValueError("monitor database directory must not be group/world writable")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not parent_existed:
            os.chmod(self.path.parent, 0o700)
        if self.path.exists():
            if self.path.is_symlink():
                self._guard()
                raise ValueError("monitor database must not be a symlink")
            if not self._is_monitor_database():
                raise ValueError("refusing to modify unknown SQLite database")
        try:
            self.connection = sqlite3.connect(self.path)
            os.chmod(self.path, 0o600)
            self.connection.execute("PRAGMA foreign_keys=ON")
            self._migrate()
            return self
        except BaseException:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
            raise

    def _is_monitor_database(self):
        try:
            probe = sqlite3.connect(f"file:{self.path.resolve()}?mode=ro", uri=True)
            version = probe.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            probe.close()
            return (
                version == 0
                and not tables
                or (
                    version in (1, 2)
                    and {"profiles", "tasks", "panes", "snapshots", "usage_attributions"}.issubset(
                        tables
                    )
                )
            )
        except sqlite3.Error:
            return False

    def __exit__(self, *_):
        if self.connection:
            self.connection.close()
            self.connection = None

    def _migrate(self):
        c = self.connection
        version = c.execute("PRAGMA user_version").fetchone()[0]
        if version > 2:
            raise ValueError("unsupported monitor database version")
        if version == 0:
            existing = {
                r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if existing and not {"profiles", "tasks", "panes"}.issubset(existing):
                raise ValueError("refusing to migrate unknown SQLite database")
        if version == 0:
            existing = {
                r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if existing and not {"profiles", "tasks", "panes"}.issubset(existing):
                raise ValueError("refusing to migrate unknown SQLite database")
        if version == 2:
            return
        statements = []
        if version == 0:
            statements = [
                "CREATE TABLE profiles(id TEXT PRIMARY KEY, name TEXT NOT NULL, db_path TEXT NOT NULL UNIQUE)",
                "CREATE TABLE tasks(task_id TEXT PRIMARY KEY, display_name TEXT, project_path TEXT, created_at REAL NOT NULL, closed_at REAL)",
                "CREATE TABLE panes(endpoint TEXT NOT NULL, pane_id TEXT NOT NULL, terminal_id TEXT, title TEXT, cwd TEXT, git_repo_root TEXT, first_seen REAL NOT NULL, last_seen REAL NOT NULL, status TEXT, PRIMARY KEY(endpoint,pane_id,terminal_id))",
                "CREATE TABLE pane_session_links(id INTEGER PRIMARY KEY, endpoint TEXT NOT NULL, pane_id TEXT NOT NULL, terminal_id TEXT, task_id TEXT NOT NULL REFERENCES tasks, profile_id TEXT NOT NULL REFERENCES profiles, session_id TEXT NOT NULL, valid_from REAL NOT NULL, valid_to REAL, source TEXT NOT NULL, confidence TEXT NOT NULL)",
                "CREATE TABLE task_pane_links(id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks, pane_key TEXT NOT NULL, valid_from REAL NOT NULL, valid_to REAL, source TEXT NOT NULL)",
                "CREATE TABLE session_catalog(profile_id TEXT NOT NULL REFERENCES profiles, session_id TEXT NOT NULL, parent_session_id TEXT, model TEXT, source TEXT, title TEXT, cwd TEXT, git_repo_root TEXT, started_at REAL, ended_at REAL, last_activity_at REAL, end_reason TEXT, PRIMARY KEY(profile_id,session_id))",
                "CREATE TABLE snapshots(id INTEGER PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES profiles, session_id TEXT NOT NULL, collected_at REAL NOT NULL, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER, estimated_cost_usd REAL, actual_cost_usd REAL, schema_version INTEGER, UNIQUE(profile_id,session_id,collected_at))",
                "CREATE INDEX snapshots_time ON snapshots(collected_at)",
                "CREATE TABLE usage_attributions(id INTEGER PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES profiles, session_id TEXT NOT NULL, interval_start REAL NOT NULL, interval_end REAL NOT NULL, task_id TEXT REFERENCES tasks, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER, estimated_cost_usd REAL, actual_cost_usd REAL, confidence TEXT, provenance TEXT, UNIQUE(profile_id,session_id,interval_start,interval_end))",
                "CREATE TABLE sync_state(source TEXT PRIMARY KEY, last_successful_poll REAL, failures INTEGER NOT NULL DEFAULT 0, stale INTEGER NOT NULL DEFAULT 0, warning TEXT)",
                "CREATE TABLE events(id INTEGER PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, detail TEXT)",
                "CREATE UNIQUE INDEX one_active_link ON pane_session_links(endpoint,pane_id,terminal_id) WHERE valid_to IS NULL",
            ]
        if version in (0, 1):
            if version == 0:
                statements += ["PRAGMA user_version=1"]
            statements += [
                "ALTER TABLE snapshots ADD COLUMN run_id TEXT",
                "ALTER TABLE snapshots ADD COLUMN source_ok INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE snapshots ADD COLUMN owner_task_id TEXT",
                "PRAGMA user_version=2",
            ]
        c.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                _migration_step(c, statement)
            c.commit()
        except BaseException:
            c.rollback()
            raise

    def register_profile(self, name, db_path):
        canonical = str(Path(db_path).expanduser().resolve(strict=False))
        profile_id = str(uuid.uuid5(uuid.NAMESPACE_URL, canonical))
        with self.connection:
            self.connection.execute(
                "INSERT INTO profiles VALUES(?,?,?) ON CONFLICT(db_path) DO UPDATE SET name=excluded.name",
                (profile_id, name, canonical),
            )
        return profile_id

    def record_sync_state(self, source, *, now, success, failures, warning=None):
        """各入力元の同期状態を最新の観測として保存する。"""
        with self.connection:
            self.connection.execute(
                "INSERT INTO sync_state(source,last_successful_poll,failures,stale,warning) VALUES(?,?,?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET last_successful_poll=COALESCE(excluded.last_successful_poll,sync_state.last_successful_poll),failures=excluded.failures,stale=excluded.stale,warning=excluded.warning",
                (str(source), now if success else None, failures, int(not success), warning),
            )

    def record_poll(
        self,
        *,
        profile_id,
        sessions,
        panes,
        matches,
        collected_at,
        schema_version=None,
        source_ok=True,
        run_id=None,
    ):
        """スナップショット、区間帰属、カタログを一つのSQLite transactionで保存する。"""
        c = self.connection
        run_id = run_id or "default"
        with c:
            latest = c.execute(
                "SELECT MAX(collected_at) FROM snapshots WHERE profile_id=?", (profile_id,)
            ).fetchone()[0]
            monotonic = latest is None or collected_at > latest
            if not monotonic:
                return
            self._record_panes(panes, collected_at)
            for pane, profile, sid, status in matches.get("_pane_matches", []):
                if status != "matched" or profile != profile_id:
                    continue
                task_id = matches.get(sid)
                if task_id:
                    c.execute(
                        "INSERT OR IGNORE INTO tasks(task_id,display_name,project_path,created_at) VALUES(?,?,?,?)",
                        (task_id, pane.title, pane.cwd, collected_at),
                    )
            for row in sessions:
                prior = c.execute(
                    "SELECT collected_at,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,estimated_cost_usd,actual_cost_usd,run_id,source_ok,owner_task_id FROM snapshots WHERE profile_id=? AND session_id=? ORDER BY collected_at DESC LIMIT 1",
                    (profile_id, row.id),
                ).fetchone()
                catalog_prior = c.execute(
                    "SELECT parent_session_id,model,source,title,cwd,git_repo_root,started_at,ended_at,last_activity_at,end_reason FROM session_catalog WHERE profile_id=? AND session_id=?",
                    (profile_id, row.id),
                ).fetchone()
                vals = (
                    row.counters.input_tokens,
                    row.counters.output_tokens,
                    row.counters.cache_read_tokens,
                    row.counters.cache_write_tokens,
                    row.counters.reasoning_tokens,
                    row.estimated_cost_usd,
                    row.actual_cost_usd,
                )
                owner = matches.get(row.id)
                if not monotonic or (prior and collected_at <= prior[0]):
                    continue
                catalog_values = (
                    row.parent_session_id,
                    row.model,
                    row.source,
                    row.title,
                    row.cwd,
                    row.git_repo_root,
                    row.started_at,
                    row.ended_at,
                    row.last_activity_at,
                    row.end_reason,
                )
                if (
                    row.ended_at is not None
                    and prior
                    and catalog_prior == catalog_values
                    and tuple(prior[1:8]) == vals
                ):
                    continue
                c.execute(
                    "INSERT OR IGNORE INTO snapshots(profile_id,session_id,collected_at,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,estimated_cost_usd,actual_cost_usd,schema_version,run_id,source_ok,owner_task_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        profile_id,
                        row.id,
                        collected_at,
                        *vals,
                        schema_version,
                        run_id,
                        int(source_ok),
                        owner,
                    ),
                )
                c.execute(
                    "INSERT INTO session_catalog(profile_id,session_id,parent_session_id,model,source,title,cwd,git_repo_root,started_at,ended_at,last_activity_at,end_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(profile_id,session_id) DO UPDATE SET parent_session_id=excluded.parent_session_id,model=excluded.model,source=excluded.source,title=excluded.title,cwd=excluded.cwd,git_repo_root=excluded.git_repo_root,started_at=excluded.started_at,ended_at=excluded.ended_at,last_activity_at=excluded.last_activity_at,end_reason=excluded.end_reason",
                    (
                        profile_id,
                        row.id,
                        row.parent_session_id,
                        row.model,
                        row.source,
                        row.title,
                        row.cwd,
                        row.git_repo_root,
                        row.started_at,
                        row.ended_at,
                        row.last_activity_at,
                        row.end_reason,
                    ),
                )
                if prior:
                    delta = counter_deltas(Counters(*prior[1:6]), row.counters)
                    costs = tuple(
                        (new - old) if new is not None and old is not None and new >= old else None
                        for old, new in zip(prior[6:8], vals[5:7])
                    )
                    task_id = (
                        owner
                        if source_ok
                        and owner
                        and prior[0] is not None
                        and prior[8] == run_id
                        and prior[9]
                        and prior[10] == owner
                        else None
                    )
                    c.execute(
                        "INSERT OR IGNORE INTO usage_attributions(profile_id,session_id,interval_start,interval_end,task_id,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,estimated_cost_usd,actual_cost_usd,confidence,provenance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            profile_id,
                            row.id,
                            prior[0],
                            collected_at,
                            task_id,
                            delta.input_tokens,
                            delta.output_tokens,
                            delta.cache_read_tokens,
                            delta.cache_write_tokens,
                            delta.reasoning_tokens,
                            *costs,
                            "exact" if task_id else "unattributed",
                            run_id if source_ok else "source_failure",
                        ),
                    )
            for pane, profile, sid, status in matches.get("_pane_matches", []):
                if status != "matched" or profile != profile_id:
                    continue
                existing = c.execute(
                    "SELECT task_id,profile_id,session_id FROM pane_session_links WHERE endpoint=? AND pane_id=? AND terminal_id IS ? AND valid_to IS NULL",
                    pane.generation_key,
                ).fetchone()
                task_id = (
                    existing[0]
                    if existing and existing[1:] == (profile, sid)
                    else matches.get(sid, str(uuid.uuid4()))
                )
                if existing and existing[1:] != (profile, sid):
                    c.execute(
                        "UPDATE pane_session_links SET valid_to=? WHERE endpoint=? AND pane_id=? AND terminal_id IS ? AND valid_to IS NULL",
                        (collected_at, *pane.generation_key),
                    )
                    c.execute(
                        "UPDATE task_pane_links SET valid_to=? WHERE pane_key=? AND valid_to IS NULL",
                        (collected_at, "|".join(str(x) for x in pane.generation_key)),
                    )
                c.execute(
                    "INSERT OR IGNORE INTO tasks(task_id,display_name,project_path,created_at) VALUES(?,?,?,?)",
                    (task_id, pane.title, pane.cwd, collected_at),
                )
                c.execute(
                    "INSERT OR IGNORE INTO pane_session_links(endpoint,pane_id,terminal_id,task_id,profile_id,session_id,valid_from,source,confidence) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        *pane.generation_key,
                        task_id,
                        profile,
                        sid,
                        collected_at,
                        "official",
                        "exact",
                    ),
                )
                pane_key = "|".join(str(x) for x in pane.generation_key)
                link = c.execute(
                    "SELECT task_id FROM task_pane_links WHERE pane_key=? AND valid_to IS NULL",
                    (pane_key,),
                ).fetchone()
                if not link:
                    c.execute(
                        "INSERT INTO task_pane_links(task_id,pane_key,valid_from,source) VALUES(?,?,?,?)",
                        (task_id, pane_key, collected_at, "official"),
                    )
                c.execute(
                    "UPDATE tasks SET display_name=?,project_path=? WHERE task_id=?",
                    (pane.title, pane.cwd, task_id),
                )

    def close_missing_panes(self, panes, now):
        """成功したherdr一覧に存在しないgenerationだけを閉じる。"""
        active = {pane.generation_key for pane in panes}
        with self.connection:
            latest = self.connection.execute("SELECT MAX(last_seen) FROM panes").fetchone()[0]
            if latest is not None and now < latest:
                return
            rows = self.connection.execute(
                "SELECT endpoint,pane_id,terminal_id FROM panes"
            ).fetchall()
            for key in rows:
                if key in active:
                    continue
                self.connection.execute(
                    "UPDATE panes SET status='inactive' WHERE endpoint=? AND pane_id=? AND terminal_id IS ?",
                    key,
                )
                links = self.connection.execute(
                    "SELECT task_id FROM pane_session_links WHERE endpoint=? AND pane_id=? AND terminal_id IS ? AND valid_to IS NULL",
                    key,
                ).fetchall()
                self.connection.execute(
                    "UPDATE pane_session_links SET valid_to=? WHERE endpoint=? AND pane_id=? AND terminal_id IS ? AND valid_to IS NULL",
                    (now, *key),
                )
                self.connection.execute(
                    "UPDATE task_pane_links SET valid_to=? WHERE pane_key=? AND valid_to IS NULL",
                    (now, "|".join(str(x) for x in key)),
                )
                for (task_id,) in links:
                    self.connection.execute(
                        "UPDATE tasks SET closed_at=COALESCE(closed_at,?) WHERE task_id=?",
                        (now, task_id),
                    )

    def record_snapshot(
        self,
        profile_id,
        session_id,
        counters: Counters,
        *,
        collected_at,
        estimated_cost_usd=None,
        actual_cost_usd=None,
        schema_version=None,
    ):
        c = self.connection
        prior_row = c.execute(
            "SELECT collected_at,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens FROM snapshots WHERE profile_id=? AND session_id=? ORDER BY collected_at DESC LIMIT 1",
            (profile_id, session_id),
        ).fetchone()
        if prior_row and collected_at <= prior_row[0]:
            return None
        prior = prior_row[1:] if prior_row else None
        delta = counter_deltas(Counters(*prior) if prior else None, counters)
        values = (
            counters.input_tokens,
            counters.output_tokens,
            counters.cache_read_tokens,
            counters.cache_write_tokens,
            counters.reasoning_tokens,
        )
        with c:
            c.execute(
                "INSERT OR IGNORE INTO snapshots(profile_id,session_id,collected_at,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,estimated_cost_usd,actual_cost_usd,schema_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    profile_id,
                    session_id,
                    collected_at,
                    *values,
                    estimated_cost_usd,
                    actual_cost_usd,
                    schema_version,
                ),
            )
        if prior:
            return delta
        return None

    def _record_panes(self, panes, collected_at):
        c = self.connection
        for pane in panes:
            key = (pane.endpoint, pane.pane_id, pane.terminal_id)
            previous = c.execute(
                "SELECT title FROM panes WHERE endpoint=? AND pane_id=? AND terminal_id IS ?", key
            ).fetchone()
            c.execute(
                "INSERT INTO panes VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(endpoint,pane_id,terminal_id) DO UPDATE SET title=excluded.title,cwd=excluded.cwd,last_seen=excluded.last_seen,status=excluded.status",
                (*key, pane.title, pane.cwd, None, collected_at, collected_at, pane.agent_status),
            )
            if previous and previous[0] != pane.title:
                detail = json.dumps(
                    {"pane_id": pane.pane_id, "old_title": previous[0], "new_title": pane.title},
                    ensure_ascii=False,
                )
                c.execute(
                    "INSERT INTO events(at,kind,detail) VALUES(?,?,?)",
                    (collected_at, "pane_title_changed", detail),
                )

    def record_panes(self, panes, collected_at):
        with self.connection:
            latest = self.connection.execute("SELECT MAX(last_seen) FROM panes").fetchone()[0]
            if latest is not None and collected_at <= latest:
                return
            self._record_panes(panes, collected_at)

    def snapshots(self, profile_id, session_id):
        return self.connection.execute(
            "SELECT count(*) FROM snapshots WHERE profile_id=? AND session_id=?",
            (profile_id, session_id),
        ).fetchone()[0]

    def link_manual(self, pane, profile_id, session_id, now):
        c = self.connection
        task_id = str(uuid.uuid4())
        with c:
            existing = c.execute(
                "SELECT task_id FROM pane_session_links WHERE endpoint=? AND pane_id=? AND terminal_id IS ? AND valid_to IS NULL",
                (pane.endpoint, pane.pane_id, pane.terminal_id),
            ).fetchone()
            if existing:
                task_id = existing[0]
                c.execute(
                    "UPDATE pane_session_links SET valid_to=? WHERE endpoint=? AND pane_id=? AND terminal_id IS ? AND valid_to IS NULL",
                    (now, pane.endpoint, pane.pane_id, pane.terminal_id),
                )
            c.execute(
                "INSERT OR IGNORE INTO tasks(task_id,display_name,project_path,created_at) VALUES(?,?,?,?)",
                (task_id, pane.title, pane.cwd, now),
            )
            c.execute(
                "INSERT INTO panes VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(endpoint,pane_id,terminal_id) DO UPDATE SET title=excluded.title,cwd=excluded.cwd,last_seen=excluded.last_seen,status=excluded.status",
                (
                    pane.endpoint,
                    pane.pane_id,
                    pane.terminal_id,
                    pane.title,
                    pane.cwd,
                    None,
                    now,
                    now,
                    pane.agent_status,
                ),
            )
            c.execute(
                "INSERT INTO pane_session_links(endpoint,pane_id,terminal_id,task_id,profile_id,session_id,valid_from,source,confidence) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    pane.endpoint,
                    pane.pane_id,
                    pane.terminal_id,
                    task_id,
                    profile_id,
                    session_id,
                    now,
                    "manual",
                    "confirmed",
                ),
            )
            c.execute(
                "INSERT INTO events(at,kind,detail) VALUES(?,?,?)",
                (now, "manual_link", f"{pane.endpoint}:{pane.pane_id}"),
            )
        return task_id


def _migration_step(connection, statement):
    connection.execute(statement)
