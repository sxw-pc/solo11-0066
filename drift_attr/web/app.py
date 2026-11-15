"""Read-only local Flask viewer for the SQLite run ledger."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, render_template, request, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer

from drift_attr import store
from drift_attr.store import GROUP_KEY_COLUMNS


MIME_TYPES = {
    "json": "application/json; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "yaml": "text/yaml; charset=utf-8",
}
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
PAGE_SIZE = 50
# Cursors are meant for a continuous paging session, not permanent bookmarks.
CURSOR_MAX_AGE_SECONDS = 60 * 60 * 12


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
    # Per-process signing key: paging cursors only need to be unforgeable while
    # the viewer process is alive; restarting simply forces a fresh first page.
    app.config.update(
        DATABASE_PATH=str(Path(database_path)),
        OUTPUT_ROOT=Path(output_root).resolve(),
        PROJECT_ROOT=Path(project_root).resolve(),
        CURSOR_SECRET=secrets.token_urlsafe(32),
    )

    def _serializer() -> URLSafeTimedSerializer:
        return URLSafeTimedSerializer(app.config["CURSOR_SECRET"], salt="run-page-cursor")

    def _decode_cursor(
        token: str | None, version_token: str
    ) -> tuple[tuple[Any, ...] | None, bool, int]:
        """Decode a keyset cursor, binding it to the run's current generation.

        Returns ``(after, stale, page_number)``. A missing, malformed, expired or
        wrong-generation cursor yields ``(None, stale, 1)`` and the view restarts
        from the first page rather than mixing two run generations.
        """
        if not token:
            return None, False, 1
        try:
            payload = _serializer().loads(token, max_age=CURSOR_MAX_AGE_SECONDS)
        except BadSignature:
            return None, True, 1
        if not isinstance(payload, dict) or set(payload) != {"v", "k", "p"}:
            return None, True, 1
        if payload["v"] != version_token or not isinstance(payload["k"], list):
            return None, True, 1
        if len(payload["k"]) != len(GROUP_KEY_COLUMNS) or not all(
            isinstance(part, str) for part in payload["k"]
        ):
            return None, True, 1
        if not isinstance(payload["p"], int) or payload["p"] < 1:
            return None, True, 1
        return tuple(payload["k"]), False, payload["p"]

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
        run = store.get_run(app.config["DATABASE_PATH"], run_id)
        if run is None:
            abort(404)
        version_token = store.run_version_token(run)
        after, cursor_stale, page_number = _decode_cursor(
            request.args.get("after"), version_token
        )

        # One keyset page only: ordered group keys first, then all rows of those
        # groups. The full-run get_events/get_attribution_items are never called.
        page = store.get_run_detail_page(
            app.config["DATABASE_PATH"],
            run_id,
            page_size=PAGE_SIZE,
            after=after,
        )
        groups = _group_view(page["events"], page["attribution_items"])
        artifacts = store.get_artifacts(app.config["DATABASE_PATH"], run_id)

        next_url = None
        next_page = page_number
        if page["has_more"] and page["next_after"] is not None:
            next_page = page_number + 1
            token = _serializer().dumps({
                "v": version_token,
                "k": list(page["next_after"]),
                "p": next_page,
            })
            next_url = url_for("run_detail", run_id=run_id, after=token)

        total_groups = run["anomaly_count"]
        shown = len(groups)
        if shown:
            range_start = (page_number - 1) * PAGE_SIZE + 1
            range_end = (page_number - 1) * PAGE_SIZE + shown
        else:
            range_start = range_end = 0
        return render_template(
            "run_detail.html",
            run=run,
            groups=groups,
            artifacts=artifacts,
            active="runs",
            page_size=PAGE_SIZE,
            page_number=page_number,
            total_groups=total_groups,
            range_start=range_start,
            range_end=range_end,
            next_url=next_url,
            next_page=next_page,
            cursor_stale=cursor_stale,
        )

    @app.get("/runs/<run_id>/artifact/<kind>")
    def artifact(run_id: str, kind: str) -> Response:
        if store.get_run(app.config["DATABASE_PATH"], run_id) is None:
            abort(404)
        artifacts = store.get_artifacts(app.config["DATABASE_PATH"], run_id)
        selected = next((item for item in artifacts if item["kind"] == kind), None)
        if selected is None:
            abort(404)
        raw = Path(selected["path"])
        candidate = raw.resolve() if raw.is_absolute() else (
            app.config["PROJECT_ROOT"] / raw
        ).resolve()
        root = app.config["OUTPUT_ROOT"]
        if not candidate.is_relative_to(root) or not candidate.is_file():
            abort(404)
        content = candidate.read_text(encoding="utf-8")
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
