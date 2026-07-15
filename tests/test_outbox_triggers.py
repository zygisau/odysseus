import json
import sqlite3

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine

from core.outbox_triggers import (
    OUTBOX_DDL,
    SCHEMA_VERSIONS_DDL,
    TableSpec,
    compute_table_version,
    install_outbox,
    table_specs_from_metadata,
    table_specs_from_pragma,
)


def test_compute_table_version_changes_when_column_added(tmp_path):
    db_path = tmp_path / "fp.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE items (id TEXT PRIMARY KEY, name TEXT)")
        v1 = compute_table_version(conn, "items")
        conn.execute("ALTER TABLE items ADD COLUMN qty INTEGER DEFAULT 0")
        v2 = compute_table_version(conn, "items")
        assert v1 != v2
        assert len(v1) == 64
        assert len(v2) == 64
    finally:
        conn.close()


def test_outbox_ddl_creates_tables(tmp_path):
    db_path = tmp_path / "ddl.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(OUTBOX_DDL)
        conn.executescript(SCHEMA_VERSIONS_DDL)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "outbox" in tables
        assert "_outbox_schema_versions" in tables
    finally:
        conn.close()


def test_table_specs_from_metadata_skips_excluded_and_reads_pk():
    md = MetaData()
    Table(
        "widgets",
        md,
        Column("id", String, primary_key=True),
        Column("label", String),
    )
    Table("outbox", md, Column("id", Integer, primary_key=True))
    specs = table_specs_from_metadata(md)
    names = [s.name for s in specs]
    assert names == ["widgets"]
    assert specs[0].pk_columns == ["id"]
    assert specs[0].columns == ["id", "label"]


def test_table_specs_from_pragma_composite_pk(tmp_path):
    db_path = tmp_path / "pragma.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE pair (
                a TEXT,
                b TEXT,
                val TEXT,
                PRIMARY KEY (a, b)
            )
            """
        )
        conn.execute("CREATE TABLE outbox (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE email_tags__old (message_id TEXT)")
        specs = table_specs_from_pragma(conn)
        assert [s.name for s in specs] == ["pair"]
        assert specs[0].pk_columns == ["a", "b"]
        assert specs[0].columns == ["a", "b", "val"]
    finally:
        conn.close()


def _make_widget_db(conn):
    conn.execute(
        "CREATE TABLE widgets (id TEXT PRIMARY KEY, name TEXT NOT NULL)"
    )


def test_install_outbox_creates_triggers_and_registry(tmp_path):
    db_path = tmp_path / "install.db"
    conn = sqlite3.connect(db_path)
    try:
        _make_widget_db(conn)
        install_outbox(conn, [TableSpec("widgets", ["id"], ["id", "name"])])
        conn.commit()
        triggers = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        assert "outbox_widgets_ai" in triggers
        assert "outbox_widgets_au" in triggers
        assert "outbox_widgets_ad" in triggers
        version = conn.execute(
            "SELECT table_version FROM _outbox_schema_versions WHERE table_name='widgets'"
        ).fetchone()[0]
        assert len(version) == 64
    finally:
        conn.close()


def _latest_outbox_row(conn):
    return conn.execute(
        "SELECT table_name, row_id, op, payload, table_version FROM outbox ORDER BY id DESC LIMIT 1"
    ).fetchone()


def test_outbox_captures_insert_update_delete(tmp_path):
    db_path = tmp_path / "iud.db"
    conn = sqlite3.connect(db_path)
    try:
        _make_widget_db(conn)
        install_outbox(conn, [TableSpec("widgets", ["id"], ["id", "name"])])
        conn.commit()

        conn.execute("INSERT INTO widgets (id, name) VALUES ('w1', 'alpha')")
        conn.commit()
        table_name, row_id, op, payload, table_version = _latest_outbox_row(conn)
        assert table_name == "widgets"
        assert op == "I"
        assert json.loads(row_id) == {"id": "w1"}
        assert json.loads(payload) == {"id": "w1", "name": "alpha"}
        insert_version = table_version

        conn.execute("UPDATE widgets SET name='beta' WHERE id='w1'")
        conn.commit()
        _, row_id, op, payload, table_version = _latest_outbox_row(conn)
        assert op == "U"
        assert json.loads(row_id) == {"id": "w1"}
        assert json.loads(payload) == {"id": "w1", "name": "beta"}
        assert table_version == insert_version

        conn.execute("DELETE FROM widgets WHERE id='w1'")
        conn.commit()
        _, row_id, op, payload, _ = _latest_outbox_row(conn)
        assert op == "D"
        assert json.loads(row_id) == {"id": "w1"}
        assert json.loads(payload) == {"id": "w1", "name": "beta"}
    finally:
        conn.close()


def test_outbox_composite_pk_row_id(tmp_path):
    db_path = tmp_path / "composite.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE pair (
                message_id TEXT,
                owner TEXT,
                body TEXT,
                PRIMARY KEY (message_id, owner)
            )
            """
        )
        spec = TableSpec("pair", ["message_id", "owner"], ["message_id", "owner", "body"])
        install_outbox(conn, [spec])
        conn.commit()
        conn.execute(
            "INSERT INTO pair (message_id, owner, body) VALUES (?, ?, ?)",
            ("<mid@x>", "alice", "hello"),
        )
        conn.commit()
        _, row_id, op, payload, _ = _latest_outbox_row(conn)
        assert op == "I"
        assert json.loads(row_id) == {"message_id": "<mid@x>", "owner": "alice"}
        assert json.loads(payload)["body"] == "hello"
    finally:
        conn.close()


