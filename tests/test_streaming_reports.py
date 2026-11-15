"""Bounded-memory streaming report pipeline tests.

Covers the acceptance contract:

* extra serialization memory stays flat while events/attribution items grow;
* streaming artifacts are byte-identical to the materialized reference output;
* SHA-256 in the ledger matches the final on-disk file byte for byte;
* mid-render failures never register a run or leave truncated/partial files;
* ``AnalysisResult.report`` lazily materializes the original dict structure.
"""
from __future__ import annotations

import hashlib
import io
import json
import tracemalloc
from datetime import date, timedelta
from pathlib import Path

import pytest

from drift_attr import engine, html_report, store
from drift_attr.engine import AnalysisResult, analyze
from drift_attr.errors import DriftError
from drift_attr.report import (
    STREAM_BLOCK_SIZE,
    ChunkedTextWriter,
    ReportContext,
    build_report,
    make_report_context,
    report_to_markdown,
    write_attribution_json,
    write_markdown,
    write_report_json,
)
from drift_attr.schema import AnomalyEvent, AnomalyGroup, AttributionItem
from helpers import ROOT, demo_project


@pytest.fixture
def synthetic_deps(tmp_path):
    """Valid config/dataset/build triple for synthetic in-memory groups."""
    root = demo_project(tmp_path)
    from drift_attr.engine import validate

    validated = validate(root / "fixtures" / "rules.yaml", root)
    return validated.config, validated.dataset, validated.build


# ---------------------------------------------------------------------------
# Synthetic large-run builders (domain objects stay the single fact source).
# ---------------------------------------------------------------------------

def _make_event(run_id: str, index: int, group_index: int, day: date) -> AnomalyEvent:
    return AnomalyEvent(
        event_id=f"evt_{run_id}_{group_index}_{index}",
        group_id=f"grp_{run_id}_{group_index}",
        run_id=run_id,
        metric="orders",
        dimension_name="channel",
        dimension_value=f"region_{group_index % 97:03d}",
        date=day,
        detector="threshold",
        rule_name="rule_demo",
        observed=20.0 + index,
        baseline=45.0,
        score=-25.0,
        severity="high",
        evidence={"summary": f"证据 {index} 含竖线|与换行\n和 <html> 字符"},
        window_start=day,
        window_end=day,
    )


def _make_item(run_id: str, group_index: int, rank: int) -> AttributionItem:
    return AttributionItem(
        item_id=f"attr_{run_id}_{group_index}_{rank}",
        group_id=f"grp_{run_id}_{group_index}",
        event_id=f"evt_{run_id}_{group_index}_0",
        attribution_dimension="channel",
        dimension_value=f"channel_{rank:03d}",
        current_value=100.0 + rank,
        baseline_value=80.0,
        contribution=20.0,
        contribution_rate=None if rank % 17 == 0 else 0.42,
        rank=rank,
        is_other=rank == 50,
        formula="current 100.000000 - baseline 80.000000 = contribution +20.000000",
        explanation=f"维度 channel_{rank:03d} 的解释文本。",
    )


def make_synthetic_groups(
    run_id: str, group_count: int, events_per_group: int, items_per_group: int
) -> list[AnomalyGroup]:
    base_day = date(2025, 1, 10)
    groups: list[AnomalyGroup] = []
    for group_index in range(group_count):
        day = base_day + timedelta(days=group_index)
        events = [
            _make_event(run_id, index, group_index, day)
            for index in range(events_per_group)
        ]
        group = AnomalyGroup(
            group_id=f"grp_{run_id}_{group_index}",
            run_id=run_id,
            metric="orders",
            dimension_name="channel",
            dimension_value=f"region_{group_index % 97:03d}",
            date=day,
            severity="high",
            events=events,
            attribution_items=[
                _make_item(run_id, group_index, rank)
                for rank in range(1, items_per_group + 1)
            ],
        )
        groups.append(group)
    return groups


