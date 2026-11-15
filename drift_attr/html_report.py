"""Self-contained HTML report rendering with inline CSS and no remote assets.

The streaming API (:func:`iter_html_parts` / :func:`write_html`) yields small
fragments per anomaly group so a complete HTML string is never held in memory.
The dict-based :func:`report_to_html` is retained for callers that already hold
a materialized report (e.g. the CLI ``report`` command) and shares the exact
same fragment builders, so both paths produce identical bytes.
"""
from __future__ import annotations

import html
from typing import Any, Iterator

STYLE = """
:root{--ink:#1f2933;--muted:#617080;--paper:#fbfaf7;--panel:#fff;--line:#dfe3e6;
--accent:#1c6b5a;--accent-soft:#e7f2ee;--danger:#a33b2b;--danger-soft:#f8ece9}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);
font-family:"Segoe UI","Noto Sans SC",Arial,sans-serif;line-height:1.55}
header{background:#143d34;color:#f3faf7;padding:28px 6vw 24px}
header h1{margin:0 0 6px;font-size:clamp(1.6rem,3vw,2.4rem);letter-spacing:.02em}
header p{margin:2px 0;color:#cbe2da;font-size:.92rem;overflow-wrap:anywhere}
main{max-width:1120px;margin:0 auto;padding:30px 5vw 60px}
section{margin:0 0 26px}h2{font-size:1.2rem;border-bottom:1px solid var(--line);
padding-bottom:8px;margin:0 0 14px}h3{margin:22px 0 6px;font-size:1.05rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.card b{display:block;font-size:1.5rem;color:var(--accent)}
.card span{color:var(--muted);font-size:.84rem}
.anomaly{background:var(--panel);border:1px solid var(--line);border-left:5px solid var(--danger);
border-radius:10px;padding:16px 18px;margin:16px 0}
.narrative{background:var(--accent-soft);padding:10px 12px;border-radius:7px;margin:10px 0}
table{width:100%;border-collapse:collapse;background:var(--panel);font-size:.9rem}
th,td{text-align:left;border-bottom:1px solid var(--line);padding:8px;vertical-align:top}
th{color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.04em}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.tag{display:inline-block;background:var(--danger-soft);color:var(--danger);border-radius:20px;
padding:1px 9px;font-size:.76rem;margin-right:5px}
.muted{color:var(--muted)}.mono{font-family:Consolas,monospace;font-size:.82rem;overflow-wrap:anywhere}
details{margin:6px 0}summary{cursor:pointer;color:var(--accent);font-weight:600}
ul.evidence{padding-left:20px}ul.evidence li{margin:3px 0}
footer{color:var(--muted);font-size:.8rem;border-top:1px solid var(--line);padding-top:14px}
@media(max-width:680px){th:nth-child(3),td:nth-child(3){display:none}}
"""


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.2f}%"


def _summary_cards(summary: dict[str, Any]) -> str:
    cards = [
        ("异常位置", summary["anomaly_count"]),
        ("原始事件", summary["raw_event_count"]),
        ("归因明细", summary["attribution_item_count"]),
        ("严重度", ", ".join(f"{key}:{value}" for key, value in summary["severity_counts"].items())),
    ]
    return '<div class="grid">' + "".join(
        f'<div class="card"><b>{_e(value)}</b><span>{_e(label)}</span></div>'
        for label, value in cards
    ) + "</div>"


def _artifact_list(artifacts: list[dict[str, Any]]) -> str:
    return "<ul>" + "".join(
        f'<li><span class="tag">{_e(item["kind"])}</span>'
        f'<span class="mono">{_e(item["path"])}</span></li>'
        for item in artifacts
    ) + "</ul>"


