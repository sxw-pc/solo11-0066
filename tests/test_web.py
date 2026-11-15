import json

from drift_attr.engine import analyze
from drift_attr.web import create_app
from helpers import demo_project


def test_empty_web_state_and_health(tmp_path):
    app = create_app(tmp_path / "missing.sqlite3", tmp_path / "output", tmp_path)
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert "还没有运行记录" in response.get_data(as_text=True)
    health = client.get("/healthz").get_json()
    assert health == {"status": "ok", "offline": True, "runs": 0}
    assert client.get("/missing").status_code == 404
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403


def test_seeded_web_pages_and_artifacts(tmp_path):
    root = demo_project(tmp_path)
    result = analyze(root / "fixtures" / "rules.yaml", run_id="web-run", base_dir=root)
    app = create_app(result.database_path, root / "output", root)
    client = app.test_client()
    index = client.get("/")
    assert index.status_code == 200
    assert "web-run" in index.get_data(as_text=True)
    detail = client.get("/runs/web-run")
    text = detail.get_data(as_text=True)
    assert detail.status_code == 200
    assert "channel_web" in text and "按 channel 归因" in text
    artifact = client.get("/runs/web-run/artifact/report_html")
    assert artifact.status_code == 200
    assert artifact.mimetype == "text/html"
    assert "漂移归因台" in artifact.get_data(as_text=True)
    download = client.get("/runs/web-run/artifact/report_json?download=1")
    assert download.status_code == 200
    assert "attachment" in download.headers["Content-Disposition"]
    assert json.loads(download.get_data(as_text=True))["run"]["run_id"] == "web-run"
    assert client.get("/runs/web-run/artifact/not-real").status_code == 404
    assert client.get("/runs/absent").status_code == 404
    health = client.get("/healthz").get_json()
    assert health["runs"] == 1


def test_purging_run_hidden_and_tombstone_page(tmp_path):
    import sqlite3

    root = demo_project(tmp_path)
    result = analyze(root / "fixtures" / "rules.yaml", run_id="hidden-run", base_dir=root)
    app = create_app(result.database_path, root / "output", root)
    client = app.test_client()
    assert client.get("/runs/hidden-run").status_code == 200

    with sqlite3.connect(result.database_path) as conn:
        conn.execute("UPDATE runs SET status = 'purging' WHERE run_id = 'hidden-run'")
    assert client.get("/runs/hidden-run").status_code == 404
    assert client.get("/runs/hidden-run/artifact/report_json").status_code == 404
    assert "hidden-run" not in client.get("/").get_data(as_text=True)
    assert client.get("/healthz").get_json()["runs"] == 0
    page = client.get("/tombstones")
    assert page.status_code == 200
    assert "还没有已清理的运行记录" in page.get_data(as_text=True)


def test_artifact_hash_drift_is_not_served(tmp_path):
    root = demo_project(tmp_path)
    result = analyze(root / "fixtures" / "rules.yaml", run_id="drift-run", base_dir=root)
    app = create_app(result.database_path, root / "output", root)
    client = app.test_client()
    assert client.get("/runs/drift-run/artifact/report_json").status_code == 200
    (result.run_dir / "report.json").write_text("{}", encoding="utf-8")
    assert client.get("/runs/drift-run/artifact/report_json").status_code == 404
