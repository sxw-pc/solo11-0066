"""Group-level keyset pagination tests for the run detail page.

The detail view must page by whole anomaly locations (groups), never by raw
event rows: every page carries all detector events and all attribution items of
its groups, reads scale with the page size rather than the run size, and cursors
are bound to the run generation so a rewritten run_id restarts paging cleanly.
"""

from __future__ import annotations

import re
import sqlite3
import time

from drift_attr import store
from drift_attr.web import create_app
from drift_attr.web.app import PAGE_SIZE, _group_view
import itsdangerous.timed as itsdangerous_timed

RUN_ID = "run-page"


def _run_record(run_id: str = RUN_ID, **overrides) -> dict:
    record = {
        "run_id": run_id, "started_at": "2025-01-01T00:00:00Z",
        "finished_at": "2025-01-01T00:00:01Z", "status": "success",
        "config_path": "fixtures/rules.yaml", "config_fingerprint": "cfg-1",
        "dataset_path": "fixtures/data.csv", "dataset_fingerprint": "data-1",
        "rule_fingerprint": "rule-1", "output_dir": "output",
        "series_count": 9, "event_count": 0, "anomaly_count": 0,
        "attribution_count": 0, "error_summary": None,
    }
    record.update(overrides)
    return record


def _event(group_index: int, event_index: int, *, day: str, metric: str,
           dimension_value: str) -> dict:
    group_id = f"grp_{group_index:04d}"
    return {
        "event_id": f"evt_{group_index:04d}_{event_index}",
        "group_id": group_id, "run_id": RUN_ID, "metric": metric,
        "dimension_name": "channel", "dimension_value": dimension_value,
        "date": day, "detector": f"detector_{event_index}",
        "rule_name": f"rule_{event_index}", "observed": 20.0, "baseline": 45.0,
        "score": -25.0, "severity": "high" if event_index else "critical",
        "evidence": {"summary": f"evidence {group_index}-{event_index}"},
        "window_start": day, "window_end": day,
    }


def _item(group_index: int, rank: int, *, dimension: str = "channel") -> dict:
    group_id = f"grp_{group_index:04d}"
    return {
        "item_id": f"attr_{group_index:04d}_{dimension}_{rank}",
        "group_id": group_id, "event_id": f"evt_{group_index:04d}_0",
        "attribution_dimension": dimension,
        "dimension_value": f"value_{rank}",
        "current_value": 40.0, "baseline_value": 60.0, "contribution": -20.0,
        "contribution_rate": 1.0, "rank": rank, "is_other": rank == 0,
        "formula": "demo", "explanation": "demo",
    }


def build_paged_run(db_path, *, groups: int = 120, run_overrides=None,
                    run_id: str = RUN_ID, edge_cases: bool = True
                    ) -> tuple[list[dict], list[dict]]:
    """Create a run with >1 page of groups and deliberate edge cases.

    Layout (``edge_cases`` enabled):
      * every group has two detector/rule events sharing one group_id;
      * groups 10 and 60 have a third detector event;
      * groups 5 and 55 have no attribution items at all;
      * groups 20 and 70 hold 60 items (more than the page size) to prove
        attribution rows are never used as the paging boundary.

    With ``edge_cases=False`` every group is uniform (2 events, 3 items), which
    makes first-page row counts directly comparable across run sizes.
    """
    events: list[dict] = []
    items: list[dict] = []
    for index in range(groups):
        day = f"2025-01-{(index % 28) + 1:02d}"
        metric = f"metric_{index % 3}"
        dimension_value = f"region_{index:04d}"
        event_count = 3 if edge_cases and index in (10, 60) else 2
        for event_index in range(event_count):
            events.append(_event(index, event_index, day=day, metric=metric,
                                 dimension_value=dimension_value))
        if edge_cases and index in (5, 55):
            continue
        item_count = 60 if edge_cases and index in (20, 70) else 3
        for rank in range(item_count):
            items.append(_item(index, rank))

    record = _run_record(
        run_id,
        event_count=len(events),
        anomaly_count=groups,
        attribution_count=len(items),
        **(run_overrides or {}),
    )
    store.save_run(
        db_path,
        run=record,
        rule_snapshot={"fingerprint": "rule-1", "content": "version: 1\n"},
        dataset={
            "path": "fixtures/data.csv", "fingerprint": "data-1",
            "row_count": 1, "format": "long", "warnings": [], "min_value": 1.0,
            "max_value": 2.0,
        },
        events=events,
        attribution_items=items,
        artifacts=[],
    )
    return events, items


