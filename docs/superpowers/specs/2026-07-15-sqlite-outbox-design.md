# SQLite Outbox Design

**Date:** 2026-07-15  
**Status:** Approved  
**Scope:** Capture all row-level changes (INSERT / UPDATE / DELETE) from every tracked table in both SQLite databases and write them to a per-database `outbox` table. Reading/consuming outbox rows is out of scope.

## Goal

Subscribe to all table changes in:

1. **`data/app.db`** — ~26 SQLAlchemy tables in `core/database.py`
2. **`data/scheduled_emails.db`** — ~12 email cache tables in `routes/email_helpers.py`

Each database gets its own `outbox` table. A future reader (not in scope) will poll or process these rows.

## Non-goals

- Outbox consumer / reader / relay
- Delivery guarantees, retries, idempotency keys
- Retention, pruning, or archival policy
- Cross-database consolidation into a single outbox
- Payload redaction (encrypted columns stored as ciphertext, same as source row)

## Approach

**SQLite triggers** (recommended and chosen) — install `AFTER INSERT/UPDATE/DELETE` triggers on every tracked table. This captures all writes regardless of path: SQLAlchemy `SessionLocal`, raw `sqlite3.connect()`, and migration scripts.

Rejected alternatives:

- **SQLAlchemy session events** — misses raw `sqlite3` writes common in email code and migrations.
- **Hybrid triggers + app hooks** — two mechanisms, incomplete coverage on email DB.

Precedent: `_migrate_chat_messages_fts()` in `core/database.py` already installs per-table SQLite triggers.

## Schema

Identical `outbox` table in both databases:

```sql
CREATE TABLE outbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name    TEXT NOT NULL,
    row_id        TEXT NOT NULL,   -- JSON object: PK column(s) → value(s)
    op            TEXT NOT NULL,   -- 'I' | 'U' | 'D'
    payload       TEXT NOT NULL,   -- JSON row snapshot
    table_version TEXT NOT NULL,   -- schema fingerprint (see below)
    created_at    INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE INDEX idx_outbox_created_at ON outbox(created_at);
```

### Row semantics

| Operation | `row_id` source | `payload` content |
|-----------|-----------------|-------------------|
| INSERT    | PK columns from `new` | full `new` row as JSON |
| UPDATE    | PK columns from `new` | full `new` row as JSON (post-update state) |
| DELETE    | PK columns from `old` | full `old` row as JSON |

### `row_id` examples

- Single string PK: `{"id": "sess-abc"}`
- Composite PK: `{"message_id": "<...>", "owner": "alice"}`
- Non-`id` PK (`calendar_events`): `{"uid": "evt-123"}`

Built with SQLite `json_object('col1', new.col1, ...)` in triggers.

### `table_version` — automatic schema fingerprint

This project has **no Alembic or per-table migration registry**. Migrations are ad-hoc `_migrate_*()` functions in `core/database.py`.

At trigger install time, compute a stable fingerprint per table from `PRAGMA table_info(<table>)` — sorted `"column_name:column_type"` pairs, hashed (e.g. SHA-256 hex). Store in a registry table:

```sql
CREATE TABLE _outbox_schema_versions (
    table_name    TEXT PRIMARY KEY,
    table_version TEXT NOT NULL
);
```

Triggers read the version at fire time:

```sql
(SELECT table_version FROM _outbox_schema_versions WHERE table_name = '<table>')
```

`install_outbox()` recomputes and upserts fingerprints on every startup, then reinstalls triggers. When a migration adds a column, the next app start bumps the fingerprint automatically.

## Architecture

```
app.db
  SQLAlchemy models (~26 tables)
    → AFTER I/U/D triggers
      → outbox

scheduled_emails.db
  email_helpers raw SQL (~12 tables)
    → AFTER I/U/D triggers
      → outbox
```

### New module: `core/outbox_triggers.py`

| Export | Responsibility |
|--------|----------------|
| `OUTBOX_DDL` | `CREATE TABLE IF NOT EXISTS outbox (...)` |
| `SCHEMA_VERSIONS_DDL` | `CREATE TABLE IF NOT EXISTS _outbox_schema_versions (...)` |
| `install_outbox(conn, table_specs)` | Create tables, refresh fingerprints, drop/recreate all triggers |
| `table_specs_from_metadata(metadata)` | Derive PK + columns from SQLAlchemy `Base.metadata` |
| `table_specs_from_pragma(conn)` | Introspect tables via `PRAGMA table_info` + index metadata for PKs |
| `compute_table_version(conn, table_name)` | Column-signature hash |

