"""Deterministic demo fixtures and one-command demo initialization."""
from __future__ import annotations
import csv
import io
from datetime import date, timedelta
from pathlib import Path
RULES_YAML = """version: 1
dataset:
  path: fixtures/demo_metrics.csv
  format: long
  encoding: utf-8
  duplicate_policy: warn
  empty_value: error
  date_field: date
  metric_field: metric
  dimension_name_field: dimension_name
  dimension_value_field: dimension_value
  value_field: value
series:
  granularity: day
  aggregate: sum
  fill_missing: zero
  week_start: monday
detection:
  detectors:
    - name: low_orders_store
      type: threshold
      metrics: [orders]
      dimension_names: [channel]
      dimension_values: [channel_store]
      severity: high
      direction: below
      lower: 45
    - name: gmv_day_over_day_web
      type: delta
      metrics: [gmv]
      dimension_values: [channel_web]
      severity: medium
      comparison: previous
      mode: relative
      threshold: 0.25
      direction: both
    - name: visits_robust_z_app
      type: robust_z
      metrics: [visits]
      dimension_values: [channel_app]
      severity: medium
      history: 21
      threshold: 3.5
      mad_zero: skip
      direction: both
    - name: sessions_level_shift_web
      type: changepoint
      metrics: [sessions]
      dimension_values: [channel_web]
      severity: high
      pre_window: 7
      post_window: 7
      threshold: 4.0
      min_scale: 1.0
      direction: increase
attribution:
  dimensions: [channel]
  top_n: 2
  baseline_window: 7
  comparison_window: 7
  other_label: "其他"
output:
  directory: output
  database: drift_attr.sqlite3
"""
WIDE_CSV = """date,region,visits,orders
2025-01-06,region_alpha,120,18
2025-01-07,region_alpha,125,19
2025-01-06,region_beta,90,14
2025-01-07,region_beta,92,15
2025-01-06,region_gamma,70,11
2025-01-07,region_gamma,72,12
"""
CHANNELS = ("channel_web", "channel_app", "channel_store")
METRIC_PROFILE = {
    "channel_web": {
        "orders": (120, 1, 1),
        "gmv": (2000, 2, 25),
        "visits": (600, 3, 5),
        "sessions": (700, 4, 2),
    },
    "channel_app": {
        "orders": (90, 5, 1),
        "gmv": (1500, 6, 20),
        "visits": (800, 7, 2),
        "sessions": (520, 8, 2),
    },
    "channel_store": {
        "orders": (70, 9, 2),
        "gmv": (1000, 10, 15),
        "visits": (400, 11, 4),
        "sessions": (300, 12, 2),
    },
}
START = date(2025, 3, 1)
DAYS = 35
def _wave(index: int, seed: int, amplitude: int) -> int:
    return (((index + seed) * 7) % 9 - 4) * amplitude
def _value(channel: str, metric: str, index: int) -> int:
    base, seed, amplitude = METRIC_PROFILE[channel][metric]
    value = base + _wave(index, seed, amplitude)
    if channel == "channel_web" and metric == "sessions" and index >= 26:
        value += 200
    if channel == "channel_store" and metric == "orders" and index == 30:
        value = 20
    if channel == "channel_app" and metric == "visits" and index == 32:
        value = 1700
    if channel == "channel_web" and metric == "gmv" and index == 34:
        value = 3000
    return value
def demo_metric_rows() -> list[tuple[str, str, str, str, int]]:
    rows: list[tuple[str, str, str, str, int]] = []
    for index in range(DAYS):
        day = START + timedelta(days=index)
        for channel in CHANNELS:
            for metric in ("orders", "gmv", "visits", "sessions"):
                rows.append(
                    (
                        day.isoformat(), metric, "channel", channel,
                        _value(channel, metric, index),
                    )
                )
    return rows
def demo_csv_text() -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["date", "metric", "dimension_name", "dimension_value", "value"])
    writer.writerows(demo_metric_rows())
    return buffer.getvalue()
def write_demo_fixtures(root: str | Path, *, force: bool = False) -> dict[str, str]:
    """Write demo data and the canonical rule file under ``root``."""
    base = Path(root)
    fixtures = base / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    files = {
        "metrics": fixtures / "demo_metrics.csv",
        "wide": fixtures / "demo_wide_metrics.csv",
        "rules": fixtures / "rules.yaml",
    }
    payloads = {
        "metrics": demo_csv_text(),
        "wide": WIDE_CSV,
        "rules": RULES_YAML,
    }
    written: dict[str, str] = {}
    for key, path in files.items():
        if force or not path.exists():
            path.write_text(payloads[key], encoding="utf-8", newline="")
        written[key] = str(path)
    return written
def ensure_demo(root: str | Path) -> dict[str, str]:
    return write_demo_fixtures(root, force=False)
