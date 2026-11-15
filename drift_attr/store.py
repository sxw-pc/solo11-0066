"""SQLite run ledger with idempotent, transactional writes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from drift_attr.errors import DriftError

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS runs (
 run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
 status TEXT NOT NULL, config_path TEXT, config_fingerprint TEXT NOT NULL,
 dataset_path TEXT NOT NULL, dataset_fingerprint TEXT NOT NULL,
 rule_fingerprint TEXT NOT NULL, output_dir TEXT NOT NULL,
 series_count INTEGER NOT NULL DEFAULT 0, event_count INTEGER NOT NULL DEFAULT 0,
 anomaly_count INTEGER NOT NULL DEFAULT 0, attribution_count INTEGER NOT NULL DEFAULT 0,
 error_summary TEXT);
CREATE TABLE IF NOT EXISTS rule_snapshots (
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
 fingerprint TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS datasets (
 dataset_id INTEGER PRIMARY KEY AUTOINCREMENT,
 run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE CASCADE,
 path TEXT NOT NULL, fingerprint TEXT NOT NULL, row_count INTEGER NOT NULL,
 format TEXT NOT NULL, warnings_json TEXT NOT NULL, min_value REAL, max_value REAL);
CREATE TABLE IF NOT EXISTS anomaly_events (
 event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
 group_id TEXT NOT NULL, metric TEXT NOT NULL, dimension_name TEXT NOT NULL,
 dimension_value TEXT NOT NULL, anomaly_date TEXT NOT NULL, detector TEXT NOT NULL,
 rule_name TEXT NOT NULL, observed REAL NOT NULL, baseline REAL NOT NULL,
 score REAL NOT NULL, severity TEXT NOT NULL, evidence_json TEXT NOT NULL,
 window_start TEXT NOT NULL, window_end TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attribution_items (
 item_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
 group_id TEXT NOT NULL, event_id TEXT NOT NULL, attribution_dimension TEXT NOT NULL,
 dimension_value TEXT NOT NULL, current_value REAL NOT NULL,
 baseline_value REAL NOT NULL, contribution REAL NOT NULL, contribution_rate REAL,
 rank INTEGER NOT NULL, is_other INTEGER NOT NULL, formula TEXT NOT NULL,
 explanation TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts (
 artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
 kind TEXT NOT NULL, format TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(run_id, kind));
CREATE INDEX IF NOT EXISTS idx_events_run_date ON anomaly_events(run_id, anomaly_date, group_id);
CREATE INDEX IF NOT EXISTS idx_attr_run_group ON attribution_items(run_id, group_id, attribution_dimension, rank);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id, kind);
"""

PURGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS purge_intents (
 run_id TEXT PRIMARY KEY, strategy TEXT NOT NULL, reason TEXT NOT NULL,
 state TEXT NOT NULL, prior_status TEXT NOT NULL, base_dir TEXT NOT NULL,
 output_root TEXT NOT NULL, quarantine_root TEXT,
 quarantine_path TEXT, manifest_json TEXT NOT NULL,
 progress_json TEXT NOT NULL DEFAULT '[]',
 conflicts_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS purge_tombstones (
 run_id TEXT PRIMARY KEY, purged_at TEXT NOT NULL, strategy TEXT NOT NULL,
 reason TEXT NOT NULL, artifact_shas_json TEXT NOT NULL,
 conflicts_json TEXT NOT NULL DEFAULT '[]');
"""

SCHEMA = SCHEMA + PURGE_SCHEMA


def _connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path) -> None:
    try:
        with _connect(db_path) as conn:
            conn.executescript(SCHEMA)
    except sqlite3.Error as exc:
        raise DriftError("E_STORE", f"初始化台账失败: {exc}", path=str(db_path)) from exc


def _artifact_id(run_id: str, kind: str) -> str:
    return "art_" + hashlib.sha256(f"{run_id}\x1f{kind}".encode()).hexdigest()[:20]


def save_run(
    db_path: str | Path, *, run: dict[str, Any], rule_snapshot: dict[str, str],
    dataset: dict[str, Any], events: list[dict[str, Any]],
    attribution_items: list[dict[str, Any]], artifacts: list[dict[str, Any]],
) -> None:
    """Replace one run atomically; rerunning a run_id never duplicates rows."""
    try:
        with _connect(db_path) as conn:
            conn.executescript(SCHEMA)
            existing = conn.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run["run_id"],)
            ).fetchone()
            if existing is not None and existing["status"] == "purging":
                raise DriftError(
                    "E_PURGE_STATE",
                    f"run {run['run_id']} 正在清理，拒绝写入新分析结果",
                    path=str(db_path),
                )
            conn.execute("DELETE FROM runs WHERE run_id = ?", (run["run_id"],))
            conn.execute(
                """INSERT INTO runs (run_id, started_at, finished_at, status,
                config_path, config_fingerprint, dataset_path, dataset_fingerprint,
                rule_fingerprint, output_dir, series_count, event_count,
                anomaly_count, attribution_count, error_summary)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run["run_id"], run["started_at"], run["finished_at"], run["status"],
                    run.get("config_path", ""), run["config_fingerprint"],
                    run["dataset_path"], run["dataset_fingerprint"],
                    run["rule_fingerprint"], run["output_dir"], run["series_count"],
                    run["event_count"], run["anomaly_count"], run["attribution_count"],
                    run.get("error_summary"),
                ),
            )
            conn.execute(
                "INSERT INTO rule_snapshots VALUES (?,?,?,?)",
                (run["run_id"], rule_snapshot["fingerprint"], rule_snapshot["content"],
                 run["finished_at"]),
            )
            conn.execute(
                """INSERT INTO datasets (run_id, path, fingerprint, row_count, format,
                warnings_json, min_value, max_value) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    run["run_id"], dataset["path"], dataset["fingerprint"],
                    dataset["row_count"], dataset["format"],
                    json.dumps(dataset.get("warnings", []), ensure_ascii=False),
                    dataset.get("min_value"), dataset.get("max_value"),
                ),
            )
            conn.executemany(
                """INSERT INTO anomaly_events (event_id, run_id, group_id, metric,
                dimension_name, dimension_value, anomaly_date, detector, rule_name,
                observed, baseline, score, severity, evidence_json, window_start,
                window_end) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(
                    event["event_id"], run["run_id"], event["group_id"], event["metric"],
                    event["dimension_name"], event["dimension_value"], event["date"],
                    event["detector"], event["rule_name"], event["observed"],
                    event["baseline"], event["score"], event["severity"],
                    json.dumps(event["evidence"], ensure_ascii=False, sort_keys=True),
                    event["window_start"], event["window_end"],
                ) for event in events],
            )
            conn.executemany(
                """INSERT INTO attribution_items (item_id, run_id, group_id, event_id,
                attribution_dimension, dimension_value, current_value, baseline_value,
                contribution, contribution_rate, rank, is_other, formula, explanation)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(
                    item["item_id"], run["run_id"], item["group_id"], item["event_id"],
                    item["attribution_dimension"], item["dimension_value"],
                    item["current_value"], item["baseline_value"], item["contribution"],
                    item["contribution_rate"], item["rank"], 1 if item["is_other"] else 0,
                    item["formula"], item["explanation"],
                ) for item in attribution_items],
            )
            conn.executemany(
                """INSERT INTO artifacts (artifact_id, run_id, kind, format, path,
                sha256, created_at) VALUES (?,?,?,?,?,?,?)""",
                [(
                    _artifact_id(run["run_id"], item["kind"]), run["run_id"], item["kind"],
                    item["format"], item["path"], item["sha256"], run["finished_at"],
                ) for item in artifacts],
            )
    except sqlite3.Error as exc:
        raise DriftError("E_STORE", f"写入台账失败: {exc}", path=str(db_path)) from exc



def _query(db_path: str | Path, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    if not Path(db_path).exists():
        return []
    try:
        with _connect(db_path) as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []


def list_runs(
    db_path: str | Path, *, include_purging: bool = False
) -> list[dict[str, Any]]:
    rows = _query(db_path, "SELECT * FROM runs ORDER BY started_at DESC, run_id DESC")
    if include_purging:
        return rows
    return [row for row in rows if row.get("status") != "purging"]


def get_run(db_path: str | Path, run_id: str) -> dict[str, Any] | None:
    rows = _query(db_path, "SELECT * FROM runs WHERE run_id = ?", (run_id,))
    return rows[0] if rows else None


def get_events(db_path: str | Path, run_id: str) -> list[dict[str, Any]]:
    rows = _query(
        db_path,
        """SELECT * FROM anomaly_events WHERE run_id = ?
        ORDER BY anomaly_date, metric, dimension_name, dimension_value,
                 detector, rule_name""",
        (run_id,),
    )
    for row in rows:
        row["evidence"] = json.loads(row.pop("evidence_json"))
    return rows


def get_attribution_items(db_path: str | Path, run_id: str) -> list[dict[str, Any]]:
    rows = _query(
        db_path,
        """SELECT * FROM attribution_items WHERE run_id = ?
        ORDER BY group_id, attribution_dimension, rank, dimension_value""",
        (run_id,),
    )
    for row in rows:
        row["is_other"] = bool(row["is_other"])
    return rows


def get_artifacts(db_path: str | Path, run_id: str) -> list[dict[str, Any]]:
    return _query(
        db_path, "SELECT * FROM artifacts WHERE run_id = ? ORDER BY kind", (run_id,)
    )


def get_rule_snapshot(db_path: str | Path, run_id: str) -> str | None:
    rows = _query(
        db_path, "SELECT content FROM rule_snapshots WHERE run_id = ?", (run_id,)
    )
    return str(rows[0]["content"]) if rows else None


# ---------------------------------------------------------------------------
# Purge lifecycle
# ---------------------------------------------------------------------------

PURGE_TABLES = ("anomaly_events", "attribution_items", "rule_snapshots", "datasets", "runs")
PURGE_ACTIVE_STATES = frozenset({"pending", "deleting", "quarantining", "conflicted"})
PURGE_STATES = frozenset(
    {"pending", "deleting", "quarantining", "quarantined", "conflicted", "complete"}
)


def _write_query(db_path: str | Path, statements: list[tuple[str, tuple]]) -> None:
    try:
        with _connect(db_path) as conn:
            conn.executescript(SCHEMA)
            for sql, params in statements:
                conn.execute(sql, params)
    except sqlite3.Error as exc:
        raise DriftError("E_STORE", f"清理台账写入失败: {exc}", path=str(db_path)) from exc


PURGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS purge_intents (
 run_id TEXT PRIMARY KEY, strategy TEXT NOT NULL, reason TEXT NOT NULL,
 state TEXT NOT NULL, prior_status TEXT NOT NULL, base_dir TEXT NOT NULL,
 output_root TEXT NOT NULL, quarantine_root TEXT,
 quarantine_path TEXT, manifest_json TEXT NOT NULL,
 progress_json TEXT NOT NULL DEFAULT '[]',
 conflicts_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS purge_tombstones (
 run_id TEXT PRIMARY KEY, purged_at TEXT NOT NULL, strategy TEXT NOT NULL,
 reason TEXT NOT NULL, artifact_shas_json TEXT NOT NULL,
 conflicts_json TEXT NOT NULL DEFAULT '[]');
"""


def verify_purge_schema(db_path: str | Path) -> None:
    """Refuse to act unless every core table the purge relies on is present."""
    path = Path(db_path)
    if not path.is_file():
        raise DriftError("E_STORE", "台账不存在，无法确认清理状态", path=str(path))
    required = {
        "runs": {"run_id", "status", "output_dir", "finished_at"},
        "rule_snapshots": {"run_id", "content"},
        "datasets": {"run_id"},
        "anomaly_events": {"run_id"},
        "attribution_items": {"run_id"},
        "artifacts": {"run_id", "kind", "path", "sha256"},
    }
    try:
        conn = sqlite3.connect(path)
        try:
            present = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            missing = sorted(set(required) - present)
            if missing:
                raise DriftError(
                    "E_STORE", f"台账缺少清理所需表: {', '.join(missing)}", path=str(path)
                )
            for table, columns in required.items():
                actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                absent = sorted(columns - actual)
                if absent:
                    raise DriftError(
                        "E_STORE",
                        f"台账表 {table} 缺少列: {', '.join(absent)}",
                        path=str(path),
                    )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise DriftError("E_STORE", f"无法确认台账状态: {exc}", path=str(path)) from exc


def migrate_purge_schema(db_path: str | Path) -> None:
    """Add purge_intents/purge_tombstones to an existing ledger.

    Core tables are verified first; a ledger that cannot support a safe purge
    is refused rather than guessed at.
    """
    verify_purge_schema(db_path)
    try:
        with _connect(db_path) as conn:
            conn.executescript(PURGE_SCHEMA)
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(purge_intents)")
            }
            if "progress_json" not in columns:
                conn.execute(
                    "ALTER TABLE purge_intents ADD COLUMN progress_json TEXT NOT NULL DEFAULT '[]'"
                )
    except sqlite3.Error as exc:
        raise DriftError("E_STORE", f"清理台账迁移失败: {exc}", path=str(db_path)) from exc


