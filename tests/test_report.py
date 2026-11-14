import json

from drift_attr.engine import analyze
from drift_attr.normalize import group_events
from helpers import ROOT, demo_project, make_event


def normalized_anomalies(report):
    result = []
    for group in report["anomalies"]:
        result.append({
            "date": group["date"], "metric": group["metric"],
            "dimension_name": group["dimension_name"],
            "dimension_value": group["dimension_value"],
            "severity": group["severity"], "detectors": group["detectors"],
            "rule_names": group["rule_names"], "event_count": group["event_count"],
            "attribution_windows": group["attribution_windows"],
            "events": [
                {key: event[key] for key in (
                    "detector", "rule_name", "observed", "baseline", "score",
                    "window_start", "window_end",
                )}
                for event in group["raw_events"]
            ],
            "attribution": {
                dimension: [
                    {key: item[key] for key in (
                        "rank", "dimension_value", "current_value",
                        "baseline_value", "contribution", "contribution_rate",
                        "is_other",
                    )}
                    for item in items
                ]
                for dimension, items in group["attribution"].items()
            },
        })
    return result


def test_demo_reports_match_expected(tmp_path):
    root = demo_project(tmp_path)
    result = analyze(root / "fixtures" / "rules.yaml", run_id="demo-run", base_dir=root)
    expected = json.loads(
        (ROOT / "fixtures" / "expected" / "anomalies_demo.json").read_text(encoding="utf-8")
    )
    assert result.report["summary"]["anomaly_count"] == expected["anomaly_count"]
    assert result.report["summary"]["raw_event_count"] == expected["raw_event_count"]
    assert normalized_anomalies(result.report) == expected["anomalies"]
    expected_attr = json.loads(
        (ROOT / "fixtures" / "expected" / "attribution_demo.json").read_text(encoding="utf-8")
    )
    flattened = []
    for group in result.report["anomalies"]:
        for dimension, items in group["attribution"].items():
            for item in items:
                flattened.append({
                    "date": group["date"], "metric": group["metric"],
                    "dimension_name": group["dimension_name"],
                    "dimension_value": group["dimension_value"],
                    "attribution_dimension": dimension,
                    **{key: item[key] for key in (
                        "rank", "dimension_value", "current_value",
                        "baseline_value", "contribution", "contribution_rate",
                        "is_other",
                    )},
                })
    assert expected_attr == {"item_count": len(flattened), "items": flattened}


def test_artifacts_are_offline_and_complete(tmp_path):
    root = demo_project(tmp_path)
    result = analyze(root / "fixtures" / "rules.yaml", run_id="offline-run", base_dir=root)
    kinds = {artifact["kind"] for artifact in result.artifacts}
    assert kinds == {
        "report_json", "report_markdown", "report_html",
        "attribution_json", "rule_snapshot",
    }
    for artifact in result.artifacts:
        assert (root / artifact["path"]).is_file()
    html = (result.run_dir / "report.html").read_text(encoding="utf-8")
    assert "http://" not in html and "https://" not in html
    assert "<script" not in html and "cdn" not in html.lower()
    assert "漂移归因台" in html
    markdown = (result.run_dir / "report.md").read_text(encoding="utf-8")
    assert "# 漂移归因台分析报告" in markdown
    assert "贡献额" in markdown
    payload = json.loads((result.run_dir / "report.json").read_text(encoding="utf-8"))
    assert payload["run"]["dataset_fingerprint"] == result.dataset.fingerprint
    assert payload["run"]["config_fingerprint"] == result.config.fingerprint


def test_rerun_is_deterministic_except_timestamps(tmp_path):
    root = demo_project(tmp_path)
    first = analyze(root / "fixtures" / "rules.yaml", run_id="same-run", base_dir=root)
    second = analyze(root / "fixtures" / "rules.yaml", run_id="same-run", base_dir=root)
    assert first.report["anomalies"] == second.report["anomalies"]
    assert first.report["summary"] == second.report["summary"]
    assert first.report["run"]["dataset_fingerprint"] == second.report["run"]["dataset_fingerprint"]


def test_merge_events_keeps_raw_evidence():
    first = make_event()
    second = make_event()
    second.event_id = "evt_second"
    second.detector = "delta"
    second.rule_name = "rule_second"
    second.evidence = {"summary": "second evidence"}
    groups = group_events([first, second])
    assert len(groups) == 1
    assert len(groups[0].events) == 2
    assert groups[0].detectors == ["delta", "threshold"]
    assert [event.evidence["summary"] for event in groups[0].events] == [
        "second evidence", "demo evidence"
    ]
