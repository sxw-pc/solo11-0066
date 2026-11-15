"""End-to-end validation, detection, attribution, rendering and ledger writes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from drift_attr.attribution import attribute_groups
from drift_attr.config import AppConfig, load_config, parse_config_text
from drift_attr.dataset import (
    Dataset, dataset_summary, load_dataset, parse_dataset_bytes, resolve_path,
)
from drift_attr.detectors import get_detector
from drift_attr.errors import DriftError, fail
from drift_attr.html_report import report_to_html
from drift_attr.normalize import group_events, make_event_id, make_group_id
from drift_attr.report import (
    attribution_document, build_report, display_path, report_to_markdown,
)
from drift_attr.schema import AnomalyEvent, AnomalyGroup, event_to_dict, item_to_dict
from drift_attr.store import (
    get_input_binding, get_rule_snapshot, get_run, save_run,
)
from drift_attr.timeseries import BuildResult, build_series, series_summary

RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
SNAPSHOT_DIR = ".input-snapshots"


@dataclass
class ValidationResult:
    config: AppConfig
    dataset: Dataset
    build: BuildResult

    def summary(self) -> dict[str, Any]:
        return {
            "config_path": str(self.config.source_path),
            "config_fingerprint": self.config.fingerprint,
            "dataset_path": self.dataset.path,
            "dataset_fingerprint": self.dataset.fingerprint,
            "row_count": self.dataset.row_count,
            **series_summary(self.build.series),
            "detector_count": len(self.config.detectors),
            "attribution_dimensions": list(self.config.attribution.dimensions),
            "warnings": self.dataset.warnings + self.build.warnings,
        }


@dataclass
class AnalysisResult:
    config: AppConfig
    dataset: Dataset
    build: BuildResult
    events: list[AnomalyEvent]
    groups: list[AnomalyGroup]
    report: dict[str, Any]
    run_id: str
    run_dir: Path
    database_path: Path
    artifacts: list[dict[str, str]]
    replay_of: str | None = None
    input_policy: str = "reference"


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        fail("E_ARGUMENT", "run_id 只能包含字母、数字、点、下划线和连字符，且必须以字母或数字开头")
    return run_id


def new_run_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S")
    return f"run-{stamp}-{uuid.uuid4().hex[:6]}"


def iso_utc(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_sort_key(event: AnomalyEvent) -> tuple[Any, ...]:
    return (event.date, event.metric, event.dimension_name, event.dimension_value,
            event.detector, event.rule_name)


def _run_detectors(config: AppConfig, build: BuildResult, run_id: str) -> list[AnomalyEvent]:
    events: list[AnomalyEvent] = []
    for series in build.series:
        for rule in config.detectors:
            if series.metric not in rule.metrics:
                continue
            if rule.dimension_names and series.dimension_name not in rule.dimension_names:
                continue
            if rule.dimension_values and series.dimension_value not in rule.dimension_values:
                continue
            for detection in get_detector(rule.detector_type).detect(series, rule.params):
                group_id = make_group_id(
                    run_id, series.metric, series.dimension_name,
                    series.dimension_value, detection.date,
                )
                events.append(AnomalyEvent(
                    event_id=make_event_id(
                        run_id, series.metric, series.dimension_name,
                        series.dimension_value, detection.date,
                        rule.detector_type, rule.name,
                    ),
                    group_id=group_id, run_id=run_id, metric=series.metric,
                    dimension_name=series.dimension_name,
                    dimension_value=series.dimension_value, date=detection.date,
                    detector=rule.detector_type, rule_name=rule.name,
                    observed=detection.observed, baseline=detection.baseline,
                    score=detection.score, severity=rule.severity,
                    evidence=detection.evidence, window_start=detection.window_start,
                    window_end=detection.window_end,
                ))
    return sorted(events, key=_event_sort_key)


def validate(config_path: str | Path, base_dir: str | Path | None = None) -> ValidationResult:
    base = Path(base_dir or Path.cwd())
    config = load_config(config_path)
    dataset = load_dataset(config.dataset, base)
    build = build_series(dataset, config.series)
    for rule in config.detectors:
        get_detector(rule.detector_type)
    return ValidationResult(config=config, dataset=dataset, build=build)


def _artifact_manifest(run_dir: Path, base: Path) -> list[dict[str, str]]:
    entries = [
        ("report_json", "json", "report.json"), ("report_markdown", "markdown", "report.md"),
        ("report_html", "html", "report.html"), ("attribution_json", "json", "attribution.json"),
        ("rule_snapshot", "yaml", "rules.snapshot.yaml"),
    ]
    return [
        {"kind": kind, "format": fmt, "path": display_path(run_dir / name, base)}
        for kind, fmt, name in entries
    ]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _snapshot_path(output_root: Path, run_id: str, dataset: Dataset) -> Path:
    suffix = Path(dataset.path).suffix or ".csv"
    return output_root / SNAPSHOT_DIR / f"{run_id}{suffix}"


def seal_input_snapshot(
    output_root: Path, run_id: str, dataset: Dataset, base: Path
) -> dict[str, str]:
    """Atomically seal the exact bytes the loader parsed into the run's snapshot.

    The parser and this writer consume the same one-time read (``dataset.raw_bytes``),
    so a source file changing after load can never bind Q bytes to P results.
    """
    target = _snapshot_path(output_root, run_id, dataset)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{run_id}-", dir=target.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(dataset.raw_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        sealed = target.read_bytes()
    except OSError as exc:
        raise DriftError(
            "E_REPLAY_SNAPSHOT", f"封存输入快照失败: {exc}", path=str(target)
        ) from exc
    sealed_sha = hashlib.sha256(sealed).hexdigest()
    if sealed_sha != dataset.raw_sha256:
        raise DriftError(
            "E_REPLAY_SNAPSHOT", "封存快照与载入字节不一致", path=str(target)
        )
    return {
        "snapshot_path": display_path(target, base),
        "snapshot_relative": target.relative_to(output_root).as_posix(),
    }


def _write_artifacts(
    run_dir: Path, report: dict[str, Any], attribution: dict[str, Any],
    rule_text: str, base: Path,
) -> list[dict[str, str]]:
    run_dir.mkdir(parents=True, exist_ok=True)
    payloads = [
        ("report_json", "json", "report.json",
         json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"),
        ("report_markdown", "markdown", "report.md", report_to_markdown(report)),
        ("report_html", "html", "report.html", report_to_html(report)),
        ("attribution_json", "json", "attribution.json",
         json.dumps(attribution, ensure_ascii=False, indent=2, sort_keys=True) + "\n"),
        ("rule_snapshot", "yaml", "rules.snapshot.yaml", rule_text),
    ]
    manifest: list[dict[str, str]] = []
    for kind, fmt, name, content in payloads:
        path = run_dir / name
        path.write_text(content, encoding="utf-8")
        manifest.append({"kind": kind, "format": fmt,
                         "path": display_path(path, base), "sha256": _sha256(content)})
    return manifest


def _input_binding(
    dataset: Dataset, policy: str, base: Path, output_root: Path,
    run_id: str, replay_of: str | None,
) -> dict[str, Any]:
    binding: dict[str, Any] = {
        "input_policy": policy,
        "source_path": display_path(dataset.resolved_path, base),
        "raw_sha256": dataset.raw_sha256,
        "dataset_fingerprint": dataset.fingerprint,
        "format": dataset.format,
        "relative_path": dataset.path,
        "snapshot_path": None,
        "snapshot_relative": None,
        "replay_of": replay_of,
    }
    if policy == "snapshot":
        sealed = seal_input_snapshot(output_root, run_id, dataset, base)
        binding["snapshot_path"] = sealed["snapshot_path"]
        binding["snapshot_relative"] = sealed["snapshot_relative"]
    return binding


def _persist_analysis(
    *, config: AppConfig, dataset: Dataset, build: BuildResult,
    run_id: str, base: Path, started: str, finished: str,
    replay_of: str | None, binding_policy: str,
) -> AnalysisResult:
    events = _run_detectors(config, build, run_id)
    groups = group_events(events)
    attribute_groups(groups, build.series, build.timeline, config.attribution)
    output_root = resolve_path(config.output.directory, base)
    run_dir = output_root / run_id
    database_path = output_root / config.output.database
    report = build_report(
        run_id=run_id, config=config, dataset=dataset, build=build, groups=groups,
        events=events, started_at=started, finished_at=finished, base_dir=base,
        artifacts=_artifact_manifest(run_dir, base),
    )
    try:
        artifacts = _write_artifacts(
            run_dir, report, attribution_document(report), config.text, base
        )
    except OSError as exc:
        raise DriftError("E_ANALYSIS", f"写入报告失败: {exc}", path=str(run_dir)) from exc
    # Snapshot sealing happens after the analysis succeeds but before any ledger
    # write; a failure here leaves no half-finished run row behind.
    input_binding = _input_binding(
        dataset, binding_policy, base, output_root, run_id, replay_of
    )
    attr_items = [item for group in groups for item in group.attribution_items]
    run_record = {
        "run_id": run_id, "started_at": started, "finished_at": finished,
        "status": "success", "config_path": display_path(config.source_path, base),
        "config_fingerprint": config.fingerprint, "dataset_path": dataset.path,
        "dataset_fingerprint": dataset.fingerprint, "rule_fingerprint": config.fingerprint,
        "output_dir": display_path(output_root, base), "series_count": len(build.series),
        "event_count": len(events), "anomaly_count": len(groups),
        "attribution_count": len(attr_items), "error_summary": None,
        "replay_of": replay_of, "input_policy": binding_policy,
    }
    save_run(
        database_path, run=run_record,
        rule_snapshot={"fingerprint": config.fingerprint, "content": config.text},
        dataset=dataset_summary(dataset),
        events=[event_to_dict(event) for event in events],
        attribution_items=[item_to_dict(item) for item in attr_items],
        artifacts=artifacts, input_binding=input_binding,
    )
    return AnalysisResult(
        config=config, dataset=dataset, build=build, events=events, groups=groups,
        report=report, run_id=run_id, run_dir=run_dir,
        database_path=database_path, artifacts=artifacts,
        replay_of=replay_of, input_policy=binding_policy,
    )


def analyze(
    config_path: str | Path, *, run_id: str | None = None,
    base_dir: str | Path | None = None, now: datetime | None = None,
) -> AnalysisResult:
    base = Path(base_dir or Path.cwd()).resolve()
    started = iso_utc(now)
    config = load_config(config_path)
    dataset = load_dataset(config.dataset, base)
    build = build_series(dataset, config.series)
    for rule in config.detectors:
        get_detector(rule.detector_type)
    chosen_run = validate_run_id(run_id) if run_id else new_run_id(now)
    finished = iso_utc(now)
    return _persist_analysis(
        config=config, dataset=dataset, build=build, run_id=chosen_run,
        base=base, started=started, finished=finished, replay_of=None,
        binding_policy=config.replay.input_policy,
    )


def _load_replay_config(rule_text: str, nominal_config_path: str, base: Path) -> AppConfig:
    config = parse_config_text(rule_text, base / nominal_config_path)
    for rule in config.detectors:
        get_detector(rule.detector_type)
    return config


def _replay_input(
    binding: dict[str, Any], output_root: Path, base: Path,
    input_override: str | Path | None,
) -> tuple[bytes, Path]:
    """Resolve and verify replay bytes before any child run is created."""
    policy = binding["input_policy"]
    if policy == "snapshot":
        if input_override is not None:
            fail(
                "E_ARGUMENT",
                "snapshot 策略的重放只读取父运行封存字节，不接受 --input-override",
                field="--input-override",
            )
        relative = binding.get("snapshot_relative")
        if not relative:
            fail(
                "E_REPLAY_BINDING_MISSING", "父运行未登记输入快照位置",
                path=str(binding.get("snapshot_path") or ""),
            )
        snapshot = (output_root / relative).resolve()
        if not snapshot.is_relative_to(output_root.resolve()):
            fail("E_REPLAY_SNAPSHOT", "快照路径越界", path=relative)
        if not snapshot.is_file():
            fail(
                "E_REPLAY_INPUT_MISSING", "父运行输入快照不存在或已损坏",
                path=relative,
            )
        try:
            raw = snapshot.read_bytes()
        except OSError as exc:
            raise DriftError(
                "E_REPLAY_INPUT_MISSING", f"输入快照读取失败: {exc}",
                path=relative,
            ) from exc
        resolved = snapshot
    else:
        source = binding.get("source_path") or binding.get("relative_path")
        location = Path(input_override) if input_override is not None else (
            base / str(source)
        )
        if not location.is_absolute():
            location = (base / location).resolve()
        if not location.is_file():
            fail(
                "E_REPLAY_INPUT_MISSING", "父运行原始输入文件不存在",
                path=str(location),
            )
        try:
            raw = location.read_bytes()
        except OSError as exc:
            raise DriftError(
                "E_REPLAY_INPUT_MISSING", f"原始输入读取失败: {exc}",
                path=str(location),
            ) from exc
        resolved = location
    raw_sha = hashlib.sha256(raw).hexdigest()
    if raw_sha != binding["raw_sha256"]:
        fail(
            "E_REPLAY_INPUT_MISMATCH",
            "输入原始字节 SHA256 与父运行不一致，无法精确重放",
            path=str(resolved),
        )
    return raw, resolved


def replay_run(
    database_path: str | Path, parent_run_id: str, *,
    run_id: str | None = None, base_dir: str | Path | None = None,
    now: datetime | None = None, input_override: str | Path | None = None,
) -> AnalysisResult:
    """Re-run a parent run's rules against its bound input, never overwriting it."""
    base = Path(base_dir or Path.cwd()).resolve()
    database = Path(database_path)
    parent = get_run(database, parent_run_id)
    if parent is None:
        fail("E_RUN_NOT_FOUND", f"台账中不存在运行: {parent_run_id}", path=str(database))
    binding = get_input_binding(database, parent_run_id)
    if binding is None:
        fail(
            "E_REPLAY_BINDING_MISSING",
            f"运行 {parent_run_id} 缺少输入绑定，无法重放",
            path=str(database),
        )
    rule_text = get_rule_snapshot(database, parent_run_id)
    if rule_text is None:
        fail(
            "E_REPLAY_BINDING_MISSING",
            f"运行 {parent_run_id} 缺少规则快照，无法重放",
            path=str(database),
        )
    config = _load_replay_config(rule_text, parent["config_path"], base)
    output_root = resolve_path(config.output.directory, base)
    # All input identity checks happen before a child run, report directory or
    # ledger row can exist: failures leave no half-finished child behind.
    raw, resolved = _replay_input(binding, output_root, base, input_override)
    nominal_path = binding["relative_path"] or parent["dataset_path"]
    replay_cfg = replace(config.dataset, path=nominal_path)
    dataset = parse_dataset_bytes(
        raw, replay_cfg, nominal_path, resolved_path=resolved
    )
    if dataset.fingerprint != binding["dataset_fingerprint"]:
        fail(
            "E_REPLAY_INPUT_MISMATCH",
            "规范化数据指纹与父运行不一致，无法重放",
            path=str(resolved),
        )
    build = build_series(dataset, config.series)
    chosen_run = validate_run_id(run_id) if run_id else new_run_id(now)
    started = iso_utc(now)
    finished = iso_utc(now)
    return _persist_analysis(
        config=config, dataset=dataset, build=build, run_id=chosen_run,
        base=base, started=started, finished=finished,
        replay_of=parent_run_id, binding_policy=binding["input_policy"],
    )
