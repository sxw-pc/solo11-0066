"""Read-only local Flask viewer for the SQLite run ledger."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, render_template, request, url_for

from drift_attr import store


MIME_TYPES = {
    "json": "application/json; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "yaml": "text/yaml; charset=utf-8",
}
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
HIDDEN_RUN_STATES = {"purging"}


def _visible_run(database_path: str | Path, run_id: str) -> dict[str, Any] | None:
    """Return a run only while it is a normal, non-purging record.

    A purge intent hides the run from the very first committed state, before
    any byte is unlinked, so partially deleted artifacts can never be served.
    """
    run = store.get_run(database_path, run_id)
    if run is None or run.get("status") in HIDDEN_RUN_STATES:
        return None
    return run


def _group_view(events: list[dict[str, Any]], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for event in events:
        group = groups.setdefault(
            event["group_id"],
            {
                "group_id": event["group_id"],
                "date": event["anomaly_date"],
                "metric": event["metric"],
                "dimension_name": event["dimension_name"],
                "dimension_value": event["dimension_value"],
                "severity": event["severity"],
                "events": [],
                "attribution": {},
            },
        )
        group["events"].append(event)
        if event["severity"] == "critical":
            group["severity"] = "critical"
        elif event["severity"] == "high" and group["severity"] != "critical":
            group["severity"] = "high"
    for item in items:
        group = groups.get(item["group_id"])
        if group is None:
            continue
        group["attribution"].setdefault(item["attribution_dimension"], []).append(item)
    result = sorted(
        groups.values(),
        key=lambda item: (
            item["date"], item["metric"], item["dimension_name"], item["dimension_value"]
        ),
    )
    for group in result:
        for dimension_items in group["attribution"].values():
            dimension_items.sort(key=lambda item: item["rank"])
    return result


def create_app(
    database_path: str | Path,
    output_root: str | Path,
    project_root: str | Path,
) -> Flask:
    app = Flask(__name__)
    app.config.update(
        DATABASE_PATH=str(Path(database_path)),
        OUTPUT_ROOT=Path(output_root).resolve(),
        PROJECT_ROOT=Path(project_root).resolve(),
    )

    @app.before_request
    def local_only() -> None:
        hostname = (request.host or "").split(":", 1)[0].strip("[]").lower()
        if hostname not in ALLOWED_HOSTS:
            abort(403)

    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; script-src 'none'; object-src 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/")
    def index() -> str:
        runs = store.list_runs(app.config["DATABASE_PATH"])
        return render_template("index.html", runs=runs, active="runs")

    @app.get("/runs/<run_id>")
    def run_detail(run_id: str) -> str:
        run = _visible_run(app.config["DATABASE_PATH"], run_id)
        if run is None:
            abort(404)
        events = store.get_events(app.config["DATABASE_PATH"], run_id)
        items = store.get_attribution_items(app.config["DATABASE_PATH"], run_id)
        artifacts = store.get_artifacts(app.config["DATABASE_PATH"], run_id)
        return render_template(
            "run_detail.html",
            run=run,
            groups=_group_view(events, items),
            artifacts=artifacts,
            active="runs",
        )

    @app.get("/runs/<run_id>/artifact/<kind>")
    def artifact(run_id: str, kind: str) -> Response:
        if _visible_run(app.config["DATABASE_PATH"], run_id) is None:
            abort(404)
        artifacts = store.get_artifacts(app.config["DATABASE_PATH"], run_id)
        selected = next((item for item in artifacts if item["kind"] == kind), None)
        if selected is None:
            abort(404)
        raw = Path(selected["path"])
        candidate = (raw if raw.is_absolute() else app.config["PROJECT_ROOT"] / raw)
        if candidate.is_symlink():
            abort(404)
        candidate = candidate.resolve()
        root = app.config["OUTPUT_ROOT"]
        if not candidate.is_relative_to(root) or not candidate.is_file():
            abort(404)
        content = candidate.read_text(encoding="utf-8")
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != selected["sha256"]:
            abort(404)
        mime = MIME_TYPES.get(selected["format"], "text/plain; charset=utf-8")
        download = request.args.get("download") == "1"
        disposition = "attachment" if download else "inline"
        response = Response(content, mimetype=mime)
        response.headers["Content-Disposition"] = (
            f'{disposition}; filename="{candidate.name}"'
        )
        return response

    @app.get("/healthz")
    def healthz() -> Response:
        return jsonify(
            {
                "status": "ok",
                "offline": True,
                "runs": len(store.list_runs(app.config["DATABASE_PATH"])),
            }
        )

    @app.get("/tombstones")
    def tombstones() -> str:
        return render_template(
            "tombstones.html",
            tombstones=store.list_tombstones(app.config["DATABASE_PATH"]),
            intents=store.list_purge_intents(app.config["DATABASE_PATH"]),
            active="tombstones",
        )

    @app.errorhandler(404)
    def not_found(_error: Exception) -> tuple[str, int]:
        return render_template("not_found.html", active=""), 404

    @app.errorhandler(403)
    def forbidden(_error: Exception) -> tuple[str, int]:
        return render_template(
            "not_found.html",
            active="",
            message="仅允许通过本机地址访问。",
        ), 403

    @app.context_processor
    def template_helpers() -> dict[str, Any]:
        return {
            "artifact_url": lambda run_id, kind, download=False: url_for(
                "artifact",
                run_id=run_id,
                kind=kind,
                **({"download": "1"} if download else {}),
            )
        }

    return app
