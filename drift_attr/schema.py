"""Small, dependency-free data structures shared by the analysis stages."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from typing import Any
SEVERITIES = ("low", "medium", "high", "critical")
SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}
def stable_float(value: float | int | None) -> float | None:
    """Round for JSON and remove negative zero without changing semantics."""
    if value is None:
        return None
    rounded = round(float(value), 6)
    return 0.0 if rounded == 0 else rounded
@dataclass(frozen=True)
class Point:
    date: date
    value: float
    imputed: bool = False
@dataclass
class Series:
    metric: str
    dimension_name: str
    dimension_value: str
    points: list[Point] = field(default_factory=list)
    fill_count: int = 0
    @property
    def key(self) -> tuple[str, str, str]:
        return (self.metric, self.dimension_name, self.dimension_value)
    @property
    def label(self) -> str:
        return f"{self.metric}/{self.dimension_name}={self.dimension_value}"
@dataclass
class Detection:
    """One detector hit before run identifiers are attached."""
    date: date
    observed: float
    baseline: float
    score: float
    evidence: dict[str, Any]
    window_start: date
    window_end: date
@dataclass
class AnomalyEvent:
    event_id: str
    group_id: str
    run_id: str
    metric: str
    dimension_name: str
    dimension_value: str
    date: date
    detector: str
    rule_name: str
    observed: float
    baseline: float
    score: float
    severity: str
    evidence: dict[str, Any]
    window_start: date
    window_end: date
    @property
    def location(self) -> tuple[str, str, str, date]:
        return (
            self.metric,
            self.dimension_name,
            self.dimension_value,
            self.date,
        )
@dataclass
class AttributionItem:
    item_id: str
    group_id: str
    event_id: str
    attribution_dimension: str
    dimension_value: str
    current_value: float
    baseline_value: float
    contribution: float
    contribution_rate: float | None
    rank: int
    is_other: bool
    formula: str
    explanation: str
@dataclass
class AnomalyGroup:
    group_id: str
    run_id: str
    metric: str
    dimension_name: str
    dimension_value: str
    date: date
    severity: str
    events: list[AnomalyEvent]
    attribution_items: list[AttributionItem] = field(default_factory=list)
    narrative: str = ""
    @property
    def detectors(self) -> list[str]:
        return sorted({event.detector for event in self.events})
    @property
    def rule_names(self) -> list[str]:
        return sorted({event.rule_name for event in self.events})
    @property
    def window_start(self) -> date:
        return min(event.window_start for event in self.events)
    @property
    def window_end(self) -> date:
        return max(event.window_end for event in self.events)
def event_to_dict(event: AnomalyEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "group_id": event.group_id,
        "run_id": event.run_id,
        "metric": event.metric,
        "dimension_name": event.dimension_name,
        "dimension_value": event.dimension_value,
        "date": event.date.isoformat(),
        "detector": event.detector,
        "rule_name": event.rule_name,
        "observed": stable_float(event.observed),
        "baseline": stable_float(event.baseline),
        "score": stable_float(event.score),
        "severity": event.severity,
        "evidence": event.evidence,
        "window_start": event.window_start.isoformat(),
        "window_end": event.window_end.isoformat(),
    }
def item_to_dict(item: AttributionItem) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "group_id": item.group_id,
        "event_id": item.event_id,
        "attribution_dimension": item.attribution_dimension,
        "dimension_value": item.dimension_value,
        "current_value": stable_float(item.current_value),
        "baseline_value": stable_float(item.baseline_value),
        "contribution": stable_float(item.contribution),
        "contribution_rate": stable_float(item.contribution_rate),
        "rank": item.rank,
        "is_other": item.is_other,
        "formula": item.formula,
        "explanation": item.explanation,
    }
