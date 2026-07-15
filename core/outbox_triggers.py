"""SQLite outbox trigger installer — captures row-level I/U/D into outbox."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

OUTBOX_DDL = """
CREATE TABLE IF NOT EXISTS outbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name    TEXT NOT NULL,
    row_id        TEXT NOT NULL,
    op            TEXT NOT NULL,
    payload       TEXT NOT NULL,
    table_version TEXT NOT NULL,
    created_at    INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
"""

OUTBOX_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_outbox_created_at ON outbox(created_at);
"""

SCHEMA_VERSIONS_DDL = """
CREATE TABLE IF NOT EXISTS _outbox_schema_versions (
    table_name    TEXT PRIMARY KEY,
    table_version TEXT NOT NULL
);
"""

EXCLUDED_TABLES = frozenset({
    "outbox",
    "_outbox_schema_versions",
    "chat_messages_fts",
})


@dataclass(frozen=True)
class TableSpec:
    name: str
    pk_columns: list[str]
    columns: list[str]


def _is_excluded_table(name: str) -> bool:
    if name in EXCLUDED_TABLES:
        return True
    if name.startswith("sqlite_"):
        return True
    if name.endswith("__old") or name.endswith("_old") or name.endswith("_new"):
        return True
    if name.startswith("_old_"):
        return True
    return False


def compute_table_version(conn, table_name: str) -> str:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    parts = sorted(f"{row[1]}:{row[2]}" for row in rows)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def table_specs_from_metadata(metadata) -> list[TableSpec]:
    specs: list[TableSpec] = []
    for table in sorted(metadata.sorted_tables, key=lambda t: t.name):
        if _is_excluded_table(table.name):
            continue
        pk_columns = [col.name for col in table.primary_key.columns]
        columns = [col.name for col in table.columns]
        specs.append(TableSpec(name=table.name, pk_columns=pk_columns, columns=columns))
    return specs


def table_specs_from_pragma(conn) -> list[TableSpec]:
    specs: list[TableSpec] = []
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    ]
    for name in tables:
        if _is_excluded_table(name):
            continue
        info = conn.execute(f"PRAGMA table_info({name})").fetchall()
        pk_columns = [
            row[1]
            for row in sorted((r for r in info if r[5]), key=lambda r: r[5])
        ]
        columns = [row[1] for row in info]
        specs.append(TableSpec(name=name, pk_columns=pk_columns, columns=columns))
    return specs


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _json_object_expr(row_alias: str, columns: list[str]) -> str:
    if not columns:
        return "json_object()"
    pairs = ", ".join(f"'{col}', {_quote_ident(row_alias)}.{_quote_ident(col)}" for col in columns)
    return f"json_object({pairs})"


def _trigger_sql(spec: TableSpec, op: str, suffix: str, row_alias: str) -> str:
    row_id_expr = _json_object_expr(row_alias, spec.pk_columns)
    payload_expr = _json_object_expr(row_alias, spec.columns)
    trigger_name = f"outbox_{spec.name}_{suffix}"
    event = {"I": "INSERT", "U": "UPDATE", "D": "DELETE"}[op]
    table = _quote_ident(spec.name)
    return f"""
CREATE TRIGGER {_quote_ident(trigger_name)}
AFTER {event} ON {table}
BEGIN
    INSERT INTO outbox (table_name, row_id, op, payload, table_version)
    VALUES (
        '{spec.name}',
        {row_id_expr},
        '{op}',
        {payload_expr},
        (SELECT table_version FROM _outbox_schema_versions WHERE table_name = '{spec.name}')
    );
END;
"""


def install_outbox(conn, table_specs: list[TableSpec] | None = None) -> None:
    conn.executescript(OUTBOX_DDL)
    conn.executescript(OUTBOX_INDEX_DDL)
    conn.executescript(SCHEMA_VERSIONS_DDL)

    specs = table_specs if table_specs is not None else table_specs_from_pragma(conn)

    for spec in specs:
        version = compute_table_version(conn, spec.name)
        conn.execute(
            "INSERT OR REPLACE INTO _outbox_schema_versions (table_name, table_version) VALUES (?, ?)",
            (spec.name, version),
        )

    for spec in specs:
        for suffix in ("ai", "au", "ad"):
            conn.execute(f"DROP TRIGGER IF EXISTS outbox_{spec.name}_{suffix}")
        conn.executescript(_trigger_sql(spec, "I", "ai", "new"))
        conn.executescript(_trigger_sql(spec, "U", "au", "new"))
        conn.executescript(_trigger_sql(spec, "D", "ad", "old"))
