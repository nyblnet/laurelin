"""Where the StarRocks tests get an engine, and how they skip without one.

Not named ``test_*`` on purpose: it is imported by
``tests/test_starrocks.py``, ``tests/test_starrocks_governance.py``,
``tests/test_dialects.py`` and ``tests/test_text_agreement.py``, and one
definition of "is StarRocks configured" is the point.

Unlike ClickHouse — embedded, so a local Parquet file *is* the remote source —
StarRocks is a server. There is no way to fake it, so these suites are gated on
``LAURELIN_TEST_STARROCKS`` holding a DSN:

    LAURELIN_TEST_STARROCKS=starrocks://root:@127.0.0.1:9030/lau

``docker run -p 9030:9030 -p 8030:8030 -p 8040:8040 starrocks/allin1-ubuntu``
is enough; cold start to a queryable BE measured about 15 seconds locally.

**Fixture data is loaded with the driver's ordinary cursor and ``%s``**, which
is client-side interpolation — the very thing the library refuses to do. That
is deliberate and it is safe *here*: a prepared INSERT is rejected by StarRocks
outright (error 1295, "not supported in the prepared statement protocol yet"),
these values are test fixtures rather than policy input, and the escaping was
checked to round-trip every hostile value used byte-for-byte. The library's own
prohibition is enforced structurally, over ``laurelin/``, in
``tests/test_starrocks.py``.
"""

from __future__ import annotations

import os
import uuid

import pytest

from laurelin.core import starrocks

URL = os.environ.get("LAURELIN_TEST_STARROCKS", "").strip()


def configured() -> bool:
    return bool(URL) and starrocks.available()


needs_starrocks = pytest.mark.skipif(
    not configured(),
    reason="needs a StarRocks server: set LAURELIN_TEST_STARROCKS="
           "starrocks://user:password@host:9030/database and "
           "pip install 'laurelin[starrocks]'",
)


def database() -> str:
    return starrocks.parse_url(URL)["database"]


def connect():
    return starrocks.connect({"url": URL})


def source(table: str) -> dict:
    """A registerable source dict for a table in the configured database."""
    return {"type": "table", "url": URL, "table": f"{database()}.{table}"}


def load(columns: str, rows: list[tuple], key: str = "id") -> str:
    """Create a uniquely named table, fill it, and return its bare name.

    Unique per call because the suite may run against a shared cluster and a
    leftover table from a failed run must not become another test's fixture.
    """
    name = f"lau_t_{uuid.uuid4().hex[:12]}"
    con = connect()
    try:
        cur = con.cursor()
        cur.execute(
            f"CREATE TABLE `{database()}`.`{name}` ({columns}) "
            f"DUPLICATE KEY(`{key}`) DISTRIBUTED BY HASH(`{key}`) "
            "PROPERTIES('replication_num'='1')"
        )
        if rows:
            placeholders = ", ".join(["%s"] * len(rows[0]))
            cur.executemany(
                f"INSERT INTO `{database()}`.`{name}` VALUES ({placeholders})", rows
            )
        cur.close()
    finally:
        con.close()
    return name


def drop(name: str) -> None:
    con = connect()
    try:
        cur = con.cursor()
        cur.execute(f"DROP TABLE IF EXISTS `{database()}`.`{name}`")
        cur.close()
    finally:
        con.close()
