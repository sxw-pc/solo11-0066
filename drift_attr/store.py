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
-- Group-level keyset pagination: ordered, distinct anomaly locations per run.
-- The leading run_id column keeps every paging query scoped to one run's slice.
CREATE INDEX IF NOT EXISTS idx_events_run_group_keys ON anomaly_events(
 run_id, anomaly_date, metric, dimension_name, dimension_value, group_id);
-- Fetch every detector event of a page's groups without a full-run scan.
CREATE INDEX IF NOT EXISTS idx_events_run_group_id ON anomaly_events(
 run_id, group_id, detector, rule_name);
"""

# Ordered columns that identify one anomaly location inside a run. group_id is a
# deterministic digest of the four location columns (see normalize.make_group_id),
# so it only acts as a deterministic tie-breaker for the keyset cursor.
GROUP_KEY_COLUMNS: tuple[str, ...] = (
    "anomaly_date", "metric", "dimension_name", "dimension_value", "group_id",
)


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


def list_runs(db_path: str | Path) -> list[dict[str, Any]]:
    return _query(db_path, "SELECT * FROM runs ORDER BY started_at DESC, run_id DESC")


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


def run_version_token(run: dict[str, Any]) -> str:
    """Fingerprint of a run record generation.

    ``save_run`` replaces every row of a run atomically; paging cursors carry this
    token so a cursor produced by an older generation can be detected instead of
    silently mixing old and new anomaly groups in one paging sequence.
    """
    fields = (
        "started_at", "finished_at", "status", "config_fingerprint",
        "dataset_fingerprint", "rule_fingerprint", "event_count",
        "anomaly_count", "attribution_count",
    )
    payload = "\x1f".join(str(run.get(field)) for field in fields)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _group_key_clause(after: tuple[Any, ...] | None) -> tuple[str, tuple[Any, ...]]:
    """Build a lexicographic keyset predicate from the previous page's last key."""
    if not after:
        return "", ()
    columns = ", ".join(GROUP_KEY_COLUMNS)
    placeholders = ", ".join("?" for _ in GROUP_KEY_COLUMNS)
    return f" AND ({columns}) > ({placeholders})", tuple(after)


def get_group_page(
    db_path: str | Path,
    run_id: str,
    *,
    page_size: int,
    after: tuple[Any, ...] | None = None,
) -> list[dict[str, Any]]:
    """Return up to ``page_size + 1`` ordered group keys for one run.

    The extra row (when present) signals that another page exists; callers drop it
    before fetching the page's events. The query is served by the covering index
    ``idx_events_run_group_keys`` and never sorts or scans another run's rows.
    """
    keyset_sql, keyset_params = _group_key_clause(after)
    columns = ", ".join(GROUP_KEY_COLUMNS)
    sql = f"""SELECT DISTINCT {columns} FROM anomaly_events
    WHERE run_id = ?{keyset_sql}
    ORDER BY {columns} LIMIT ?"""
    return _query(db_path, sql, (run_id, *keyset_params, page_size + 1))


def get_events_for_groups(
    db_path: str | Path, run_id: str, group_ids: list[str]
) -> list[dict[str, Any]]:
    """Fetch every detector event of the given groups (whole groups, never split)."""
    if not group_ids:
        return []
    placeholders = ", ".join("?" for _ in group_ids)
    rows = _query(
        db_path,
        f"""SELECT * FROM anomaly_events WHERE run_id = ?
        AND group_id IN ({placeholders})
        ORDER BY group_id, detector, rule_name""",
        (run_id, *group_ids),
    )
    for row in rows:
        row["evidence"] = json.loads(row.pop("evidence_json"))
    return rows


def get_attribution_items_for_groups(
    db_path: str | Path, run_id: str, group_ids: list[str]
) -> list[dict[str, Any]]:
    """Fetch every attribution item of the given groups, including empty groups."""
    if not group_ids:
        return []
    placeholders = ", ".join("?" for _ in group_ids)
    rows = _query(
        db_path,
        f"""SELECT * FROM attribution_items WHERE run_id = ?
        AND group_id IN ({placeholders})
        ORDER BY group_id, attribution_dimension, rank, dimension_value""",
        (run_id, *group_ids),
    )
    for row in rows:
        row["is_other"] = bool(row["is_other"])
    return rows


def get_run_detail_page(
    db_path: str | Path,
    run_id: str,
    *,
    page_size: int,
    after: tuple[Any, ...] | None = None,
) -> dict[str, Any]:
    """One keyset page of fully-assembled anomaly groups.

    Only the group keys of this page are selected first; all detector events and
    attribution items are then fetched for exactly those groups, so reads scale
    with the page rather than with the whole run. Groups without any attribution
    items are returned with an empty ``attribution`` mapping and must not be
    presented as "unloaded" by callers.
    """
    keys = get_group_page(db_path, run_id, page_size=page_size, after=after)
    has_more = len(keys) > page_size
    page_keys = keys[:page_size]
    group_ids = [key["group_id"] for key in page_keys]
    events = get_events_for_groups(db_path, run_id, group_ids)
    items = get_attribution_items_for_groups(db_path, run_id, group_ids)
    return {
        "group_keys": page_keys,
        "group_ids": group_ids,
        "events": events,
        "attribution_items": items,
        "has_more": has_more,
        "next_after": tuple(page_keys[-1][column] for column in GROUP_KEY_COLUMNS)
        if page_keys
        else None,
    }


def get_rule_snapshot(db_path: str | Path, run_id: str) -> str | None:
    rows = _query(
        db_path, "SELECT content FROM rule_snapshots WHERE run_id = ?", (run_id,)
    )
    return str(rows[0]["content"]) if rows else None
