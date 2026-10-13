import pytest

from sessiongauge.cli import _safe_csv


def test_csv_formula_guard_handles_leading_whitespace_and_controls():
    assert _safe_csv(" \t=1+1").startswith("'")
    assert _safe_csv("雪") == "雪"


def test_export_refuses_existing_output_without_force(tmp_path):
    from argparse import Namespace

    from sessiongauge.cli import _export
    from sessiongauge.config import Config

    path = tmp_path / "out.csv"
    path.write_text("keep")
    args = Namespace(output=str(path), force=False, format="csv", bom=False, days=30)
    with pytest.raises(ValueError):
        _export(Config(data_dir=tmp_path), args)
    assert path.read_text() == "keep"


def test_standalone_export_has_stable_columns_guard_and_protects_hardlinks(tmp_path):
    import csv

    from sessiongauge.export import FIELDS, export_rows

    source = tmp_path / "monitor.db"
    source.write_text("protected")
    alias = tmp_path / "alias.csv"
    alias.hardlink_to(source)
    with pytest.raises(ValueError):
        export_rows([], alias, protected_paths=[source])
    output = tmp_path / "out.csv"
    export_rows([{"task_title": " \t=1+1", "profile": "雪\x01"}], output, bom=True)
    assert output.read_bytes().startswith(b"\xef\xbb\xbf")
    with output.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        assert tuple(reader.fieldnames) == FIELDS
        row = next(reader)
    assert row["task_title"].startswith("'")
    assert row["profile"] == "雪"