`TableSpec` shape: `name`, `pk_columns: list[str]`, `columns: list[str]`.

### Installation points

1. **`core/database.py` → `init_db()`** — after `Base.metadata.create_all()`, call `_migrate_outbox_triggers()` which opens `app.db`, runs `install_outbox()` with specs from `Base.metadata`.

2. **`routes/email_helpers.py` → `_init_scheduled_db()`** — after email cache tables are created, run `install_outbox()` with specs from `PRAGMA` introspection.

### Trigger strategy

On every startup:

1. `CREATE TABLE IF NOT EXISTS outbox`
2. `CREATE TABLE IF NOT EXISTS _outbox_schema_versions`
3. For each tracked table → compute fingerprint → `INSERT OR REPLACE` into registry
4. `DROP TRIGGER IF EXISTS outbox_<table>_ai|_au|_ad`
5. `CREATE TRIGGER` (3 per table)

Idempotent and self-healing when columns are added via migrations.

**Trigger naming:** `outbox_<table>_ai`, `outbox_<table>_au`, `outbox_<table>_ad`.

### Excluded tables

- `outbox` (prevents infinite recursion)
- `_outbox_schema_versions`
- `chat_messages_fts` (virtual FTS mirror; source of truth is `chat_messages`)
- SQLite internal tables (`sqlite_%`)
- Temporary migration tables (e.g. `_old_scheduled_tasks`, `*_new`, `*_old`)

Only regular tables from `sqlite_master WHERE type='table'` are tracked.

### Trigger body template

```sql
INSERT INTO outbox (table_name, row_id, op, payload, table_version)
VALUES (
  '<table>',
  json_object('id', new.id),                    -- all PK cols
  'I',
  json_object('id', new.id, 'name', new.name, ...),  -- all columns
  (SELECT table_version FROM _outbox_schema_versions WHERE table_name = '<table>')
);
```

DELETE triggers use `old.*`. UPDATE triggers use `new.*` for both `row_id` and `payload`.

## Data flow

```
App → source table (INSERT/UPDATE/DELETE)
  → AFTER trigger (same transaction)
    → outbox INSERT
```

Outbox writes are atomic with the source change. Rollback of the source write rolls back the outbox row.

## Error handling

- `install_outbox()` wrapped in try/except; failures log a warning (same pattern as `_migrate_chat_messages_fts()`). App still starts; outbox may be incomplete.
- Skip entirely for non-SQLite `DATABASE_URL` backends.
- Skip or no-op for `:memory:` test databases where trigger install is not needed (tests call `install_outbox()` explicitly on temp DBs).

## Testing

New file: `tests/test_outbox_triggers.py`

1. Temp SQLite DB → `install_outbox()` → verify `outbox` and `_outbox_schema_versions` exist.
2. INSERT / UPDATE / DELETE on a simple table → one outbox row with correct `op`, `row_id`, `payload`, `table_version`.
3. Composite-PK table fixture → `row_id` JSON contains all PK columns.
4. `ALTER TABLE ADD COLUMN` + reinstall → `table_version` changes.
5. Write to `outbox` directly → no recursive outbox rows.
6. Email DB fixture → triggers fire on `scheduled_emails.db` tables.

## Files to create / modify

| File | Change |
|------|--------|
| `core/outbox_triggers.py` | **New** — shared installer, trigger generator, schema fingerprint |
| `core/database.py` | Add `_migrate_outbox_triggers()`, call from `init_db()` |
| `routes/email_helpers.py` | Call `install_outbox()` at end of `_init_scheduled_db()` |
| `tests/test_outbox_triggers.py` | **New** — trigger and payload tests |

## Decisions log

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Which databases | Both `app.db` and `scheduled_emails.db` | User requirement |
| Outbox location | One per database | SQLite cannot cross-DB trigger without ATTACH |
| Capture mechanism | SQLite triggers | Universal coverage including raw sqlite3 |
| Payload format | Full row JSON | User requirement; `table_name` identifies shape |
| UPDATE payload | New row only | User requirement |
| DELETE payload | Old row only | Logical complement to UPDATE=new |
| `row_id` type | JSON object of PK columns | Supports string and composite PKs |
| Schema versioning | Automatic column fingerprint | No Alembic; zero manual bump discipline |
| Reader | Out of scope | User requirement |