def purge_child_counts(db_path: str | Path, run_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in PURGE_TABLES:
        rows = _query(db_path, f"SELECT COUNT(*) AS n FROM {table} WHERE run_id = ?", (run_id,))
        counts[table] = int(rows[0]["n"]) if rows else 0
    return counts


def is_purging(db_path: str | Path, run_id: str) -> bool:
    rows = _query(db_path, "SELECT status FROM runs WHERE run_id = ?", (run_id,))
    return bool(rows) and rows[0]["status"] == "purging"


def get_purge_intent(db_path: str | Path, run_id: str) -> dict[str, Any] | None:
    rows = _query(db_path, "SELECT * FROM purge_intents WHERE run_id = ?", (run_id,))
    if not rows:
        return None
    intent = rows[0]
    intent["manifest"] = json.loads(intent.pop("manifest_json"))
    intent["progress"] = json.loads(intent.pop("progress_json", "[]") or "[]")
    intent["conflicts"] = json.loads(intent.pop("conflicts_json"))
    return intent


def list_purge_intents(db_path: str | Path) -> list[dict[str, Any]]:
    intents: list[dict[str, Any]] = []
    for row in _query(db_path, "SELECT run_id FROM purge_intents ORDER BY run_id"):
        intent = get_purge_intent(db_path, row["run_id"])
        if intent is not None:
            intents.append(intent)
    return intents


def begin_purge(
    db_path: str | Path, *, run_id: str, strategy: str, reason: str,
    prior_status: str, base_dir: str, output_root: str,
    quarantine_root: str | None, manifest: list[dict[str, Any]],
    conflicts: list[dict[str, Any]], now: str,
) -> dict[str, Any]:
    """Persist the exact path/SHA manifest and hide the run in one transaction.

    Re-entering with the same strategy resumes the stored intent; a different
    strategy or a run that is not in a purgeable state is refused.
    """
    existing = get_purge_intent(db_path, run_id)
    if existing is not None:
        if existing["strategy"] != strategy:
            raise DriftError(
                "E_PURGE_CONFLICT",
                f"run {run_id} 已有策略 {existing['strategy']} 的清理意图，不能改为 {strategy}",
            )
        return existing
    statements = [
        (
            "INSERT INTO purge_intents (run_id, strategy, reason, state, prior_status,"
            " base_dir, output_root, quarantine_root, quarantine_path, manifest_json,"
            " progress_json, conflicts_json, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, strategy, reason, "pending", prior_status, base_dir, output_root,
                quarantine_root, None, json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                "[]", json.dumps(conflicts, ensure_ascii=False, sort_keys=True), now, now,
            ),
        ),
        ("UPDATE runs SET status = 'purging' WHERE run_id = ?", (run_id,)),
    ]
    _write_query(db_path, statements)
    intent = get_purge_intent(db_path, run_id)
    assert intent is not None
    return intent