def synthetic_context(
    config, dataset, build, groups, events, tmp_path: Path
) -> ReportContext:
    run_dir = tmp_path / "output" / "synthetic-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    artifacts = engine._artifact_manifest(run_dir, tmp_path)
    return make_report_context(
        run_id="synthetic-run", config=config, dataset=dataset, build=build,
        groups=groups, events=events, started_at="2026-01-02T03:04:05Z",
        finished_at="2026-01-02T03:04:06Z", base_dir=tmp_path, artifacts=artifacts,
    )


def _render_to_string(renderer, ctx, block_size: int = 4096) -> str:
    buffer = io.StringIO()
    writer = ChunkedTextWriter(buffer, block_size=block_size)
    renderer(writer, ctx)
    writer.close()
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Byte-identity: streaming output == materialized reference output.
# ---------------------------------------------------------------------------

def test_streaming_json_matches_materialized_report(tmp_path):
    root = demo_project(tmp_path)
    result = analyze(
        root / "fixtures" / "rules.yaml", run_id="stream-demo", base_dir=root
    )
    ctx = make_report_context(
        run_id=result.run_id, config=result.config, dataset=result.dataset,
        build=result.build, groups=result.groups, events=result.events,
        started_at="2026-01-02T03:04:05Z", finished_at="2026-01-02T03:04:05Z",
        base_dir=root, artifacts=engine._artifact_manifest(result.run_dir, root),
    )
    materialized = build_report(
        run_id=result.run_id, config=result.config, dataset=result.dataset,
        build=result.build, groups=result.groups, events=result.events,
        started_at="2026-01-02T03:04:05Z", finished_at="2026-01-02T03:04:05Z",
        base_dir=root, artifacts=engine._artifact_manifest(result.run_dir, root),
    )
    expected_json = (
        json.dumps(materialized, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    assert _render_to_string(write_report_json, ctx) == expected_json

    expected_attr = (
        json.dumps(
            {
                "schema_version": "1.0",
                "run_id": materialized["run"]["run_id"],
                "config_fingerprint": materialized["run"]["config_fingerprint"],
                "dataset_fingerprint": materialized["run"]["dataset_fingerprint"],
                "items": [
                    {
                        "group_id": group["group_id"],
                        "metric": group["metric"],
                        "dimension_name": group["dimension_name"],
                        "dimension_value": group["dimension_value"],
                        "date": group["date"],
                        "attribution_dimension": dimension,
                        **item,
                    }
                    for group in materialized["anomalies"]
                    for dimension, items in group["attribution"].items()
                    for item in items
                ],
            },
            ensure_ascii=False, indent=2, sort_keys=True,
        )
        + "\n"
    )
    assert _render_to_string(write_attribution_json, ctx) == expected_attr
    assert _render_to_string(write_markdown, ctx) == report_to_markdown(
        materialized
    )
    streamed_html = "".join(
        html_report.iter_html_parts(
            materialized["run"], materialized["summary"],
            materialized["artifacts"], iter(materialized["anomalies"]),
        )
    )
    assert streamed_html == html_report.report_to_html(materialized)


def test_artifact_sha256_matches_final_file_bytes(tmp_path):
    root = demo_project(tmp_path)
    result = analyze(
        root / "fixtures" / "rules.yaml", run_id="sha-run", base_dir=root
    )
    ledger = store.get_artifacts(result.database_path, result.run_id)
    assert len(ledger) == 5
    for entry in ledger:
        final_path = root / entry["path"]
        assert final_path.is_file()
        assert not final_path.name.endswith(".partial")
        digest = hashlib.sha256(final_path.read_bytes()).hexdigest()
        assert digest == entry["sha256"]
    assert not list(result.run_dir.glob("*.partial"))


def test_golden_fixtures_remain_byte_identical(tmp_path):
    from datetime import datetime, timezone

    root = demo_project(tmp_path)
    fixed_now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    result = analyze(
        root / "fixtures" / "rules.yaml", run_id="golden-run",
        base_dir=root, now=fixed_now,
    )
    for name in (
        "report.json", "report.md", "report.html", "attribution.json",
        "rules.snapshot.yaml",
    ):
        current = (result.run_dir / name).read_bytes()
        golden = (ROOT / "tests" / "golden" / name).read_bytes()
        assert current == golden, name
    # Ledger hashes are the actual final-file hashes.
    for entry in store.get_artifacts(result.database_path, "golden-run"):
        digest = hashlib.sha256(
            (root / entry["path"]).read_bytes()
        ).hexdigest()
        assert digest == entry["sha256"]


def test_unicode_and_escape_boundaries_survive_tiny_chunks(
    synthetic_deps, tmp_path
):
    config, dataset, build = synthetic_deps
    groups = make_synthetic_groups("unicode-run", 3, 4, 7)
    events = [event for group in groups for event in group.events]
    ctx = synthetic_context(config, dataset, build, groups, events, tmp_path)
    # Tiny blocks force multi-byte CJK characters and JSON string escapes to
    # be split across many flush boundaries.
    report_text = _render_to_string(write_report_json, ctx, block_size=37)
    parsed = json.loads(report_text)
    assert parsed["summary"]["raw_event_count"] == 12
    raw_evidence = [
        event["evidence"]["summary"]
        for group in parsed["anomalies"]
        for event in group["raw_events"]
    ]
    assert "证据 0 含竖线|与换行\n和 <html> 字符" in raw_evidence
    # Every raw string survives intact (pipe/newline/angle brackets included).
    assert all("竖线|与换行\n和 <html> 字符" in value for value in raw_evidence)
    # Re-serializing the parsed tree must reproduce the streamed bytes exactly.
    assert (
        json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        == report_text
    )
    attr_text = _render_to_string(write_attribution_json, ctx, block_size=37)
    assert json.loads(attr_text)["items"][0]["dimension_value"] == "channel_001"
    markdown_text = _render_to_string(write_markdown, ctx, block_size=37)
    assert "证据 0 含竖线\\|与换行 和 &lt;html&gt; 字符" in markdown_text
    materialized = build_report(
        run_id=ctx.run_id, config=config, dataset=dataset, build=build,
        groups=groups, events=events, started_at=ctx.started_at,
        finished_at=ctx.finished_at, base_dir=tmp_path, artifacts=ctx.artifacts,
    )
    html_text = "".join(
        html_report.iter_html_parts(
            materialized["run"], materialized["summary"],
            materialized["artifacts"], ctx.group_views(),
        )
    )
    assert "&lt;html&gt;" in html_text and "<html>" not in html_text


def test_negative_zero_and_null_round_trip(tmp_path, synthetic_deps):
    from drift_attr.schema import stable_float

    assert stable_float(-0.0) == 0.0
    config, dataset, build = synthetic_deps
    groups = make_synthetic_groups("nz-run", 1, 1, 17)
    events = groups[0].events
    ctx = synthetic_context(config, dataset, build, groups, events, tmp_path)
    payload = json.loads(_render_to_string(write_report_json, ctx))
    rates = [
        item["contribution_rate"]
        for group in payload["anomalies"]
        for items in group["attribution"].values()
        for item in items
    ]
    assert None in rates
    attr = json.loads(_render_to_string(write_attribution_json, ctx))
    assert len(attr["items"]) == 17
    assert "-0.0" not in json.dumps(payload) and "-0.0" not in json.dumps(attr)


# ---------------------------------------------------------------------------
# Memory: extra serialization memory is bounded by block/group size.
# ---------------------------------------------------------------------------

def _peak_artifacts_memory(ctx, run_dir: Path) -> int:
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        engine._write_artifacts(run_dir, ctx, ctx.config.text, ctx.base_dir)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak


def test_extra_memory_does_not_grow_with_result_size(
    synthetic_deps, tmp_path
):
    config, dataset, build = synthetic_deps
    # The acceptance case holds 100k raw events; attribution stays deliberately
    # compact so wall-clock is dominated by the streaming buffers, not object
    # construction. Memory flatness is what the assertion measures.
    scales = {
        "small": (5, 10, 10),
        "medium": (25, 250, 20),
        "large": (200, 500, 25),
    }
    peaks: dict[str, int] = {}
    for label, (group_count, events_per_group, items_per_group) in scales.items():
        groups = make_synthetic_groups(
            f"mem-{label}", group_count, events_per_group, items_per_group
        )
        events = [event for group in groups for event in group.events]
        run_dir = tmp_path / f"out-{label}"
        run_dir.mkdir(parents=True)
        ctx = synthetic_context(config, dataset, build, groups, events, tmp_path)
        peaks[label] = _peak_artifacts_memory(ctx, run_dir)
        # Sanity: the run really is two orders of magnitude larger.
        total_events = group_count * events_per_group
        assert (run_dir / "report.json").stat().st_size > total_events * 50
    # Streaming budget: large-run peak may exceed small-run peak only by a
    # constant (one group view + block buffers), never by O(N) report copies.
    assert peaks["large"] - peaks["small"] < 2 * 1024 * 1024
    assert peaks["medium"] < peaks["large"]  # guard against a degenerate metric


def test_writer_buffer_never_exceeds_block_plus_fragment(
    synthetic_deps, tmp_path
):
    config, dataset, build = synthetic_deps
    groups = make_synthetic_groups("buf-run", 40, 200, 40)
    events = [event for group in groups for event in group.events]
    ctx = synthetic_context(config, dataset, build, groups, events, tmp_path)

    class MeasuringWriter(ChunkedTextWriter):
        max_buffer = 0

        def write(self, text):
            super().write(text)
            self.max_buffer = max(self.max_buffer, len(self._buffer))

    handle = io.StringIO()
    writer = MeasuringWriter(handle, block_size=STREAM_BLOCK_SIZE)
    write_report_json(writer, ctx)
    writer.close()
    # The internal buffer always drains back below one block after a fragment.
    assert writer.max_buffer < STREAM_BLOCK_SIZE + 4096


# ---------------------------------------------------------------------------
# Failure injection: no ledger row, no truncated/partial artifacts exposed.
# ---------------------------------------------------------------------------

class FailingWriter(ChunkedTextWriter):
    def __init__(self, handle, *, fail_after_bytes: int, label: str, seen: dict):
        super().__init__(handle)
        self._fail_after = fail_after_bytes
        self._label = label
        self._seen = seen
        self._emitted = 0

    def write(self, text):
        super().write(text)
        self._emitted += len(text.encode("utf-8"))
        self._seen[self._label] = self._emitted
        if self._emitted >= self._fail_after:
            raise OSError(f"injected failure in {self._label}")


@pytest.mark.parametrize(
    "failing_kind",
    ["report_json", "report_markdown", "report_html", "attribution_json",
     "rule_snapshot"],
)
def test_mid_render_failure_leaves_no_ledger_or_partial_files(
    tmp_path, monkeypatch, failing_kind
):
    root = demo_project(tmp_path)
    config_path = root / "fixtures" / "rules.yaml"
    seen: dict[str, int] = {}
    real_factory = engine._open_chunked_writer
    order: list[str] = []

    def factory(handle):
        # Handles are opened in the fixed artifact spec order.
        kind = ["report_json", "report_markdown", "report_html",
                "attribution_json", "rule_snapshot"][len(order)]
        order.append(kind)
        if kind == failing_kind:
            return FailingWriter(
                handle, fail_after_bytes=128, label=kind, seen=seen
            )
        return real_factory(handle)

    monkeypatch.setattr(engine, "_open_chunked_writer", factory)
    with pytest.raises(DriftError) as excinfo:
        analyze(config_path, run_id="fail-run", base_dir=root)
    assert "E_ANALYSIS" in str(excinfo.value)

    run_dir = root / "output" / "fail-run"
    assert not run_dir.exists() or not list(run_dir.iterdir())
    assert not list(root.glob("output/**/*.partial"))
    database = root / "output" / "drift_attr.sqlite3"
    assert store.get_run(database, "fail-run") is None
    assert store.get_artifacts(database, "fail-run") == []
    assert seen[failing_kind] >= 128  # the failure really happened mid-render


def test_hash_mismatch_after_rename_aborts_and_cleans(tmp_path, monkeypatch):
    root = demo_project(tmp_path)
    config_path = root / "fixtures" / "rules.yaml"
    real_hash_file = engine._hash_file

    def corrupted_hash(path):
        if path.name == "report.html":
            return "0" * 64
        return real_hash_file(path)

    monkeypatch.setattr(engine, "_hash_file", corrupted_hash)
    with pytest.raises(DriftError) as excinfo:
        analyze(config_path, run_id="hash-run", base_dir=root)
    assert "E_ANALYSIS" in str(excinfo.value)
    run_dir = root / "output" / "hash-run"
    assert not run_dir.exists() or not list(run_dir.iterdir())
    database = root / "output" / "drift_attr.sqlite3"
    assert store.get_run(database, "hash-run") is None


def test_successful_rerun_after_failure_recovers(tmp_path, monkeypatch):
    root = demo_project(tmp_path)
    config_path = root / "fixtures" / "rules.yaml"
    calls = {"count": 0}
    real_factory = engine._open_chunked_writer

    def flaky(handle):
        calls["count"] += 1
        if calls["count"] == 2:  # fail inside the markdown renderer
            raise OSError("transient injected failure")
        return real_factory(handle)

    monkeypatch.setattr(engine, "_open_chunked_writer", flaky)
    with pytest.raises(DriftError):
        analyze(config_path, run_id="retry-run", base_dir=root)
    monkeypatch.undo()
    result = analyze(config_path, run_id="retry-run", base_dir=root)
    assert len(store.get_artifacts(result.database_path, "retry-run")) == 5
    assert not list(result.run_dir.glob("*.partial"))


# ---------------------------------------------------------------------------
# Lazy AnalysisResult.report
# ---------------------------------------------------------------------------

def test_report_is_lazy_until_explicitly_accessed(tmp_path, monkeypatch):
    root = demo_project(tmp_path)
    built: dict[str, int] = {}
    real_builder = engine.build_report

    def counting_builder(*args, **kwargs):
        built["calls"] = built.get("calls", 0) + 1
        return real_builder(*args, **kwargs)

    monkeypatch.setattr(engine, "build_report", counting_builder)
    result = analyze(
        root / "fixtures" / "rules.yaml", run_id="lazy-run", base_dir=root
    )
    assert built == {}  # analyze() never materializes the full report
    assert isinstance(result, AnalysisResult)
    report = result.report
    assert built == {"calls": 1}
    assert report["run"]["run_id"] == "lazy-run"
    assert report["summary"]["anomaly_count"] >= 1
    assert {group["group_id"] for group in report["anomalies"]} == {
        group.group_id for group in result.groups
    }
    # Cached: repeated access does not rebuild.
    assert result.report is report
    assert built == {"calls": 1}


def test_lazy_report_matches_on_disk_json_without_reloading_file(tmp_path):
    root = demo_project(tmp_path)
    result = analyze(
        root / "fixtures" / "rules.yaml", run_id="lazy-json-run", base_dir=root
    )
    on_disk = json.loads((result.run_dir / "report.json").read_text("utf-8"))
    # The lazy dict is rebuilt from domain objects, not read back from disk.
    assert result.report == on_disk
    assert result.report["artifacts"] == [
        {"kind": item["kind"], "format": item["format"], "path": item["path"]}
        for item in result.artifacts
    ]
