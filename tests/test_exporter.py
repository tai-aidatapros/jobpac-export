"""
Unit tests for src/exporter.py.

Uses unittest.mock to patch jaydebeapi so tests run without a real database.
"""

import csv
import itertools
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.exporter import TableResult, export_tables


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_cursor(columns: list[str], rows: list[list]):
    cursor = MagicMock()
    cursor.description = [(col, None, None, None, None, None, None) for col in columns]
    tuples = [tuple(r) for r in rows]
    cursor.fetchall.return_value = tuples
    # fetchmany: return all rows on first call, empty list on subsequent calls
    cursor.fetchmany.side_effect = itertools.cycle([tuples, []])
    return cursor


_FAKE_JDBC = "jdbc:as400://testhost/testdb"
_FAKE_CREDS = ("user", "pass")
_FAKE_JAR = "/fake/jt400.jar"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestExportTables:
    """Tests for the export_tables function."""

    @patch("src.drivers.jdbc.jaydebeapi")
    def test_exports_single_table(self, mock_jaydebeapi):
        """A single table should produce one CSV file and a summary CSV."""
        columns = ["ID", "Name", "Value"]
        rows = [
            [1, "Alpha", 10.5],
            [2, "Beta", 20.3],
            [3, "Gamma", 30.1],
        ]

        mock_conn = MagicMock()
        mock_cursor = _make_mock_cursor(columns, rows)
        mock_conn.cursor.return_value = mock_cursor
        mock_jaydebeapi.connect.return_value = mock_conn

        with tempfile.TemporaryDirectory() as tmp_dir:
            results, _ = export_tables(
                connection_string=_FAKE_JDBC,
                tables=["TEST_TABLE"],
                output_dir=tmp_dir,
                credentials=_FAKE_CREDS,
                jar_path=_FAKE_JAR,
            )

            assert len(results) == 1
            r = results[0]
            assert r.table_name == "TEST_TABLE"
            assert r.row_count == 3
            assert r.elapsed_seconds >= 0

            csv_path = Path(tmp_dir) / "TEST_TABLE.csv"
            assert csv_path.exists()
            with open(csv_path) as fh:
                reader = list(csv.reader(fh))
                assert reader[0] == columns
                assert len(reader) == 4  # header + 3 data rows

            summary_path = Path(tmp_dir) / "JobPacTableProcessingInfo.csv"
            assert summary_path.exists()
            with open(summary_path) as fh:
                reader = list(csv.reader(fh))
                assert reader[0] == ["TableName", "RowCount", "ElapsedSeconds"]
                assert reader[1][0] == "TEST_TABLE"
                assert reader[1][1] == "3"

    @patch("src.drivers.jdbc.jaydebeapi")
    def test_exports_multiple_tables(self, mock_jaydebeapi):
        """Multiple tables should each produce their own CSV."""
        mock_conn = MagicMock()
        mock_cursor = _make_mock_cursor(["Col1"], [[1], [2]])
        mock_conn.cursor.return_value = mock_cursor
        mock_jaydebeapi.connect.return_value = mock_conn

        tables = ["TABLE_A", "TABLE_B", "TABLE_C"]

        with tempfile.TemporaryDirectory() as tmp_dir:
            results, _ = export_tables(
                connection_string=_FAKE_JDBC,
                tables=tables,
                output_dir=tmp_dir,
                credentials=_FAKE_CREDS,
                jar_path=_FAKE_JAR,
            )

            assert len(results) == 3
            for i, name in enumerate(tables):
                assert results[i].table_name == name
                assert (Path(tmp_dir) / f"{name}.csv").exists()

    @patch("src.drivers.jdbc.jaydebeapi")
    def test_empty_table(self, mock_jaydebeapi):
        """A table with zero rows should still produce a CSV with just the header."""
        mock_conn = MagicMock()
        mock_cursor = _make_mock_cursor(["ID", "Name"], [])
        mock_conn.cursor.return_value = mock_cursor
        mock_jaydebeapi.connect.return_value = mock_conn

        with tempfile.TemporaryDirectory() as tmp_dir:
            results, _ = export_tables(
                connection_string=_FAKE_JDBC,
                tables=["EMPTY_TABLE"],
                output_dir=tmp_dir,
                credentials=_FAKE_CREDS,
                jar_path=_FAKE_JAR,
            )

            assert results[0].row_count == 0
            csv_path = Path(tmp_dir) / "EMPTY_TABLE.csv"
            with open(csv_path) as fh:
                reader = list(csv.reader(fh))
                assert len(reader) == 1  # header only

    @patch("src.drivers.jdbc.jaydebeapi")
    def test_connection_failure_raises(self, mock_jaydebeapi):
        """If the connection test fails, the exception should propagate."""
        mock_jaydebeapi.connect.side_effect = Exception("Connection refused")

        with tempfile.TemporaryDirectory() as tmp_dir:
            with pytest.raises(Exception, match="Connection refused"):
                export_tables(
                    connection_string=_FAKE_JDBC,
                    tables=["TABLE_X"],
                    output_dir=tmp_dir,
                    credentials=_FAKE_CREDS,
                    jar_path=_FAKE_JAR,
                )
