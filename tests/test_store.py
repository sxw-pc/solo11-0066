from drift_attr import store


def run_payload(run_id="run-store"):
    return {
        "run_id": run_id, "started_at": "2025-01-01T00:00:00Z",
        "finished_at": "2025-01-01T00:00:01Z", "status": "success",
        "config_path": "fixtures/rules.yaml", "config_fingerprint": "cfg",
        "dataset_path": "fixtures/data.csv", "dataset_fingerprint": "data",
        "rule_fingerprint": "cfg", "output_dir": "output",
        "series_count": 2, "event_count": 2, "anomaly_count": 1,
        "attribution_count": 1, "error_summary": None,
    }


def event_payload(event_id, detector="threshold"):
    return {
        "event_id": event_id, "group_id": "grp_1", "run_id": "run-store",
        "metric": "orders", "dimension_name": "channel",
        "dimension_value": "region_alpha", "date": "2025-01-03",
        "detector": detector, "rule_name": "rule_" + detector,
        "observed": 20.0, "baseline": 45.0, "score": -25.0,
        "severity": "high", "evidence": {"summary": "demo"},
        "window_start": "2025-01-03", "window_end": "2025-01-03",
    }


def attr_payload():
    return {
        "item_id": "attr_1", "group_id": "grp_1", "event_id": "evt_1",
        "attribution_dimension": "channel", "dimension_value": "region_alpha",
        "current_value": 40.0, "baseline_value": 60.0, "contribution": -20.0,
        "contribution_rate": 1.0, "rank": 1, "is_other": False,
        "formula": "demo", "explanation": "demo",
    }


def artifact_payload():
    return {
        "kind": "report_json", "format": "json", "path": "output/run-store/report.json",
        "sha256": "a" * 64,
    }


def save(db, run_id="run-store"):
    store.save_run(
        db,
        run=run_payload(run_id),
        rule_snapshot={"fingerprint": "cfg", "content": "version: 1\n"},
        dataset={
            "path": "fixtures/data.csv", "fingerprint": "data",
            "row_count": 1, "format": "long", "warnings": [], "min_value": 1,
            "max_value": 2,
        },
        events=[event_payload("evt_1"), event_payload("evt_2", "delta")],
        attribution_items=[attr_payload()],
        artifacts=[artifact_payload()],
    )


def test_save_query_and_idempotent_rewrite(tmp_path):
    db = tmp_path / "ledger.sqlite3"
    assert store.list_runs(db) == []
    save(db)
    save(db)
    runs = store.list_runs(db)
    assert len(runs) == 1
    assert runs[0]["run_id"] == "run-store" and runs[0]["event_count"] == 2
    assert store.get_run(db, "run-store")["anomaly_count"] == 1
    assert store.get_run(db, "absent") is None
    events = store.get_events(db, "run-store")
    assert len(events) == 2
    assert events[0]["evidence"] == {"summary": "demo"}
    items = store.get_attribution_items(db, "run-store")
    assert len(items) == 1 and items[0]["is_other"] is False
    assert store.get_artifacts(db, "run-store")[0]["kind"] == "report_json"
    assert store.get_rule_snapshot(db, "run-store") == "version: 1\n"


def test_missing_database_reads_are_empty(tmp_path):
    db = tmp_path / "absent.sqlite3"
    assert store.get_run(db, "x") is None
    assert store.get_events(db, "x") == []
    assert store.get_attribution_items(db, "x") == []
    assert store.get_artifacts(db, "x") == []
    assert store.get_rule_snapshot(db, "x") is None