def update_purge(
    db_path: str | Path, run_id: str, *, state: str | None = None,
    conflicts: list[dict[str, Any]] | None = None,
    progress: list[str] | None = None,
    quarantine_path: str | None = None, now: str | None = None,
) -> dict[str, Any]:
    if state is not None and state not in PURGE_STATES:
        raise DriftError("E_ARGUMENT", f"未知清理状态: {state}")
    sets: list[str] = []
    params: list[Any] = []
    if state is not None:
        sets.append("state = ?")
        params.append(state)
    if conflicts is not None:
        sets.append("conflicts_json = ?")
        params.append(json.dumps(conflicts, ensure_ascii=False, sort_keys=True))
    if progress is not None:
        sets.append("progress_json = ?")
        params.append(json.dumps(progress, ensure_ascii=False, sort_keys=True))
    if quarantine_path is not None:
        sets.append("quarantine_path = ?")
        params.append(quarantine_path)
    if now is not None:
        sets.append("updated_at = ?")
        params.append(now)
    if not sets:
        return get_purge_intent(db_path, run_id) or {}
    params.append(run_id)
    _write_query(db_path, [(f"UPDATE purge_intents SET {', '.join(sets)} WHERE run_id = ?", tuple(params))])
    intent = get_purge_intent(db_path, run_id)
    assert intent is not None
    return intent