def _attribution_table(group: dict[str, Any]) -> str:
    rows: list[str] = []
    for dimension, items in group["attribution"].items():
        for item in items:
            rows.append(
                "<tr>"
                f"<td>{item['rank']}</td>"
                f"<td>{_e(dimension)}={_e(item['dimension_value'])}</td>"
                f"<td class=\"num\">{item['current_value']}</td>"
                f"<td class=\"num\">{item['baseline_value']}</td>"
                f"<td class=\"num\">{item['contribution']:+.6f}</td>"
                f"<td class=\"num\">{_rate(item['contribution_rate'])}</td>"
                f"<td>{'是' if item['is_other'] else '否'}</td>"
                "</tr>"
            )
    if not rows:
        return '<p class="muted">此异常没有可用的归因维度数据。</p>'
    return (
        "<table><thead><tr><th>排名</th><th>维度值与归因维度</th>"
        "<th>异常窗口</th><th>对照窗口</th><th>贡献额</th><th>贡献率</th>"
        "<th>其他项</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _group_section(group: dict[str, Any]) -> str:
    evidence = "".join(
        f"<li><b>{_e(event['rule_name'])}</b> / {_e(event['detector'])}："
        f"{_e(event['evidence'].get('summary', ''))} "
        f"<span class=\"muted\">score={_e(event['score'])}</span></li>"
        for event in group["raw_events"]
    )
    window = group["attribution_windows"]
    window_text = "n/a"
    if window:
        window_text = (
            f"对照 {window['baseline_start']} ~ {window['baseline_end']}；"
            f"异常 {window['comparison_start']} ~ {window['comparison_end']}"
        )
    return f"""
<article class="anomaly">
  <h3>{_e(group['date'])} · {_e(group['metric'])} ·
      {_e(group['dimension_name'])}={_e(group['dimension_value'])}</h3>
  <span class="tag">{_e(group['severity'])}</span>
  <span class="muted">检测器：{_e(', '.join(group['detectors']))}；规则：
      {_e(', '.join(group['rule_names']))}</span>
  <div class="narrative">{_e(group['narrative'])}</div>
  <p class="muted">归因窗口：{_e(window_text)}</p>
  {_attribution_table(group)}
  <details><summary>展开原始检测证据（{len(group['raw_events'])} 条）</summary>
    <ul class="evidence">{evidence}</ul>
  </details>
</article>
"""


def _head_and_header(run: dict[str, Any], summary: dict[str, Any]) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>漂移归因台 · {_e(run['run_id'])}</title>
<style>{STYLE}</style>
</head>
<body>
<header>
  <h1>漂移归因台 · 分析报告</h1>
  <p>运行 ID：{_e(run['run_id'])} · 状态：{_e(run['status'])}</p>
  <p>配置指纹：{_e(run['config_fingerprint'])}</p>
  <p>数据指纹：{_e(run['dataset_fingerprint'])}</p>
  <p>开始：{_e(run['started_at'])} · 结束：{_e(run['finished_at'])}</p>
</header>
<main>
  <section><h2>运行概要</h2>{_summary_cards(summary)}</section>
  <section><h2>异常与维度归因</h2>"""


_FOOTER_TEMPLATE = """</section>
  <section><h2>产物清单</h2>{artifacts}
    <p class="muted">本页样式与数据全部内联，无远程字体、脚本、样式或图片引用。</p>
  </section>
  <footer>漂移归因台 · 本地离线确定性分析 · 业务数据均为虚构 fixture</footer>
</main>
</body>
</html>
"""


def iter_html_parts(
    run: dict[str, Any],
    summary: dict[str, Any],
    artifacts: list[dict[str, Any]],
    group_views: Iterator[dict[str, Any]],
) -> Iterator[str]:
    """Yield HTML fragments; each fragment covers at most one anomaly group."""
    yield _head_and_header(run, summary)
    saw_group = False
    for group in group_views:
        saw_group = True
        yield _group_section(group)
    if not saw_group:
        yield '<p class="muted">本次运行没有检测到异常。</p>'
    yield _FOOTER_TEMPLATE.format(artifacts=_artifact_list(artifacts))


def write_html(writer: Any, ctx: Any) -> None:
    """Stream the HTML report through a :class:`ChunkedTextWriter`."""
    from drift_attr.report import report_header

    header = report_header(ctx)
    for fragment in iter_html_parts(
        header["run"], header["summary"], ctx.artifacts, ctx.group_views()
    ):
        writer.write(fragment)


def report_to_html(report: dict[str, Any]) -> str:
    return "".join(
        iter_html_parts(
            report["run"],
            report["summary"],
            report["artifacts"],
            iter(report["anomalies"]),
        )
    )
