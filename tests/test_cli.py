import json

from drift_attr.cli import main


def test_demo_cli_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["demo", "--run-id", "cli-demo"]) == 0
    demo_output = capsys.readouterr().out
    assert (tmp_path / "fixtures" / "demo_metrics.csv").is_file()
    assert "anomalies: 4 groups / 4 raw events" in demo_output
    assert main(["validate", "--config", "fixtures/rules.yaml"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["row_count"] == 420 and summary["series_count"] == 12
    assert (tmp_path / "output" / "cli-demo" / "report.html").is_file()
    assert (tmp_path / "output" / "drift_attr.sqlite3").is_file()
    assert main(["runs", "--db", "output/drift_attr.sqlite3"]) == 0
    assert "cli-demo" in capsys.readouterr().out
    assert main([
        "report", "--run-id", "cli-demo", "--db", "output/drift_attr.sqlite3",
        "--format", "markdown", "--out", "export.md",
    ]) == 0
    assert "漂移归因台分析报告" in (tmp_path / "export.md").read_text(encoding="utf-8")


def test_cli_errors_are_stable(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main([
        "analyze", "--config", "fixtures/rules.yaml", "--run-id", "../bad"
    ]) == 1
    assert "E_CONFIG_FILE_NOT_FOUND" in capsys.readouterr().err
    assert main(["init-demo"]) == 0
    capsys.readouterr()
    assert main([
        "analyze", "--config", "fixtures/rules.yaml", "--run-id", "../bad"
    ]) == 1
    assert "E_ARGUMENT" in capsys.readouterr().err
    assert main([
        "report", "--run-id", "absent", "--db", "output/drift_attr.sqlite3"
    ]) == 1
    assert "E_RUN_NOT_FOUND" in capsys.readouterr().err
    assert main(["serve", "--host", "0.0.0.0"]) == 1
    assert "E_WEB_HOST" in capsys.readouterr().err


def test_report_without_out_writes_stdout(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init-demo", "--force"]) == 0
    assert main(["analyze", "--config", "fixtures/rules.yaml", "--run-id", "stdout-run"]) == 0
    capsys.readouterr()
    assert main([
        "report", "--run-id", "stdout-run", "--format", "json",
        "--db", "output/drift_attr.sqlite3",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run"]["run_id"] == "stdout-run"