def make_client(db_path, tmp_path):
    app = create_app(db_path, tmp_path / "output", tmp_path)
    return app.test_client()


def next_cursor(text: str, run_id: str = RUN_ID) -> str | None:
    # Jinja autoescapes dots in URL attributes as &#46;; normalize entities.
    text = text.replace("&#46;", ".")
    match = re.search(rf'href="/runs/{re.escape(run_id)}\?after=([^"]+)"', text)
    if match is None:
        return None
    return match.group(1).replace("&amp;", "&")


def test_first_page_is_capped_at_page_size_and_uses_run_counter(tmp_path):
    db = tmp_path / "ledger.sqlite3"
    build_paged_run(db, groups=120)
    client = make_client(db, tmp_path)

    response = client.get(f"/runs/{RUN_ID}")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert text.count('class="anomaly-card"') == PAGE_SIZE
    # Total comes from the runs table counter, not from counted event rows.
    assert "第 1–50 个，共 120 个异常位置" in text
    assert "加载下一页（第 2 页）" in text
    assert "本次运行没有异常" not in text


def _decode_payload(client, token):
    from itsdangerous import URLSafeTimedSerializer

    serializer = URLSafeTimedSerializer(
        client.application.config["CURSOR_SECRET"], salt="run-page-cursor"
    )
    return serializer.loads(token)


def test_walk_every_page_keeps_groups_whole_and_in_order(tmp_path):
    db = tmp_path / "ledger.sqlite3"
    events, items = build_paged_run(db, groups=120)
    client = make_client(db, tmp_path)
    events_by_group: dict[str, list[dict]] = {}
    items_by_group: dict[str, list[dict]] = {}
    for event in events:
        events_by_group.setdefault(event["group_id"], []).append(event)
    for item in items:
        items_by_group.setdefault(item["group_id"], []).append(item)

    seen_groups: list[str] = []
    cursor = None
    page_count = 0
    last_text = ""
    while True:
        url = f"/runs/{RUN_ID}" + (f"?after={cursor}" if cursor else "")
        response = client.get(url)
        assert response.status_code == 200
        text = response.get_data(as_text=True)
        last_text = text
        page_count += 1

        after = _decode_payload(client, cursor)["k"] if cursor else None
        page = store.get_run_detail_page(
            db, RUN_ID, page_size=PAGE_SIZE, after=tuple(after) if after else None
        )
        page_ids = page["group_ids"]
        assert len(page_ids) <= PAGE_SIZE
        seen_groups.extend(page_ids)

        # Whole groups, never split: all detector events of each page group...
        for group_id in page_ids:
            for event in events_by_group[group_id]:
                assert event["evidence"]["summary"] in text
        # ...and all of its attribution items (the 60-item group included).
        for group_id in page_ids:
            for item in items_by_group.get(group_id, []):
                assert item["dimension_value"] in text

        cursor = next_cursor(text)
        if cursor is None:
            break

    assert page_count == 3  # 50 + 50 + 20
    assert len(seen_groups) == 120 and len(set(seen_groups)) == 120
    canonical = [row["group_id"] for row in store.get_group_page(
        db, RUN_ID, page_size=10_000
    )]
    assert seen_groups == canonical
    # Last page boundary: short page, no next link, full-count footer.
    assert "已展示全部 120 个异常位置（第 3 页，共 3 页）" in last_text
    assert "加载下一页" not in last_text


def _card_containing(text: str, marker: str) -> str:
    start = text.find(marker)
    assert start >= 0, marker
    card_start = text.rfind('<article class="anomaly-card">', 0, start)
    card_end = text.find("</article>", start)
    return text[card_start:card_end]