def finish_purge(
    db_path: str | Path, *, run_id: str, purged_at: str, strategy: str, reason: str,
    artifact_shas: list[dict[str, str]], conflicts: list[dict[str, Any]],
    keep_intent: bool = False,
) -> None:
    """Delete the run and every child row, leaving only a minimal tombstone.

    SQLite has no transaction across the filesystem, so this is the final
    idempotent step: all artifact bytes were already handled by the caller.
    The tombstone deliberately keeps no attribution values, rule text or paths.
    """
    minimal = [
        {key: item[key] for key in ("kind", "code") if key in item}
        for item in conflicts
    ]
    statements: list[tuple[str, tuple]] = [
        ("DELETE FROM anomaly_events WHERE run_id = ?", (run_id,)),
        ("DELETE FROM attribution_items WHERE run_id = ?", (run_id,)),
        ("DELETE FROM rule_snapshots WHERE run_id = ?", (run_id,)),
        ("DELETE FROM datasets WHERE run_id = ?", (run_id,)),
        ("DELETE FROM artifacts WHERE run_id = ?", (run_id,)),
        ("DELETE FROM runs WHERE run_id = ?", (run_id,)),
        (
            "INSERT OR REPLACE INTO purge_tombstones (run_id, purged_at, strategy, reason,"
            " artifact_shas_json, conflicts_json) VALUES (?,?,?,?,?,?)",
            (
                run_id, purged_at, strategy, reason,
                json.dumps(artifact_shas, ensure_ascii=False, sort_keys=True),
                json.dumps(minimal, ensure_ascii=False, sort_keys=True),
            ),
        ),
    ]
    if not keep_intent:
        statements.append(("DELETE FROM purge_intents WHERE run_id = ?", (run_id,)))
    _write_query(db_path, statements)


def restore_run_after_purge(
    db_path: str | Path, run_id: str, prior_status: str
) -> None:
    """Remove an abandoned intent and make the run visible again atomically."""
    _write_query(db_path, [
        ("DELETE FROM purge_intents WHERE run_id = ?", (run_id,)),
        ("UPDATE runs SET status = ? WHERE run_id = ?", (prior_status, run_id)),
    ])


def get_tombstone(db_path: str | Path, run_id: str) -> dict[str, Any] | None:
    rows = _query(db_path, "SELECT * FROM purge_tombstones WHERE run_id = ?", (run_id,))
    if not rows:
        return None
    tombstone = rows[0]
    tombstone["artifact_shas"] = json.loads(tombstone.pop("artifact_shas_json"))
    tombstone["conflicts"] = json.loads(tombstone.pop("conflicts_json"))
    return tombstone


def list_tombstones(db_path: str | Path) -> list[dict[str, Any]]:
    return [
        get_tombstone(db_path, row["run_id"])  # type: ignore[misc]
        for row in _query(db_path, "SELECT run_id FROM purge_tombstones ORDER BY run_id")
    ]


def artifacts_of_other_runs(db_path: str | Path, run_id: str) -> list[dict[str, Any]]:
    """Artifact rows belonging to any other run, for shared-path detection."""
    return _query(
        db_path,
        """SELECT a.*, r.status AS run_status FROM artifacts a
        JOIN runs r ON r.run_id = a.run_id WHERE a.run_id != ?""",
        (run_id,),
    )

