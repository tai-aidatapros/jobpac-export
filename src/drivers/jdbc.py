"""JDBC backend via jaydebeapi + jt400.jar (original implementation)."""

from __future__ import annotations

import jaydebeapi

_DRIVER_CLASS = "com.ibm.as400.access.AS400JDBCDriver"


def connect(host: str, database: str, username: str, password: str, jar_path: str):
    """Return a DB-API 2.0 connection to IBM i via JDBC."""
    url = f"jdbc:as400://{host}/{database}"
    return jaydebeapi.connect(_DRIVER_CLASS, url, [username, password], jar_path)
