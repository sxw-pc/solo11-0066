"""Replay contract: input identity, snapshots, parent/child runs and failures."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from drift_attr import store
from drift_attr.cli import main
from drift_attr.config import load_config
from drift_attr.engine import analyze, replay_run
from drift_attr.errors import DriftError
from helpers import config_dict, write_config, write_csv


DETECTORS = [
    {
        "name": "low_orders", "type": "threshold", "metrics": ["orders"],
        "severity": "high", "direction": "below", "lower": 5,
    }
]


def _project(tmp_path: Path, *, policy: str, rows: str | None = None) -> Path:
    cfg = config_dict(
        DETECTORS,
        dataset_path="data.csv",
        output={"directory": "output", "database": "drift_attr.sqlite3"},
    )
    cfg["replay"] = {"input_policy": policy}
    write_config(tmp_path / "rules.yaml", cfg)
    body = rows or (
        "2025-01-01,orders,channel,region_alpha,10\n"
        "2025-01-02,orders,channel,region_alpha,9\n"
        "2025-01-03,orders,channel,region_alpha,4\n"
        "2025-01-04,orders,channel,region_alpha,11\n"
        "2025-01-05,orders,channel,region_alpha,12\n"
        "2025-01-06,orders,channel,region_alpha,13\n"
    )
    write_csv(tmp_path / "data.csv", body)
    return tmp_path


def _db(root: Path) -> Path:
    return root / "output" / "drift_attr.sqlite3"


def _business_view(root: Path, run_id: str) -> dict:
    """Business fields that must be identical between a run and its replay."""
    events = [
        {key: event[key] for key in event if key not in {"event_id", "group_id", "run_id"}}
        for event in store.get_events(_db(root), run_id)
    ]
    items = [
        {key: item[key] for key in item if key not in {"item_id", "group_id", "event_id", "run_id"}}
        for item in store.get_attribution_items(_db(root), run_id)
    ]
    run = store.get_run(_db(root), run_id)
    return {
        "events": events,
        "attribution": items,
        "counts": (run["event_count"], run["anomaly_count"], run["attribution_count"]),
        "dataset_fingerprint": run["dataset_fingerprint"],
        "rule_fingerprint": run["rule_fingerprint"],
    }


def _run_ids(root: Path) -> set[str]:
    return {row["run_id"] for row in store.list_runs(_db(root))}


# ---------- reference policy ----------

def test_reference_replay_uses_same_bytes_and_keeps_parent(tmp_path):
    root = _project(tmp_path, policy="reference")
    parent = analyze(root / "rules.yaml", run_id="run-A", base_dir=root)
    binding = store.get_input_binding(_db(root), "run-A")
    assert binding["input_policy"] == "reference"
    assert binding["snapshot_path"] is None and binding["snapshot_relative"] is None
    assert binding["raw_sha256"] == hashlib.sha256(
        (root / "data.csv").read_bytes()
    ).hexdigest()
    assert binding["replay_of"] is None
    assert not (root / "output" / ".input-snapshots").exists()

    child = replay_run(_db(root), "run-A", run_id="run-B", base_dir=root)
    assert child.replay_of == "run-A" and child.input_policy == "reference"
    assert _run_ids(root) == {"run-A", "run-B"}
    child_row = store.get_run(_db(root), "run-B")
    assert child_row["replay_of"] == "run-A"
    assert child_row["input_policy"] == "reference"
    assert _business_view(root, "run-A") == _business_view(root, "run-B")
    # The parent run's report directory and ledger row are untouched.
    assert (root / "output" / "run-A" / "report.json").is_file()
    assert parent.dataset.fingerprint == child.dataset.fingerprint


def test_reference_replay_rejects_replaced_source_without_child(tmp_path):
    root = _project(tmp_path, policy="reference")
    analyze(root / "rules.yaml", run_id="run-A", base_dir=root)
    (root / "data.csv").write_text(
        "date,metric,dimension_name,dimension_value,value\n"
        "2025-01-01,orders,channel,region_alpha,10\n"
        "2025-01-02,orders,channel,region_alpha,9\n"
        "2025-01-03,orders,channel,region_alpha,999\n",
        encoding="utf-8",
    )
    before = _run_ids(root)
    with pytest.raises(DriftError) as exc:
        replay_run(_db(root), "run-A", run_id="run-B", base_dir=root)
    assert exc.value.code == "E_REPLAY_INPUT_MISMATCH"
    assert _run_ids(root) == before
    assert not (root / "output" / "run-B").exists()
    # Parent remains intact and still records the old canonical fingerprint.
    assert store.get_run(_db(root), "run-A") is not None


def test_reference_replay_missing_source_code_and_override_recovery(tmp_path):
    root = _project(tmp_path, policy="reference")
    original = (root / "data.csv").read_bytes()
    analyze(root / "rules.yaml", run_id="run-A", base_dir=root)
    moved = tmp_path / "archive" / "data-moved.csv"
    moved.parent.mkdir()
    moved.write_bytes(original)
    (root / "data.csv").unlink()

    with pytest.raises(DriftError) as exc:
        replay_run(_db(root), "run-A", run_id="run-B", base_dir=root)
    assert exc.value.code == "E_REPLAY_INPUT_MISSING"
    assert not (root / "output" / "run-B").exists()

    child = replay_run(
        _db(root), "run-A", run_id="run-B", base_dir=root, input_override=moved
    )
    assert child.replay_of == "run-A"
    assert _business_view(root, "run-A") == _business_view(root, "run-B")

    # Override pointing at different bytes is a mismatch, not a silent replay.
    other = tmp_path / "other.csv"
    other.write_bytes(original + b"2025-01-07,orders,channel,region_alpha,1\n")
    with pytest.raises(DriftError) as exc:
        replay_run(
            _db(root), "run-A", run_id="run-C", base_dir=root, input_override=other
        )
    assert exc.value.code == "E_REPLAY_INPUT_MISMATCH"
    assert "run-C" not in _run_ids(root)


def test_row_reorder_same_fingerprint_different_raw_sha_is_mismatch(tmp_path):
    root = _project(tmp_path, policy="reference")
    analyze(root / "rules.yaml", run_id="run-A", base_dir=root)
    lines = (root / "data.csv").read_text(encoding="utf-8").splitlines(keepends=True)
    header, data_lines = lines[0], lines[1:]
    (root / "data.csv").write_text(header + "".join(reversed(data_lines)), encoding="utf-8")
    # Canonical fingerprint is order-insensitive, raw bytes are not.
    reordered = load_config(root / "rules.yaml")
    from drift_attr.dataset import load_dataset

    assert load_dataset(reordered.dataset, root).fingerprint == store.get_input_binding(
        _db(root), "run-A"
    )["dataset_fingerprint"]
    with pytest.raises(DriftError) as exc:
        replay_run(_db(root), "run-A", run_id="run-B", base_dir=root)
    assert exc.value.code == "E_REPLAY_INPUT_MISMATCH"
    assert "run-B" not in _run_ids(root)


# ---------- snapshot policy ----------

def test_snapshot_replay_survives_source_deletion(tmp_path):
    root = _project(tmp_path, policy="snapshot")
    analyze(root / "rules.yaml", run_id="run-A", base_dir=root)
    binding = store.get_input_binding(_db(root), "run-A")
    assert binding["input_policy"] == "snapshot"
    snapshot_rel = binding["snapshot_relative"]
    snapshot = root / "output" / snapshot_rel
    assert snapshot.is_file()
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == binding["raw_sha256"]
    assert snapshot.read_bytes() == (root / "data.csv").read_bytes()
    # The snapshot lives outside any run report directory.
    assert snapshot_rel.startswith(".input-snapshots/")

    (root / "data.csv").unlink()
    child = replay_run(_db(root), "run-A", run_id="run-B", base_dir=root)
    assert child.replay_of == "run-A" and child.input_policy == "snapshot"
    assert _business_view(root, "run-A") == _business_view(root, "run-B")
    child_binding = store.get_input_binding(_db(root), "run-B")
    assert child_binding["replay_of"] == "run-A"
    assert child_binding["snapshot_relative"] is not None


def test_snapshot_does_not_fall_back_to_source_on_corruption_or_loss(tmp_path):
    root = _project(tmp_path, policy="snapshot")
    analyze(root / "rules.yaml", run_id="run-A", base_dir=root)
    binding = store.get_input_binding(_db(root), "run-A")
    snapshot = root / "output" / binding["snapshot_relative"]

    # One-byte tamper: even with the source still present, no source fallback.
    tampered = bytearray(snapshot.read_bytes())
    tampered[-1] ^= 0xFF
    snapshot.write_bytes(bytes(tampered))
    with pytest.raises(DriftError) as exc:
        replay_run(_db(root), "run-A", run_id="run-B", base_dir=root)
    assert exc.value.code == "E_REPLAY_INPUT_MISMATCH"
    assert "run-B" not in _run_ids(root)

    # Deleted snapshot: missing, again no fallback to the live source path.
    snapshot.unlink()
    with pytest.raises(DriftError) as exc:
        replay_run(_db(root), "run-A", run_id="run-B", base_dir=root)
    assert exc.value.code == "E_REPLAY_INPUT_MISSING"
    assert "run-B" not in _run_ids(root)
    assert not (root / "output" / "run-B").exists()

    # Override is rejected outright for snapshot policy.
    with pytest.raises(DriftError) as exc:
        replay_run(
            _db(root), "run-A", run_id="run-B", base_dir=root,
            input_override=root / "data.csv",
        )
    assert exc.value.code == "E_ARGUMENT"


def test_snapshot_seals_exact_bytes_loader_parsed(tmp_path):
    root = _project(tmp_path, policy="snapshot")
    result = analyze(root / "rules.yaml", run_id="run-A", base_dir=root)
    binding = store.get_input_binding(_db(root), "run-A")
    # Snapshot bytes are byte-identical to the one-time loader read.
    snapshot = root / "output" / binding["snapshot_relative"]
    assert snapshot.read_bytes() == result.dataset.raw_bytes
    assert binding["raw_sha256"] == result.dataset.raw_sha256


# ---------- replay-of replay and CLI ----------

def test_replay_of_replay_always_binds_to_original_parent(tmp_path):
    root = _project(tmp_path, policy="reference")
    analyze(root / "rules.yaml", run_id="run-A", base_dir=root)
    replay_run(_db(root), "run-A", run_id="run-B", base_dir=root)
    # Move the source: B's current directory layout must not become the anchor.
    moved = tmp_path / "moved.csv"
    shutil.move(root / "data.csv", moved)
    child = replay_run(
        _db(root), "run-A", run_id="run-C", base_dir=root, input_override=moved
    )
    assert child.replay_of == "run-A"
    binding_c = store.get_input_binding(_db(root), "run-C")
    assert binding_c["replay_of"] == "run-A"
    assert _business_view(root, "run-A") == _business_view(root, "run-C")
    # Re-replaying B also points back to A, using A's binding.
    child2 = replay_run(
        _db(root), "run-B", run_id="run-D", base_dir=root, input_override=moved
    )
    assert child2.replay_of == "run-B"
    assert store.get_input_binding(_db(root), "run-D")["replay_of"] == "run-B"
    assert store.get_run(_db(root), "run-D")["replay_of"] == "run-B"
    assert _business_view(root, "run-A") == _business_view(root, "run-D")


def test_unknown_run_and_missing_binding(tmp_path):
    root = _project(tmp_path, policy="reference")
    analyze(root / "rules.yaml", run_id="run-A", base_dir=root)
    with pytest.raises(DriftError) as exc:
        replay_run(_db(root), "absent", base_dir=root)
    assert exc.value.code == "E_RUN_NOT_FOUND"

    # A ledger row without an input binding cannot be replayed.
    with sqlite3.connect(_db(root)) as conn:
        conn.execute("DELETE FROM input_bindings WHERE run_id = 'run-A'")
    with pytest.raises(DriftError) as exc:
        replay_run(_db(root), "run-A", base_dir=root)
    assert exc.value.code == "E_REPLAY_BINDING_MISSING"


def test_replay_cli_end_to_end_and_failure_leaves_nothing(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path, policy="snapshot")
    monkeypatch.chdir(root)
    assert main(["analyze", "--config", "rules.yaml", "--run-id", "run-A"]) == 0
    capsys.readouterr()
    (root / "data.csv").unlink()
    assert main([
        "replay", "--run-id", "run-A", "--db", "output/drift_attr.sqlite3",
        "--child-run-id", "run-B",
    ]) == 0
    out = capsys.readouterr().out
    assert "parent_run_id: run-A" in out
    assert "replay_of: run-A" in out
    assert "run_id: run-B" in out and "input_policy: snapshot" in out
    assert _business_view(root, "run-A") == _business_view(root, "run-B")

    # Tamper with the snapshot: CLI exits 1 and creates no run-C.
    binding = store.get_input_binding(_db(root), "run-A")
    snapshot = root / "output" / binding["snapshot_relative"]
    raw = bytearray(snapshot.read_bytes())
    raw[10] ^= 0x01
    snapshot.write_bytes(bytes(raw))
    assert main([
        "replay", "--run-id", "run-A", "--db", "output/drift_attr.sqlite3",
        "--child-run-id", "run-C",
    ]) == 1
    err = capsys.readouterr().err
    assert "E_REPLAY_INPUT_MISMATCH" in err
    assert "run-C" not in _run_ids(root)
    assert not (root / "output" / "run-C").exists()


def test_snapshot_failure_leaves_no_ledger_row_or_binding(tmp_path, monkeypatch):
    root = _project(tmp_path, policy="snapshot")
    import drift_attr.engine as engine_mod

    def boom(*args, **kwargs):
        raise DriftError("E_REPLAY_SNAPSHOT", "simulated seal failure")

    monkeypatch.setattr(engine_mod, "seal_input_snapshot", boom)
    with pytest.raises(DriftError) as exc:
        analyze(root / "rules.yaml", run_id="run-A", base_dir=root)
    assert exc.value.code == "E_REPLAY_SNAPSHOT"
    assert store.get_run(_db(root), "run-A") is None
    assert store.get_input_binding(_db(root), "run-A") is None


def test_canonical_fingerprint_mismatch_also_rejects(tmp_path):
    root = _project(tmp_path, policy="reference")
    analyze(root / "rules.yaml", run_id="run-A", base_dir=root)
    # Forge a binding whose raw SHA matches but whose canonical fingerprint lies:
    # both identities must agree, either one failing is a mismatch.
    with sqlite3.connect(_db(root)) as conn:
        conn.execute(
            "UPDATE input_bindings SET dataset_fingerprint = ? WHERE run_id = 'run-A'",
            ("0" * 64,),
        )
    with pytest.raises(DriftError) as exc:
        replay_run(_db(root), "run-A", run_id="run-B", base_dir=root)
    assert exc.value.code == "E_REPLAY_INPUT_MISMATCH"
    assert "run-B" not in _run_ids(root)
    assert not (root / "output" / "run-B").exists()


def test_rewriting_run_id_replaces_binding_idempotently(tmp_path):
    root = _project(tmp_path, policy="snapshot")
    analyze(root / "rules.yaml", run_id="run-A", base_dir=root)
    replay_run(_db(root), "run-A", run_id="run-B", base_dir=root)
    replay_run(_db(root), "run-A", run_id="run-B", base_dir=root)
    assert _run_ids(root) == {"run-A", "run-B"}
    with sqlite3.connect(_db(root)) as conn:
        bindings = list(conn.execute(
            "SELECT run_id, replay_of FROM input_bindings ORDER BY run_id"
        ))
    assert bindings == [("run-A", None), ("run-B", "run-A")]
    assert _business_view(root, "run-A") == _business_view(root, "run-B")


def test_legacy_ledger_migrates_and_queries_replay_columns(tmp_path):    # A ledger created with the pre-replay schema (no replay columns/bindings).
    legacy = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(legacy) as conn:
        conn.executescript(
            """
            CREATE TABLE runs (
             run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
             status TEXT NOT NULL, config_path TEXT, config_fingerprint TEXT NOT NULL,
             dataset_path TEXT NOT NULL, dataset_fingerprint TEXT NOT NULL,
             rule_fingerprint TEXT NOT NULL, output_dir TEXT NOT NULL,
             series_count INTEGER NOT NULL DEFAULT 0, event_count INTEGER NOT NULL DEFAULT 0,
             anomaly_count INTEGER NOT NULL DEFAULT 0, attribution_count INTEGER NOT NULL DEFAULT 0,
             error_summary TEXT);
            """
        )
        conn.execute(
            """INSERT INTO runs VALUES ('old-run','2025-01-01T00:00:00Z',
            '2025-01-01T00:00:01Z','success','r.yaml','cfg','d.csv','fp','cfg',
            'output',1,1,1,1,NULL)"""
        )
    store.init_db(legacy)
    row = store.get_run(legacy, "old-run")
    assert row["replay_of"] is None and row["input_policy"] == "reference"
    assert store.get_input_binding(legacy, "old-run") is None


def test_web_detail_and_index_show_replay_relationship(tmp_path):
    from drift_attr.web import create_app

    root = _project(tmp_path, policy="snapshot")
    analyze(root / "rules.yaml", run_id="run-A", base_dir=root)
    replay_run(_db(root), "run-A", run_id="run-B", base_dir=root)
    app = create_app(_db(root), root / "output", root)
    client = app.test_client()
    detail = client.get("/runs/run-B").get_data(as_text=True)
    assert "重放来源" in detail and "run-A" in detail
    index = client.get("/").get_data(as_text=True)
    assert "重放来源" in index and "run-A" in index
    # Sealed input snapshots stay internal: the artifact route only serves
    # kinds registered in the artifacts table, never the snapshot directory.
    binding = store.get_input_binding(_db(root), "run-B")
    assert binding["snapshot_relative"].startswith(".input-snapshots/")
    assert client.get("/runs/run-B/artifact/rule_snapshot").status_code == 200
    assert client.get("/runs/run-B/artifact/input_snapshot").status_code == 404
    # Direct path traversal to the snapshot bytes is also refused.
    sneaky = client.get("/runs/run-B/artifact/..%2f..%2f.input-snapshots%2frun-A.csv")
    assert sneaky.status_code == 404
