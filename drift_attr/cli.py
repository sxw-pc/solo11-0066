"""Command-line interface for init, validation, analysis, reports and serving."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence
from drift_attr import store
from drift_attr.config import load_config
from drift_attr.dataset import resolve_path
from drift_attr.demo import ensure_demo, write_demo_fixtures
from drift_attr.engine import analyze, validate
from drift_attr.errors import DriftError, fail
from drift_attr.html_report import report_to_html
from drift_attr.report import report_to_markdown
from drift_attr.web import create_app
DEFAULT_CONFIG = "fixtures/rules.yaml"
DEFAULT_DB = "output/drift_attr.sqlite3"
def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
def _cmd_init_demo(args: argparse.Namespace) -> int:
    paths = write_demo_fixtures(Path.cwd(), force=args.force)
    for path in paths.values():
        print(path)
    return 0
def _cmd_demo(args: argparse.Namespace) -> int:
    write_demo_fixtures(Path.cwd(), force=True)
    return _cmd_analyze(args)
def _cmd_validate(args: argparse.Namespace) -> int:
    result = validate(args.config, Path.cwd())
    _print_json(result.summary())
    return 0
def _cmd_analyze(args: argparse.Namespace) -> int:
    result = analyze(args.config, run_id=args.run_id, base_dir=Path.cwd())
    print(f"run_id: {result.run_id}")
    print(f"dataset_fingerprint: {result.dataset.fingerprint}")
    print(f"config_fingerprint: {result.config.fingerprint}")
    print(
        f"anomalies: {len(result.groups)} groups / "
        f"{len(result.events)} raw events / "
        f"{sum(len(group.attribution_items) for group in result.groups)} attribution items"
    )
    print(f"database: {result.database_path}")
    print(f"run_dir: {result.run_dir}")
    for artifact in result.artifacts:
        print(f"artifact[{artifact['kind']}]: {artifact['path']}")
    return 0
def _resolve_artifact_path(value: str, base: Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base / path).resolve()
def _cmd_report(args: argparse.Namespace) -> int:
    run = store.get_run(args.db, args.run_id)
    if run is None:
        fail("E_RUN_NOT_FOUND", f"台账中不存在运行: {args.run_id}", path=args.db)
    artifacts = store.get_artifacts(args.db, args.run_id)
    source = next((item for item in artifacts if item["kind"] == "report_json"), None)
    if source is None:
        fail("E_ARTIFACT_NOT_FOUND", "缺少 report_json 产物", path=args.db)
    path = _resolve_artifact_path(source["path"], Path.cwd())
    if not path.is_file():
        fail("E_ARTIFACT_NOT_FOUND", "报告文件不存在", path=str(path))
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DriftError("E_ARTIFACT_NOT_FOUND", f"无法读取报告: {exc}", path=str(path)) from exc
    if args.format == "json":
        content = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    elif args.format == "markdown":
        content = report_to_markdown(report)
    else:
        content = report_to_html(report)
    if args.out:
        destination = Path(args.out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        print(f"report: {destination}")
    else:
        sys.stdout.write(content)
    return 0
def _cmd_runs(args: argparse.Namespace) -> int:
    runs = store.list_runs(args.db)
    if args.json:
        _print_json(runs)
        return 0
    if not runs:
        print("暂无运行记录。")
        return 0
    print("run_id\tstatus\tstarted_at\tanomalies\tevents\tattribution")
    for run in runs:
        print(
            f"{run['run_id']}\t{run['status']}\t{run['started_at']}\t"
            f"{run['anomaly_count']}\t{run['event_count']}\t"
            f"{run['attribution_count']}"
        )
    return 0
def _cmd_serve(args: argparse.Namespace) -> int:
    if args.host not in {"127.0.0.1", "localhost"}:
        fail("E_WEB_HOST", "Web 服务只允许监听 127.0.0.1 或 localhost", field="--host")
    if not (1 <= args.port <= 65535):
        fail("E_ARGUMENT", "端口必须在 1-65535 之间", field="--port")
    base = Path.cwd()
    if args.demo:
        ensure_demo(base)
    config = load_config(args.config)
    output_root = resolve_path(config.output.directory, base)
    database = output_root / config.output.database
    if args.demo and not any(
        run["run_id"] == "demo-run" for run in store.list_runs(database)
    ):
        analyze(args.config, run_id="demo-run", base_dir=base)
    app = create_app(database, output_root, base)
    print(f"漂移归因台: http://{args.host}:{args.port}/")
    print(f"台账: {database}")
    print("按 Ctrl+C 停止；服务仅监听本机地址。")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return 0
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drift_attr",
        description="漂移归因台：本地业务指标异常检测与维度贡献归因",
    )
    parser.add_argument("--version", action="version", version="drift-attr 1.0.0")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init-demo", help="初始化演示数据与规则")
    init.add_argument("--force", action="store_true", help="覆盖已有 demo fixture")
    init.set_defaults(func=_cmd_init_demo)
    demo_cmd = sub.add_parser("demo", help="一条命令完成初始化、校验、检测与台账")
    demo_cmd.add_argument("--config", default=DEFAULT_CONFIG)
    demo_cmd.add_argument("--run-id", default="demo-run")
    demo_cmd.set_defaults(func=_cmd_demo)
    validate_cmd = sub.add_parser("validate", help="只校验配置、数据与时间序列")
    validate_cmd.add_argument("--config", default=DEFAULT_CONFIG)
    validate_cmd.set_defaults(func=_cmd_validate)
    analyze_cmd = sub.add_parser("analyze", help="执行检测、归因、报告与台账写入")
    analyze_cmd.add_argument("--config", default=DEFAULT_CONFIG)
    analyze_cmd.add_argument("--run-id", default=None)
    analyze_cmd.set_defaults(func=_cmd_analyze)
    report_cmd = sub.add_parser("report", help="从台账读取并重新导出报告")
    report_cmd.add_argument("--run-id", required=True)
    report_cmd.add_argument("--db", default=DEFAULT_DB)
    report_cmd.add_argument("--format", choices=("json", "markdown", "html"), default="json")
    report_cmd.add_argument("--out", default=None)
    report_cmd.set_defaults(func=_cmd_report)
    runs_cmd = sub.add_parser("runs", help="列出本地运行台账")
    runs_cmd.add_argument("--db", default=DEFAULT_DB)
    runs_cmd.add_argument("--json", action="store_true")
    runs_cmd.set_defaults(func=_cmd_runs)
    serve_cmd = sub.add_parser("serve", help="启动本地只读 Web 查看器")
    serve_cmd.add_argument("--config", default=DEFAULT_CONFIG)
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8000)
    serve_cmd.add_argument("--demo", action="store_true", help="确保 demo 运行可用")
    serve_cmd.set_defaults(func=_cmd_serve)
    return parser
def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except DriftError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已停止。", file=sys.stderr)
        return 130
