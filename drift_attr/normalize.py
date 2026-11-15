"""Stable identifiers, event de-duplication and merged anomaly groups."""

from __future__ import annotations

import hashlib
from datetime import date

from drift_attr.schema import AnomalyEvent, AnomalyGroup, SEVERITY_RANK


def _digest(*parts: object) -> str:
    text = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


def make_group_id(
    run_id: str, metric: str, dimension_name: str, dimension_value: str, day: date
) -> str:
    return "grp_" + _digest(run_id, metric, dimension_name, dimension_value, day.isoformat())


def make_event_id(
    run_id: str,
    metric: str,
    dimension_name: str,
    dimension_value: str,
    day: date,
    detector: str,
    rule_name: str,
) -> str:
    return "evt_" + _digest(
        run_id, metric, dimension_name, dimension_value,
        day.isoformat(), detector, rule_name,
    )


def make_item_id(
    run_id: str,
    group_id: str,
    attribution_dimension: str,
    dimension_value: str,
    rank: int,
) -> str:
    return "attr_" + _digest(
        run_id, group_id, attribution_dimension, dimension_value, rank
    )


def _event_sort_key(event: AnomalyEvent) -> tuple[object, ...]:
    return (
        event.date, event.metric, event.dimension_name, event.dimension_value,
        event.detector, event.rule_name,
    )


def group_events(events: list[AnomalyEvent]) -> list[AnomalyGroup]:
    """Merge detector hits at the same metric/dimension/date location.

    Raw events stay intact inside the group so evidence is never discarded.
    """
    buckets: dict[tuple[str, str, str, date], list[AnomalyEvent]] = {}
    for event in sorted(events, key=_event_sort_key):
        buckets.setdefault(event.location, []).append(event)
    groups: list[AnomalyGroup] = []
    for location in sorted(
        buckets,
        key=lambda item: (item[3], item[0], item[1], item[2]),
    ):
        metric, dimension_name, dimension_value, day = location
        members = buckets[location]
        severity = max((event.severity for event in members), key=lambda item: SEVERITY_RANK[item])
        group_id = members[0].group_id
        if any(event.group_id != group_id for event in members):
            raise ValueError("events at one location must share a group id")
        groups.append(
            AnomalyGroup(
                group_id=group_id,
                run_id=members[0].run_id,
                metric=metric,
                dimension_name=dimension_name,
                dimension_value=dimension_value,
                date=day,
                severity=severity,
                events=members,
            )
        )
    return groups


def apply_group_narrative(group: AnomalyGroup) -> None:
    detector_text = "、".join(group.detectors)
    first = group.events[0]
    parts = [
        f"{group.date.isoformat()} 的 {group.metric} "
        f"（{group.dimension_name}={group.dimension_value}）出现异常，"
        f"命中检测器：{detector_text}。"
    ]
    if first.evidence.get("summary"):
        parts.append(str(first.evidence["summary"]) + "。")
    comparable_items = [item for item in group.attribution_items if item.status == "ok"]
    if comparable_items:
        top = sorted(
            comparable_items,
            key=lambda item: (item.attribution_dimension, item.rank),
        )[0]
        rate = "不适用（总变化为 0）"
        if top.contribution_rate is not None:
            rate = f"{top.contribution_rate * 100:.2f}%"
        label = "其他维度合计" if top.is_other else top.dimension_value
        parts.append(
            f"按 {top.attribution_dimension} 归因，{label} 的贡献额为 "
            f"{top.contribution:+.2f}，占总变化 {rate}。"
        )
    elif group.attribution_items:
        reasons = sorted(
            {item.status_reason for item in group.attribution_items if item.status_reason}
        )
        if reasons:
            parts.append("归因维度均不可比：" + "；".join(reasons) + "。")
    group.narrative = " ".join(parts)
