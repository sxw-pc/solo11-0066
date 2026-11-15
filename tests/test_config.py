import pytest

from drift_attr.config import load_config
from drift_attr.errors import DriftError
from helpers import ROOT, config_dict, write_config


def valid_config() -> dict:
    return config_dict([
        {"name": "r1", "type": "threshold", "metrics": ["orders"], "lower": 5, "direction": "below"}
    ])


def load_with(tmp_path, payload):
    return load_config(write_config(tmp_path / "rules.yaml", payload))


def test_demo_config_loads_and_has_fingerprint():
    config = load_config(ROOT / "fixtures" / "rules.yaml")
    assert config.version == 1
    assert len(config.detectors) == 4
    assert config.attribution.dimensions == ("channel",)
    assert len(config.fingerprint) == 64
    assert config.dataset.path == "fixtures/demo_metrics.csv"


def test_unknown_detector(tmp_path):
    payload = valid_config()
    payload["detection"]["detectors"][0]["type"] = "magic"
    with pytest.raises(DriftError) as exc:
        load_with(tmp_path, payload)
    assert exc.value.code == "E_CONFIG_UNKNOWN_DETECTOR"


def test_unknown_fields_are_rejected(tmp_path):
    payload = valid_config()
    payload["secret_remote"] = "forbidden"
    with pytest.raises(DriftError) as exc:
        load_with(tmp_path, payload)
    assert exc.value.code == "E_CONFIG_SCHEMA"
    assert "secret_remote" in exc.value.message


def test_unknown_detector_param(tmp_path):
    payload = valid_config()
    payload["detection"]["detectors"][0]["window"] = 3
    with pytest.raises(DriftError) as exc:
        load_with(tmp_path, payload)
    assert exc.value.code == "E_CONFIG_SCHEMA"
    assert "window" in exc.value.message


def test_duplicate_dimension(tmp_path):
    payload = valid_config()
    payload["attribution"]["dimensions"] = ["channel", "channel"]
    with pytest.raises(DriftError) as exc:
        load_with(tmp_path, payload)
    assert exc.value.code == "E_CONFIG_DUPLICATE"


def test_duplicate_detector_name(tmp_path):
    payload = valid_config()
    payload["detection"]["detectors"].append(dict(payload["detection"]["detectors"][0]))
    with pytest.raises(DriftError) as exc:
        load_with(tmp_path, payload)
    assert exc.value.code == "E_CONFIG_DUPLICATE"


@pytest.mark.parametrize("top_n", [0, -1, 101, "3", True])
def test_invalid_top_n_code(tmp_path, top_n):
    payload = valid_config()
    payload["attribution"]["top_n"] = top_n
    with pytest.raises(DriftError) as exc:
        load_with(tmp_path, payload)
    assert exc.value.code == "E_CONFIG_TOP_N"


@pytest.mark.parametrize("window", [0, -2])
def test_invalid_window(tmp_path, window):
    payload = valid_config()
    payload["attribution"]["baseline_window"] = window
    with pytest.raises(DriftError) as exc:
        load_with(tmp_path, payload)
    assert exc.value.code in {"E_CONFIG_SCHEMA", "E_CONFIG_WINDOW"}


def test_threshold_requires_matching_bounds(tmp_path):
    payload = valid_config()
    payload["detection"]["detectors"][0].pop("lower")
    with pytest.raises(DriftError) as exc:
        load_with(tmp_path, payload)
    assert exc.value.code == "E_CONFIG_SCHEMA"
    assert "lower" in exc.value.field


def test_missing_detector_list_and_parse_failure(tmp_path):
    payload = valid_config()
    payload["detection"]["detectors"] = []
    with pytest.raises(DriftError) as exc:
        load_with(tmp_path, payload)
    assert exc.value.code == "E_CONFIG_SCHEMA"
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: [\n", encoding="utf-8")
    with pytest.raises(DriftError) as exc:
        load_config(bad)
    assert exc.value.code == "E_CONFIG_PARSE"
    with pytest.raises(DriftError) as exc:
        load_config(tmp_path / "absent.yaml")
    assert exc.value.code == "E_CONFIG_FILE_NOT_FOUND"


def test_long_dataset_requires_all_columns(tmp_path):
    payload = valid_config()
    payload["dataset"].pop("value_field")
    with pytest.raises(DriftError) as exc:
        load_with(tmp_path, payload)
    assert exc.value.code == "E_CONFIG_SCHEMA"
    assert exc.value.field == "dataset.value_field"


def test_replay_input_policy_defaults_to_reference(tmp_path):
    config = load_with(tmp_path, valid_config())
    assert config.replay.input_policy == "reference"


def test_replay_snapshot_policy_loads_and_rejects_unknown(tmp_path):
    payload = valid_config()
    payload["replay"] = {"input_policy": "snapshot"}
    assert load_with(tmp_path, payload).replay.input_policy == "snapshot"
    bad = valid_config()
    bad["replay"] = {"input_policy": "cloud"}
    with pytest.raises(DriftError) as exc:
        load_with(tmp_path, bad)
    assert exc.value.code == "E_CONFIG_SCHEMA"
    assert exc.value.field == "replay.input_policy"
    unknown = valid_config()
    unknown["replay"] = {"retention": "forever"}
    with pytest.raises(DriftError) as exc:
        load_with(tmp_path, unknown)
    assert exc.value.code == "E_CONFIG_SCHEMA"


def test_parse_config_text_reloads_rule_snapshot(tmp_path):
    from drift_attr.config import parse_config_text

    path = write_config(tmp_path / "rules.yaml", valid_config())
    text = path.read_text(encoding="utf-8")
    config = parse_config_text(text, tmp_path / "virtual-snapshot.yaml")
    assert config.source_path == (tmp_path / "virtual-snapshot.yaml")
    assert config.fingerprint == load_config(path).fingerprint
