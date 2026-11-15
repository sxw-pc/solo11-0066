"""Retention purge: dry-run, conflicts, resumability, Web gating, A/B semantics."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from drift_attr import purge, store
from drift_attr.engine import analyze
from drift_attr.errors import DriftError
from drift_attr.web import create_app
from helpers import demo_project


def _run(tmp_path: Path, run_id: str = "target-run"):
    root = demo_project(tmp_path)
    result = analyze(root / "fixtures" / "rules.yaml", run_id=run_id, base_dir=root)
    return root, result


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _db_children(db: Path, run_id: str) -> dict[str, int]:
    with sqlite3.connect(db) as conn:
        return {
            table: conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            for table in ("anomaly_events", "attribution_items", "rule_snapshots",
                          "datasets", "artifacts", "runs")
        }


def _client(result):
    return create_app(result.database_path, result.run_dir.parent, result.run_dir.parents[1]).test_client()


def test_dry_run_mutates_nothing(tmp_path):
    root, result = _run(tmp_path)
    before = {p.name: _sha(p) for p in result.run_dir.iterdir()}
    plan = purge.build_plan(
        result.database_path, "target-run", strategy="hard",
        base_dir=root, output_root=root / "output",
    )
    summary = purge.describe_plan(plan)
    assert summary["sqlite"]["tables"]["anomaly_events"] > 0
    assert summary["sqlite"]["tables"]["runs"] == 1
    assert {item["kind"] for item in summary["artifacts"]} == {
        "report_json", "report_markdown", "report_html",
        "attribution_json", "rule_snapshot",
    }
    assert summary["conflicts"] == []
    assert {p.name: _sha(p) for p in result.run_dir.iterdir()} == before
    assert store.get_run(result.database_path, "target-run")["status"] == "success"
    assert store.get_purge_intent(result.database_path, "target-run") is None


def test_hash_mismatch_preserves_replaced_file(tmp_path):
    root, result = _run(tmp_path)
    report = result.run_dir / "report.json"
    replaced = report.read_text(encoding="utf-8") + "\n# replaced\n"
    report.write_text(replaced, encoding="utf-8")
    replacement_sha = _sha(report)

    outcome = purge.execute(
        result.database_path, "target-run", strategy="hard", reason="retention",
        base_dir=root, output_root=root / "output",
    )
    assert outcome["state"] == "conflicted"
    assert any(item["code"] == "conflict_hash" for item in outcome["conflicts"])
    assert report.read_text(encoding="utf-8") == replaced
    assert _sha(report) == replacement_sha
    # nothing else was deleted, the run is hidden but its rows still exist
    assert (result.run_dir / "report.html").is_file()
    run = store.get_run(result.database_path, "target-run")
    assert run["status"] == "purging"
    assert store.get_events(result.database_path, "target-run")


def test_path_escape_rejected(tmp_path):
    root, result = _run(tmp_path)
    outside = root / "shared" / "report.json"
    outside.parent.mkdir()
    outside.write_text("outside\n", encoding="utf-8")
    with sqlite3.connect(result.database_path) as conn:
        conn.execute(
            "UPDATE artifacts SET path = ?, sha256 = ? WHERE run_id = ? AND kind = ?",
            ("../shared/report.json", _sha(outside), "target-run", "report_json"),
        )
    outcome = purge.execute(
        result.database_path, "target-run", strategy="hard", reason="retention",
        base_dir=root, output_root=root / "output",
    )
    assert outcome["state"] == "conflicted"
    assert any(item["code"] == "conflict_path" for item in outcome["conflicts"])
    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert result.run_dir.is_dir()


def test_symlink_rejected(tmp_path):
    root, result = _run(tmp_path)
    target = result.run_dir / "report.json"
    real = root / "real-report.json"
    real.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    target.unlink()
    target.symlink_to(real)
    outcome = purge.execute(
        result.database_path, "target-run", strategy="hard", reason="retention",
        base_dir=root, output_root=root / "output",
    )
    assert outcome["state"] == "conflicted"
    assert any(item["code"] == "conflict_symlink" for item in outcome["conflicts"])
    assert target.is_symlink() and real.is_file()


def test_shared_path_conflict_keeps_other_run_downloadable(tmp_path):
    root = demo_project(tmp_path)
    first = analyze(root / "fixtures" / "rules.yaml", run_id="run-a", base_dir=root)
    second = analyze(root / "fixtures" / "rules.yaml", run_id="run-b", base_dir=root)
    shared = first.run_dir / "report.json"
    shared_sha = _sha(shared)
    with sqlite3.connect(first.database_path) as conn:
        conn.execute(
            "UPDATE artifacts SET path = ?, sha256 = ? WHERE run_id = ? AND kind = ?",
            (str(shared), shared_sha, "run-b", "report_json"),
        )
    outcome = purge.execute(
        first.database_path, "run-a", strategy="hard", reason="retention",
        base_dir=root, output_root=root / "output",
    )
    assert outcome["state"] == "conflicted"
    assert any(item["code"] == "conflict_shared" for item in outcome["conflicts"])
    assert shared.is_file() and _sha(shared) == shared_sha
    client = create_app(first.database_path, root / "output", root).test_client()
    assert client.get("/runs/run-b/artifact/report_json").status_code == 200
    assert client.get("/runs/run-a").status_code == 404


def test_interrupted_hard_delete_resumes_from_manifest(tmp_path):
    root, result = _run(tmp_path)
    plan = purge.build_plan(
        result.database_path, "target-run", strategy="hard",
        base_dir=root, output_root=root / "output",
    )
    store.begin_purge(
        result.database_path, run_id="target-run", strategy="hard", reason="retention",
        prior_status="success", base_dir=str(root), output_root=str(root / "output"),
        quarantine_root=None, manifest=purge._manifest_from_plan(plan), conflicts=[],
        now=purge.iso_utc(),
    )
    store.update_purge(result.database_path, "target-run", state="deleting",
                       now=purge.iso_utc())
    (result.run_dir / "report.json").unlink()
    (result.run_dir / "report.md").unlink()

    client = _client(result)
    assert client.get("/runs/target-run").status_code == 404
    assert client.get("/runs/target-run/artifact/report_html").status_code == 404
    assert "target-run" not in client.get("/").get_data(as_text=True)

    outcome = purge.execute(
        result.database_path, "target-run", strategy="hard", reason="retention",
        base_dir=root, output_root=root / "output",
    )
    assert outcome["state"] == "complete"
    # only the remaining managed files are unlinked on resume; the two already
    # gone files are not re-deleted or reported as errors
    assert sorted(outcome["deleted"]) == [
        "attribution_json", "report_html", "rule_snapshot",
    ]
    assert store.get_run(result.database_path, "target-run") is None
    assert not result.run_dir.exists()
    assert _db_children(result.database_path, "target-run") == {
        "anomaly_events": 0, "attribution_items": 0, "rule_snapshots": 0,
        "datasets": 0, "artifacts": 0, "runs": 0,
    }


def test_web_rejects_during_purge_and_restore_aborts(tmp_path):
    root, result = _run(tmp_path)
    plan = purge.build_plan(
        result.database_path, "target-run", strategy="quarantine",
        base_dir=root, output_root=root / "output",
    )
    store.begin_purge(
        result.database_path, run_id="target-run", strategy="quarantine", reason="retention",
        prior_status="success", base_dir=str(root), output_root=str(root / "output"),
        quarantine_root=str(root / "output" / "quarantine"),
        manifest=purge._manifest_from_plan(plan), conflicts=[], now=purge.iso_utc(),
    )
    client = _client(result)
    assert client.get("/runs/target-run").status_code == 404
    assert client.get("/runs/target-run/artifact/report_json").status_code == 404

    outcome = purge.execute(
        result.database_path, "target-run", strategy="quarantine", reason="retention",
        base_dir=root, output_root=root / "output",
    )
    assert outcome["state"] == "complete"
    quarantined = Path(outcome["quarantine_path"])
    assert quarantined.is_dir() and (quarantined / "report.json").is_file()
    assert not result.run_dir.exists()
    assert client.get("/tombstones").status_code == 200
    assert "target-run" in client.get("/tombstones").get_data(as_text=True)

    with pytest.raises(DriftError) as excinfo:
        purge.restore_quarantine(
            result.database_path, "target-run", base_dir=root, output_root=root / "output"
        )
    assert excinfo.value.code == "E_PURGE_STATE"

    discarded = purge.discard_quarantine(
        result.database_path, "target-run", base_dir=root, output_root=root / "output"
    )
    assert discarded["state"] == "complete"
    assert not quarantined.exists()
    tombstone = store.get_tombstone(result.database_path, "target-run")
    assert tombstone["strategy"] == "quarantine"
    assert len(tombstone["artifact_shas"]) == 5
    assert store.get_purge_intent(result.database_path, "target-run") is None


def test_quarantine_restore_before_final_cleanup(tmp_path):
    root, result = _run(tmp_path)
    plan = purge.build_plan(
        result.database_path, "target-run", strategy="quarantine",
        base_dir=root, output_root=root / "output",
    )
    store.begin_purge(
        result.database_path, run_id="target-run", strategy="quarantine", reason="retention",
        prior_status="success", base_dir=str(root), output_root=str(root / "output"),
        quarantine_root=str(root / "output" / "quarantine"),
        manifest=purge._manifest_from_plan(plan), conflicts=[], now=purge.iso_utc(),
    )
    store.update_purge(result.database_path, "target-run", state="quarantining",
                       now=purge.iso_utc())
    purge._move_directory(Path(plan["quarantine_source"]), Path(plan["quarantine_path"]))
    store.update_purge(result.database_path, "target-run", state="quarantined",
                       quarantine_path=plan["quarantine_path"], now=purge.iso_utc())

    restored = purge.restore_quarantine(
        result.database_path, "target-run", base_dir=root, output_root=root / "output"
    )
    assert restored["state"] == "restored"
    assert result.run_dir.is_dir()
    assert store.get_run(result.database_path, "target-run")["status"] == "success"
    client = _client(result)
    assert client.get("/runs/target-run").status_code == 200
    assert client.get("/runs/target-run/artifact/report_json").status_code == 200


def test_tombstone_keeps_no_paths_or_attribution_text(tmp_path):
    root, result = _run(tmp_path)
    outcome = purge.execute(
        result.database_path, "target-run", strategy="hard", reason="retention-expired",
        base_dir=root, output_root=root / "output",
    )
    assert outcome["state"] == "complete"
    tombstone = store.get_tombstone(result.database_path, "target-run")
    assert tombstone["reason"] == "retention-expired"
    assert set(tombstone) == {
        "run_id", "purged_at", "strategy", "reason", "artifact_shas", "conflicts",
    }
    serialized = repr(tombstone)
    assert "target-run/" not in serialized.replace("target-run", "")
    assert "output/" not in serialized
    assert store.get_events(result.database_path, "target-run") == []
    assert store.get_attribution_items(result.database_path, "target-run") == []
    assert store.get_rule_snapshot(result.database_path, "target-run") is None
    assert store.get_artifacts(result.database_path, "target-run") == []


def test_purge_is_idempotent_after_completion(tmp_path):
    root, result = _run(tmp_path)
    first = purge.execute(
        result.database_path, "target-run", strategy="hard", reason="retention",
        base_dir=root, output_root=root / "output",
    )
    second = purge.execute(
        result.database_path, "target-run", strategy="hard", reason="retention",
        base_dir=root, output_root=root / "output",
    )
    assert first["state"] == second["state"] == "complete"
    assert second.get("already") is True
    assert store.get_tombstone(result.database_path, "target-run") is not None


def test_missing_database_or_tables_refused(tmp_path):
    with pytest.raises(DriftError) as excinfo:
        purge.build_plan(
            tmp_path / "absent.sqlite3", "x", strategy="hard",
            base_dir=tmp_path, output_root=tmp_path / "output",
        )
    assert excinfo.value.code == "E_STORE"

    db = tmp_path / "broken.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY)")
    with pytest.raises(DriftError) as excinfo:
        purge.build_plan(
            db, "x", strategy="hard", base_dir=tmp_path, output_root=tmp_path / "output",
        )
    assert excinfo.value.code == "E_STORE"


def test_missing_artifact_blocks_fresh_purge(tmp_path):
    root, result = _run(tmp_path)
    (result.run_dir / "report.md").unlink()
    outcome = purge.execute(
        result.database_path, "target-run", strategy="hard", reason="retention",
        base_dir=root, output_root=root / "output",
    )
    assert outcome["state"] == "conflicted"
    assert any(item["code"] == "conflict_missing" for item in outcome["conflicts"])
    assert (result.run_dir / "report.html").is_file()


def test_strategy_switch_refused_and_abort_restores_visibility(tmp_path):
    root, result = _run(tmp_path)
    plan = purge.build_plan(
        result.database_path, "target-run", strategy="hard",
        base_dir=root, output_root=root / "output",
    )
    store.begin_purge(
        result.database_path, run_id="target-run", strategy="hard", reason="retention",
        prior_status="success", base_dir=str(root), output_root=str(root / "output"),
        quarantine_root=None, manifest=purge._manifest_from_plan(plan), conflicts=[],
        now=purge.iso_utc(),
    )
    with pytest.raises(DriftError) as excinfo:
        purge.build_plan(
            result.database_path, "target-run", strategy="quarantine",
            base_dir=root, output_root=root / "output",
        )
    assert excinfo.value.code == "E_PURGE_CONFLICT"
    assert _client(result).get("/runs/target-run").status_code == 404

    aborted = purge.abort_purge(result.database_path, "target-run")
    assert aborted["state"] == "aborted"
    assert _client(result).get("/runs/target-run").status_code == 200
    assert result.run_dir.is_dir()


def test_rerun_refused_while_purging(tmp_path):
    root, result = _run(tmp_path)
    plan = purge.build_plan(
        result.database_path, "target-run", strategy="hard",
        base_dir=root, output_root=root / "output",
    )
    store.begin_purge(
        result.database_path, run_id="target-run", strategy="hard", reason="retention",
        prior_status="success", base_dir=str(root), output_root=str(root / "output"),
        quarantine_root=None, manifest=purge._manifest_from_plan(plan), conflicts=[],
        now=purge.iso_utc(),
    )
    with pytest.raises(DriftError) as excinfo:
        analyze(root / "fixtures" / "rules.yaml", run_id="target-run", base_dir=root)
    assert excinfo.value.code == "E_PURGE_STATE"
    # analysis never silently un-hides the run or replaces the intent
    assert store.get_run(result.database_path, "target-run")["status"] == "purging"
    assert store.get_purge_intent(result.database_path, "target-run") is not None


def test_quarantine_refuses_to_move_shared_foreign_artifact(tmp_path):
    root = demo_project(tmp_path)
    first = analyze(root / "fixtures" / "rules.yaml", run_id="run-a", base_dir=root)
    second = analyze(root / "fixtures" / "rules.yaml", run_id="run-b", base_dir=root)
    shared = first.run_dir / "report.json"
    shared_sha = _sha(shared)
    # run-b references a file physically inside run-a's directory
    with sqlite3.connect(first.database_path) as conn:
        conn.execute(
            "UPDATE artifacts SET path = ?, sha256 = ? WHERE run_id = ? AND kind = ?",
            (str(shared), shared_sha, "run-b", "report_json"),
        )
    outcome = purge.execute(
        first.database_path, "run-a", strategy="quarantine", reason="retention",
        base_dir=root, output_root=root / "output",
    )
    assert outcome["state"] == "conflicted"
    assert any(item["code"] == "conflict_shared" for item in outcome["conflicts"])
    assert shared.is_file()
    assert not (root / "output" / "quarantine").exists()
    client = create_app(first.database_path, root / "output", root).test_client()
    assert client.get("/runs/run-b/artifact/report_json").status_code == 200