def test_empty_attribution_group_is_shown_as_empty_not_unloaded(tmp_path):
    db = tmp_path / "ledger.sqlite3"
    build_paged_run(db, groups=60)
    client = make_client(db, tmp_path)

    text = client.get(f"/runs/{RUN_ID}").get_data(as_text=True)
    # Group 5 genuinely has zero attribution rows: the explicit empty note must
    # render inside its card, while the 60-item group 20 shows rows, not the note.
    empty_card = _card_containing(text, "evidence 5-0")
    assert "此事件没有可用的归因维度数据" in empty_card
    assert "<table" not in empty_card
    rich_card = _card_containing(text, "evidence 20-0")
    assert "没有可用的归因维度数据" not in rich_card
    assert rich_card.count("<tr") == 61  # one header row + 60 item rows


def test_same_date_groups_order_by_metric_and_dimension(tmp_path):
    db = tmp_path / "ledger.sqlite3"
    store.save_run(
        db,
        run=_run_record(anomaly_count=3),
        rule_snapshot={"fingerprint": "rule-1", "content": "v\n"},
        dataset={
            "path": "d.csv", "fingerprint": "data-1", "row_count": 1,
            "format": "long", "warnings": [], "min_value": 1.0, "max_value": 2.0,
        },
        events=[
            _event(2, 0, day="2025-02-01", metric="revenue", dimension_value="zeta"),
            _event(0, 0, day="2025-02-01", metric="orders", dimension_value="alpha"),
            _event(1, 0, day="2025-02-01", metric="orders", dimension_value="beta"),
        ],
        attribution_items=[],
        artifacts=[],
    )
    page = store.get_run_detail_page(db, RUN_ID, page_size=50)
    groups = _group_view(page["events"], page["attribution_items"])
    assert [group["dimension_value"] for group in groups] == ["alpha", "beta", "zeta"]


def test_empty_run_renders_empty_state_without_pager(tmp_path):
    db = tmp_path / "ledger.sqlite3"
    store.save_run(
        db,
        run=_run_record(anomaly_count=0, event_count=0, attribution_count=0),
        rule_snapshot={"fingerprint": "rule-1", "content": "v\n"},
        dataset={
            "path": "d.csv", "fingerprint": "data-1", "row_count": 0,
            "format": "long", "warnings": [], "min_value": None, "max_value": None,
        },
        events=[],
        attribution_items=[],
        artifacts=[],
    )
    client = make_client(db, tmp_path)
    response = client.get(f"/runs/{RUN_ID}")
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "本次运行没有异常" in text
    assert "0 个异常位置" in text
    assert "加载下一页" not in text
    assert store.get_run_detail_page(db, RUN_ID, page_size=50) == {
        "group_keys": [], "group_ids": [], "events": [],
        "attribution_items": [], "has_more": False, "next_after": None,
    }


def test_stale_cursor_after_run_rewrite_restarts_at_first_page(tmp_path):
    db = tmp_path / "ledger.sqlite3"
    build_paged_run(db, groups=120)
    client = make_client(db, tmp_path)
    token = next_cursor(client.get(f"/runs/{RUN_ID}").get_data(as_text=True))
    assert token is not None
    second = client.get(f"/runs/{RUN_ID}?after=" + token)
    assert "第 51–100 个" in second.get_data(as_text=True)

    # Rerun the same run_id: save_run atomically replaces every row; the changed
    # finished_at marks the new generation and must invalidate old cursors.
    build_paged_run(
        db, groups=120,
        run_overrides={"finished_at": "2025-02-02T00:00:02Z"},
    )
    stale = client.get(f"/runs/{RUN_ID}?after=" + token)
    text = stale.get_data(as_text=True)
    assert stale.status_code == 200
    assert "翻页游标已过期或该运行已被重跑" in text
    assert "第 1–50 个，共 120 个异常位置" in text
    assert text.count('class="anomaly-card"') == PAGE_SIZE


def test_expired_cursor_is_rejected_and_restarted(tmp_path, monkeypatch):
    db = tmp_path / "ledger.sqlite3"
    build_paged_run(db, groups=120)
    client = make_client(db, tmp_path)
    token = next_cursor(client.get(f"/runs/{RUN_ID}").get_data(as_text=True))
    assert token is not None

    real_time = itsdangerous_timed.time.time
    # itsdangerous timestamps sign time at dumps() and validate at loads().
    monkeypatch.setattr(itsdangerous_timed.time, "time",
                        lambda: real_time() + 24 * 3600)
    response = client.get(f"/runs/{RUN_ID}?after=" + token)
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "翻页游标已过期" in text
    assert "第 1–50 个" in text


