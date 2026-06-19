"""ODBC backend via pyodbc + IBM i Access ODBC Driver."""

from __future__ import annotations

import pyodbc

# Driver name as registered by IBM i Access Client Solutions on the host OS.
# Linux: "IBM i Access ODBC Driver"
# Windows: "IBM i Access ODBC Driver" (same, but registered via ODBC Data Sources)
_DRIVER = "IBM i Access ODBC Driver"


def connect(host: str, database: str, username: str, password: str, **_ignored):
    """Return a DB-API 2.0 connection to IBM i via ODBC."""
    conn_str = (
        f"DRIVER={{{_DRIVER}}};"
        f"SYSTEM={host};"
        f"DATABASE={database};"
        f"UID={username};"
        f"PWD={password};"
        "TRANSLATE=1;"          # auto-translate EBCDIC → UTF-8
        "UNICODESQL=1;"         # send SQL as Unicode
    )
    return pyodbc.connect(conn_str, autocommit=True)
