"""End-to-end validation, detection, attribution, rendering and ledger writes."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from drift_attr.attribution import attribute_groups
from drift_attr.config import AppConfig, load_config
from drift_attr.dataset import Dataset, dataset_summary, load_dataset, resolve_path
from drift_attr.detectors import get_detector
from drift_attr.errors import DriftError, fail
from drift_attr.html_report import write_html
from drift_attr.normalize import group_events, make_event_id, make_group_id
from drift_attr.report import (
    ChunkedTextWriter,
    build_report,
    display_path,
    make_report_context,
    write_attribution_json,
    write_markdown,
    write_report_json,
)
from drift_attr.schema import AnomalyEvent, AnomalyGroup, event_to_dict, item_to_dict
from drift_attr.store import save_run
from drift_attr.timeseries import BuildResult, build_series, series_summary

RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

#: Temporary suffix used while an artifact is still being rendered.
STAGING_SUFFIX = ".partial"


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
    run_id: str
    run_dir: Path
    database_path: Path
    artifacts: list[dict[str, str]]
    # Lazy materialization: the full report dict is only rebuilt when a caller
    # explicitly asks for ``result.report``; analyze() never loads it back.
    _report_loader: Callable[[], dict[str, Any]] | None = field(
        default=None, repr=False, compare=False
    )
    _materialized_report: dict[str, Any] | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def report(self) -> dict[str, Any]:
        if self._materialized_report is None:
            if self._report_loader is None:
                raise RuntimeError("report loader is not configured")
            self._materialized_report = self._report_loader()
        return self._materialized_report


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


def _open_chunked_writer(handle: TextIO) -> ChunkedTextWriter:
    """Indirection point so tests can inject failures mid-render."""
    return ChunkedTextWriter(handle)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_rule_snapshot(writer: ChunkedTextWriter, rule_text: str) -> None:
    block_size = 64 * 1024
    for start in range(0, len(rule_text), block_size):
        writer.write(rule_text[start : start + block_size])


def _write_artifacts(
    run_dir: Path, ctx: Any, rule_text: str, base: Path,
) -> list[dict[str, str]]:
    """Render all artifacts through bounded buffers into staging files.

    Every artifact is first written to ``<name>.partial`` and only renamed to
    its final name once rendering finished and the on-disk hash matches the
    streaming digest.  A failure at any point removes every staging/final file
    created by this call, so truncated content can never be registered or
    exposed as a successful artifact.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    spec = [
        ("report_json", "json", "report.json", write_report_json),
        ("report_markdown", "markdown", "report.md", write_markdown),
        ("report_html", "html", "report.html", write_html),
        ("attribution_json", "json", "attribution.json", write_attribution_json),
        ("rule_snapshot", "yaml", "rules.snapshot.yaml", None),
    ]
    final_names = [name for _kind, _fmt, name, _renderer in spec]
    staged: list[tuple[Path, Path, str]] = []
    manifest: list[dict[str, str]] = []
    try:
        for kind, fmt, name, renderer in spec:
            staging = run_dir / (name + STAGING_SUFFIX)
            with staging.open("w", encoding="utf-8", newline="") as handle:
                writer = _open_chunked_writer(handle)
                if renderer is None:
                    _write_rule_snapshot(writer, rule_text)
                else:
                    renderer(writer, ctx)
                writer.close()
                digest_hex = writer.sha256_hex
            staged.append((staging, run_dir / name, digest_hex))

        expected = {final: digest for _staging, final, digest in staged}
        for staging, final_path, _digest in staged:
            staging.replace(final_path)
        for kind, fmt, name, _renderer in spec:
            final_path = run_dir / name
            actual_digest = _hash_file(final_path)
            if actual_digest != expected[final_path]:
                raise DriftError(
                    "E_ANALYSIS", "产物哈希校验失败", path=str(final_path)
                )
            manifest.append({
                "kind": kind, "format": fmt,
                "path": display_path(final_path, base), "sha256": actual_digest,
            })
    except (OSError, DriftError):
        touched = set(final_names) | {name + STAGING_SUFFIX for name in final_names}
        for name in touched:
            try:
                (run_dir / name).unlink()
            except FileNotFoundError:
                pass
        raise
    return manifest


def analyze(
    config_path: str | Path, *, run_id: str | None = None,
    base_dir: str | Path | None = None, now: datetime | None = None,
) -> AnalysisResult:
    base = Path(base_dir or Path.cwd()).resolve()
    started = iso_utc(now)
    validated = validate(config_path, base)
    config, dataset, build = validated.config, validated.dataset, validated.build
    chosen_run = validate_run_id(run_id) if run_id else new_run_id(now)
    events = _run_detectors(config, build, chosen_run)
    groups = group_events(events)
    attribute_groups(groups, build.series, build.timeline, config.attribution)
    output_root = resolve_path(config.output.directory, base)
    run_dir = output_root / chosen_run
    database_path = output_root / config.output.database
    finished = iso_utc(now)
    artifact_paths = _artifact_manifest(run_dir, base)
    ctx = make_report_context(
        run_id=chosen_run, config=config, dataset=dataset, build=build,
        groups=groups, events=events, started_at=started, finished_at=finished,
        base_dir=base, artifacts=artifact_paths,
    )
    try:
        artifacts = _write_artifacts(run_dir, ctx, config.text, base)
    except DriftError:
        # Hash-verification failures already carry the E_ANALYSIS code and the
        # staging cleanup has run inside _write_artifacts.
        raise
    except OSError as exc:
        raise DriftError("E_ANALYSIS", f"写入报告失败: {exc}", path=str(run_dir)) from exc
    attr_items = [item for group in groups for item in group.attribution_items]
    run_record = {
        "run_id": chosen_run, "started_at": started, "finished_at": finished,
        "status": "success", "config_path": display_path(config.source_path, base),
        "config_fingerprint": config.fingerprint, "dataset_path": dataset.path,
        "dataset_fingerprint": dataset.fingerprint, "rule_fingerprint": config.fingerprint,
        "output_dir": display_path(output_root, base), "series_count": len(build.series),
        "event_count": len(events), "anomaly_count": len(groups),
        "attribution_count": len(attr_items), "error_summary": None,
    }
    save_run(
        database_path, run=run_record,
        rule_snapshot={"fingerprint": config.fingerprint, "content": config.text},
        dataset=dataset_summary(dataset),
        events=[event_to_dict(event) for event in events],
        attribution_items=[item_to_dict(item) for item in attr_items],
        artifacts=artifacts,
    )

    def _load_report() -> dict[str, Any]:
        return build_report(
            run_id=chosen_run, config=config, dataset=dataset, build=build,
            groups=groups, events=events, started_at=started, finished_at=finished,
            base_dir=base, artifacts=artifact_paths,
        )

    return AnalysisResult(
        config=config, dataset=dataset, build=build, events=events, groups=groups,
        run_id=chosen_run, run_dir=run_dir, database_path=database_path,
        artifacts=artifacts, _report_loader=_load_report,
    )