def test_garbled_cursor_does_not_crash(tmp_path):
    db = tmp_path / "ledger.sqlite3"
    build_paged_run(db, groups=120)
    client = make_client(db, tmp_path)
    response = client.get(f"/runs/{RUN_ID}?after=not-a-real-token")
    assert response.status_code == 200
    assert "第 1–50 个" in response.get_data(as_text=True)


class _CountingCursor:
    def __init__(self, cursor, connection, sql):
        self._cursor = cursor
        self._connection = connection
        self._sql = sql

    def __iter__(self):
        return iter(self._cursor)

    def fetchall(self):
        rows = self._cursor.fetchall()
        upper = self._sql.upper()
        if "ANOMALY_EVENTS" in upper or "ATTRIBUTION_ITEMS" in upper:
            self._connection.detail_fetched += len(rows)
        self._connection.fetched_rows += len(rows)
        return rows

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _CountingConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fetched_rows = 0
        self.detail_fetched = 0
        self.statements: list[str] = []

    def execute(self, sql, params=()):
        self.statements.append(sql)
        return _CountingCursor(super().execute(sql, params), self, sql)


def _counted_request(db_path, tmp_path, monkeypatch) -> tuple[int, list[str], str]:
    connections: list[_CountingConnection] = []
    real_connect = sqlite3.connect

    def counting_connect(path, *args, **kwargs):
        kwargs["factory"] = _CountingConnection
        conn = real_connect(path, *args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", counting_connect)
    client = make_client(db_path, tmp_path)
    response = client.get(f"/runs/{RUN_ID}")
    assert response.status_code == 200
    detail_sql = [
        sql for conn in connections for sql in conn.statements
        if "anomaly_events" in sql or "attribution_items" in sql
    ]
    return (
        sum(conn.detail_fetched for conn in connections),
        detail_sql,
        response.get_data(as_text=True),
    )


def test_first_page_row_reads_are_invariant_to_run_size(tmp_path, monkeypatch):
    db = tmp_path / "ledger.sqlite3"
    build_paged_run(db, groups=200, edge_cases=False)
    fetched, detail_sql, text = _counted_request(db, tmp_path, monkeypatch)
    assert text.count('class="anomaly-card"') == PAGE_SIZE

    # Bounded by the page: 51 key rows + 2 events/group*50 + 3 items/group*50.
    assert fetched == 51 + PAGE_SIZE * 2 + PAGE_SIZE * 3
    # No full-run SELECT * fetch: every event/item query must be scoped either by
    # a LIMIT (group keys) or by an explicit group_id IN (...) page filter.
    for sql in detail_sql:
        upper = sql.upper()
        assert "LIMIT" in upper or "GROUP_ID IN" in upper

    monkeypatch.undo()
    # A run twice as large must cost exactly the same first-page read budget.
    build_paged_run(db, groups=400, edge_cases=False)
    bigger_fetched, _, bigger_text = _counted_request(db, tmp_path, monkeypatch)
    assert bigger_text.count('class="anomaly-card"') == PAGE_SIZE
    assert bigger_fetched == fetched


def test_paging_queries_use_run_prefixed_indexes_without_full_scan(tmp_path):
    db = tmp_path / "ledger.sqlite3"
    build_paged_run(db, groups=120)
    conn = sqlite3.connect(db)
    try:
        plans = conn.execute(
            "EXPLAIN QUERY PLAN SELECT DISTINCT anomaly_date, metric, "
            "dimension_name, dimension_value, group_id FROM anomaly_events "
            "WHERE run_id = ? ORDER BY anomaly_date, metric, dimension_name, "
            "dimension_value, group_id LIMIT ?",
            (RUN_ID, 51),
        ).fetchall()
        detail = " ".join(row[3] for row in plans)
        assert "COVERING INDEX idx_events_run_group_keys" in detail
        assert "SCAN" not in detail

        deep = conn.execute(
            "EXPLAIN QUERY PLAN SELECT DISTINCT anomaly_date, metric, "
            "dimension_name, dimension_value, group_id FROM anomaly_events "
            "WHERE run_id = ? AND (anomaly_date, metric, dimension_name, "
            "dimension_value, group_id) > (?,?,?,?,?) ORDER BY anomaly_date, "
            "metric, dimension_name, dimension_value, group_id LIMIT ?",
            (RUN_ID, "2025-01-01", "m", "c", "v", "g", 51),
        ).fetchall()
        deep_detail = " ".join(row[3] for row in deep)
        assert "COVERING INDEX idx_events_run_group_keys" in deep_detail
        assert "SCAN" not in deep_detail

        event_plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM anomaly_events WHERE run_id = ? "
            "AND group_id IN (?,?) ORDER BY group_id, detector, rule_name",
            (RUN_ID, "grp_0000", "grp_0001"),
        ).fetchall()
        event_detail = " ".join(row[3] for row in event_plan)
        assert "USING INDEX idx_events_run_group_id" in event_detail
        assert "SCAN anomaly_events" not in event_detail
    finally:
        conn.close()


