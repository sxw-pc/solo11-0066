"""Shared tiny builders for tests (no network, no user-home writes)."""

from __future__ import annotations

import shutil
from datetime import date, timedelta
from pathlib import Path

import yaml

from drift_attr.schema import AnomalyEvent, AnomalyGroup, Point, Series

ROOT = Path(__file__).resolve().parents[1]
LONG_HEADER = "date,metric,dimension_name,dimension_value,value\n"


def write_csv(path: Path, body: str, header: str = LONG_HEADER) -> Path:
    path.write_text(header + body, encoding="utf-8")
    return path


def config_dict(
    detectors: list[dict],
    *,
    dataset_path: str = "data.csv",
    dataset_extra: dict | None = None,
    attribution: dict | None = None,
    series: dict | None = None,
    output: dict | None = None,
) -> dict:
    dataset = {
        "path": dataset_path,
        "format": "long",
        "date_field": "date",
        "metric_field": "metric",
        "dimension_name_field": "dimension_name",
        "dimension_value_field": "dimension_value",
        "value_field": "value",
    }
    dataset.update(dataset_extra or {})
    return {
        "version": 1,
        "dataset": dataset,
        "series": {"granularity": "day", **(
            series or {}
        )},
        "detection": {"detectors": detectors},
        "attribution": attribution or {
            "dimensions": ["channel"],
            "top_n": 3,
            "baseline_window": 2,
            "comparison_window": 2,
        },
        "output": output or {"directory": "output"},
    }


def write_config(path: Path, payload: dict) -> Path:
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def make_series(
    values: list[float],
    *,
    metric: str = "orders",
    dimension_name: str = "channel",
    dimension_value: str = "region_alpha",
    start: date = date(2025, 1, 1),
) -> Series:
    return Series(
        metric=metric,
        dimension_name=dimension_name,
        dimension_value=dimension_value,
        points=[
            Point(start + timedelta(days=index), value)
            for index, value in enumerate(values)
        ],
    )


def make_event(
    *,
    day: date = date(2025, 1, 3),
    metric: str = "orders",
    dimension_name: str = "channel",
    dimension_value: str = "region_alpha",
    run_id: str = "run-test",
) -> AnomalyEvent:
    return AnomalyEvent(
        event_id="evt_demo",
        group_id="grp_demo",
        run_id=run_id,
        metric=metric,
        dimension_name=dimension_name,
        dimension_value=dimension_value,
        date=day,
        detector="threshold",
        rule_name="rule_demo",
        observed=20,
        baseline=45,
        score=-25,
        severity="high",
        evidence={"summary": "demo evidence"},
        window_start=day,
        window_end=day,
    )


def make_group(event: AnomalyEvent | None = None) -> AnomalyGroup:
    item = event or make_event()
    return AnomalyGroup(
        group_id=item.group_id,
        run_id=item.run_id,
        metric=item.metric,
        dimension_name=item.dimension_name,
        dimension_value=item.dimension_value,
        date=item.date,
        severity=item.severity,
        events=[item],
    )


def demo_project(tmp_path: Path) -> Path:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    shutil.copy(ROOT / "fixtures" / "demo_metrics.csv", fixtures / "demo_metrics.csv")
    shutil.copy(ROOT / "fixtures" / "rules.yaml", fixtures / "rules.yaml")
    return tmp_path
