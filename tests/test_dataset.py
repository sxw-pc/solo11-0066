from dataclasses import replace
import hashlib

import pytest

from drift_attr.config import DatasetConfig
from drift_attr.dataset import load_dataset
from drift_attr.errors import DriftError
from helpers import write_csv


def long_cfg(path) -> DatasetConfig:
    return DatasetConfig(
        path=str(path), format="long", encoding="utf-8", duplicate_policy="warn",
        empty_value="error", date_field="date", metric_field="metric",
        dimension_name_field="dimension_name", dimension_value_field="dimension_value",
        value_field="value",
    )


def test_long_load_and_fingerprint_stable(tmp_path):
    body = "2025-01-02,orders,channel,region_alpha,10\n2025-01-01,orders,channel,region_alpha,5\n"
    one = write_csv(tmp_path / "one.csv", body)
    two = write_csv(
        tmp_path / "two.csv",
        "2025-01-01,orders,channel,region_alpha,5\n2025-01-02,orders,channel,region_alpha,10\n",
    )
    first, second = load_dataset(long_cfg(one)), load_dataset(long_cfg(two))
    assert first.row_count == 2
    assert [row.value for row in first.rows] == [10, 5]
    assert first.fingerprint == second.fingerprint
    assert first.dimension_names == ["channel"]


def test_missing_file_code(tmp_path):
    with pytest.raises(DriftError) as exc:
        load_dataset(long_cfg(tmp_path / "missing.csv"))
    assert exc.value.code == "E_DATA_FILE_NOT_FOUND"
    assert "missing.csv" in str(exc.value)


def test_bad_encoding(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_bytes(b"date,metric,dimension_name,dimension_value,value\n\xff\n")
    with pytest.raises(DriftError) as exc:
        load_dataset(long_cfg(path))
    assert exc.value.code == "E_DATA_ENCODING"


def test_bad_header(tmp_path):
    path = write_csv(tmp_path / "bad.csv", "", header="date,metric,value\n")
    with pytest.raises(DriftError) as exc:
        load_dataset(long_cfg(path))
    assert exc.value.code == "E_DATA_HEADER"
    assert "dimension_name" in exc.value.message


@pytest.mark.parametrize(
    ("body", "code", "line", "field"),
    [
        ("not-a-date,orders,channel,region_alpha,1\n", "E_DATA_DATE", 2, "date"),
        ("2025-01-01,orders,channel,region_alpha,NaN\n", "E_DATA_NON_FINITE", 2, "value"),
        ("2025-01-01,orders,channel,region_alpha,abc\n", "E_DATA_NUMBER", 2, "value"),
        ("2025-01-01,orders,,region_alpha,abc\n", "E_DATA_EMPTY_VALUE", 2, "dimension_name"),
    ],
)
def test_row_errors_are_located(tmp_path, body, code, line, field):
    path = write_csv(tmp_path / "data.csv", body)
    with pytest.raises(DriftError) as exc:
        load_dataset(long_cfg(path))
    assert (exc.value.code, exc.value.line, exc.value.field) == (code, line, field)


def test_empty_drop_and_duplicate_policy(tmp_path):
    path = write_csv(
        tmp_path / "data.csv",
        "2025-01-01,orders,channel,region_alpha,\n"
        "2025-01-01,orders,channel,region_alpha,4\n"
        "2025-01-01,orders,channel,region_alpha,6\n",
    )
    cfg = replace(long_cfg(path), empty_value="drop")
    dataset = load_dataset(cfg)
    assert dataset.row_count == 2
    assert any("空值" in warning for warning in dataset.warnings)
    assert any("重复业务键" in warning for warning in dataset.warnings)
    with pytest.raises(DriftError) as exc:
        load_dataset(replace(cfg, duplicate_policy="error"))
    assert exc.value.code == "E_DATA_DUPLICATE_KEY"


def test_no_rows(tmp_path):
    path = write_csv(tmp_path / "empty.csv", "")
    with pytest.raises(DriftError) as exc:
        load_dataset(long_cfg(path))
    assert exc.value.code == "E_DATA_NO_ROWS"


def test_wide_adapter(tmp_path):
    path = tmp_path / "wide.csv"
    path.write_text(
        "date,region,visits,orders\n"
        "2025-01-06,region_alpha,10,3\n"
        "2025-01-07,region_alpha,12,4\n",
        encoding="utf-8",
    )
    cfg = DatasetConfig(
        path=str(path), format="wide", encoding="utf-8", duplicate_policy="warn",
        empty_value="error", date_field="date", dimension_field="region",
        dimension_name="region", metric_columns=("visits", "orders"),
    )
    dataset = load_dataset(cfg)
    assert dataset.row_count == 4
    assert {(row.metric, row.value) for row in dataset.rows} == {
        ("visits", 10.0), ("orders", 3.0), ("visits", 12.0), ("orders", 4.0)
    }
    assert all(row.dimension_name == "region" for row in dataset.rows)


def test_dataset_carries_exact_raw_bytes_and_sha(tmp_path):
    body = (
        "date,metric,dimension_name,dimension_value,value\n"
        "2025-01-01,orders,channel,region_alpha,5\n"
        "2025-01-02,orders,channel,region_alpha,10\n"
    )
    path = write_csv(tmp_path / "data.csv", body.split("\n", 1)[1])
    raw = path.read_bytes()
    dataset = load_dataset(long_cfg(path))
    assert dataset.raw_bytes == raw
    assert dataset.raw_sha256 == hashlib.sha256(raw).hexdigest()
    # Reordered rows keep the canonical fingerprint but change the raw SHA.
    reordered = tmp_path / "reordered.csv"
    reordered.write_bytes(
        b"date,metric,dimension_name,dimension_value,value\n"
        b"2025-01-02,orders,channel,region_alpha,10\n"
        b"2025-01-01,orders,channel,region_alpha,5\n"
    )
    other = load_dataset(long_cfg(reordered))
    assert other.fingerprint == dataset.fingerprint
    assert other.raw_sha256 != dataset.raw_sha256


def test_parse_dataset_bytes_never_touches_source_again(tmp_path):
    path = write_csv(
        tmp_path / "data.csv",
        "2025-01-01,orders,channel,region_alpha,5\n"
        "2025-01-02,orders,channel,region_alpha,10\n",
    )
    cfg = long_cfg(path)
    sealed = path.read_bytes()
    from drift_attr.dataset import parse_dataset_bytes

    dataset = parse_dataset_bytes(sealed, cfg, "data.csv", resolved_path=path)
    # Deleting the source after the read cannot affect parsing of the bytes.
    path.unlink()
    assert dataset.row_count == 2 and dataset.raw_bytes == sealed
    assert dataset.raw_sha256 == hashlib.sha256(sealed).hexdigest()