def test_legacy_full_run_getters_remain_available(tmp_path):
    db = tmp_path / "ledger.sqlite3"
    events, items = build_paged_run(db, groups=70)
    assert len(store.get_events(db, RUN_ID)) == len(events)
    assert len(store.get_attribution_items(db, RUN_ID)) == len(items)
    missing = tmp_path / "missing.sqlite3"
    assert store.get_events(missing, RUN_ID) == []
    assert store.get_attribution_items(missing, RUN_ID) == []


def _bulk_run(db_path, groups: int, run_id: str = "run-bulk") -> None:
    """Fill a run directly through SQL for a controlled deep-page benchmark."""
    connection = sqlite3.connect(db_path)
    connection.executescript(store.SCHEMA)
    connection.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, "2025-01-01T00:00:00Z", "2025-01-01T00:00:01Z", "success",
         "", "cfg", "d.csv", "data", "rule", "output", 1, groups * 3,
         groups, groups * 4, None),
    )
    events, items = [], []
    for index in range(groups):
        gid = f"grp_{index:06d}"
        day = f"2025-{(index % 12) + 1:02d}-{(index % 28) + 1:02d}"
        for event_index in range(3):
            events.append((
                f"ev_{index}_{event_index}", run_id, gid, f"metric_{index % 5}",
                "channel", f"region_{index % 40:02d}", day, f"det_{event_index}",
                f"rule_{event_index}", 1.0, 2.0, 0.5, "high", "{}", day, day,
            ))
        for rank in range(4):
            items.append((
                f"it_{index}_{rank}", run_id, gid, f"ev_{index}_0", "channel",
                f"value_{rank}", 1.0, 2.0, 0.5, 0.1, rank, 0, "formula", "expl",
            ))
    connection.executemany(
        "INSERT INTO anomaly_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", events
    )
    connection.executemany(
        "INSERT INTO attribution_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", items
    )
    connection.commit()
    connection.close()


def test_deep_page_cost_does_not_grow_with_run_size(tmp_path):
    import time

    small = tmp_path / "small.sqlite3"
    large = tmp_path / "large.sqlite3"
    _bulk_run(small, 500)
    _bulk_run(large, 5000)

    def deep_page_ms(db_path, offset_groups: int) -> float:
        keys = store.get_group_page(db_path, "run-bulk", page_size=offset_groups)
        after = tuple(keys[-1][column] for column in store.GROUP_KEY_COLUMNS)
        start = time.perf_counter()
        for _ in range(20):
            page = store.get_run_detail_page(
                db_path, "run-bulk", page_size=50, after=after
            )
        elapsed = (time.perf_counter() - start) / 20 * 1000
        assert len(page["group_ids"]) == 50
        assert len(page["events"]) == 150  # whole groups: 3 events each
        assert len(page["attribution_items"]) == 200  # 4 items each
        return elapsed

    small_ms = deep_page_ms(small, 400)
    large_ms = deep_page_ms(large, 4000)
    # Ten times as much underlying data must not make a deep page ten times
    # slower; the covering index turns deep paging into a bounded range seek.
    assert large_ms < small_ms * 5