def test_reinstall_bumps_table_version_after_alter(tmp_path):
    db_path = tmp_path / "alter.db"
    conn = sqlite3.connect(db_path)
    try:
        _make_widget_db(conn)
        spec = TableSpec("widgets", ["id"], ["id", "name"])
        install_outbox(conn, [spec])
        conn.commit()
        v1 = conn.execute(
            "SELECT table_version FROM _outbox_schema_versions WHERE table_name='widgets'"
        ).fetchone()[0]
        conn.execute("ALTER TABLE widgets ADD COLUMN qty INTEGER DEFAULT 0")
        install_outbox(conn, [TableSpec("widgets", ["id"], ["id", "name", "qty"])])
        conn.commit()
        v2 = conn.execute(
            "SELECT table_version FROM _outbox_schema_versions WHERE table_name='widgets'"
        ).fetchone()[0]
        assert v1 != v2
    finally:
        conn.close()


def test_direct_outbox_write_does_not_recurse(tmp_path):
    db_path = tmp_path / "recurse.db"
    conn = sqlite3.connect(db_path)
    try:
        _make_widget_db(conn)
        install_outbox(conn, [TableSpec("widgets", ["id"], ["id", "name"])])
        conn.commit()
        conn.execute(
            """
            INSERT INTO outbox (table_name, row_id, op, payload, table_version)
            VALUES ('outbox', '{}', 'I', '{}', 'deadbeef')
            """
        )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_scheduled_emails_db_outbox_triggers(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()

    conn = sqlite3.connect(db_path)
    try:
        before = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        conn.execute(
            """
            INSERT INTO scheduled_emails
            (id, to_addr, body, send_at, created_at, status)
            VALUES ('sched-1', 'a@b.com', 'body', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'pending')
            """
        )
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        assert after == before + 1
        row = conn.execute(
            "SELECT table_name, op, row_id FROM outbox ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row[0] == "scheduled_emails"
        assert row[1] == "I"
        assert json.loads(row[2]) == {"id": "sched-1"}
    finally:
        conn.close()


def test_init_db_installs_outbox_triggers(tmp_path, monkeypatch):
    from core import database as cdb

    db_path = tmp_path / "app.db"
    new_engine = create_engine(f"sqlite:///{db_path}")
    monkeypatch.setattr(cdb, "DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setattr(cdb, "engine", new_engine)

    cdb.init_db()

    conn = sqlite3.connect(db_path)
    try:
        triggers = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'outbox_%'"
            ).fetchall()
        }
        assert "outbox_sessions_ai" in triggers
        assert "outbox_chat_messages_ai" in triggers
        conn.execute(
            "INSERT INTO sessions (id, name, endpoint_url, model, created_at, updated_at) "
            "VALUES ('s1', 'test', 'http://localhost', 'gpt-4', datetime('now'), datetime('now'))"
        )
        conn.commit()
        op = conn.execute(
            "SELECT op FROM outbox WHERE table_name='sessions' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        assert op == "I"
    finally:
        conn.close()
