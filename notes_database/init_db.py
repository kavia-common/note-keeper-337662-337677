#!/usr/bin/env python3
"""Initialize SQLite database for notes_database.

This script is intended to be deterministic and safe to run repeatedly:
- It will create the DB file if missing.
- It will create required tables/indexes if missing.
- It will apply small schema upgrades in-place (idempotently) when possible.
- It will (re)write db_connection.txt and db_visualizer/sqlite.env to reflect the current location.

Contract:
- Inputs: none (uses local working directory; DB_NAME constant).
- Outputs: creates/updates `myapp.db`, writes `db_connection.txt`, writes `db_visualizer/sqlite.env`.
- Errors: raises/prints sqlite3/IO errors with context; attempts to keep DB usable.
- Side effects: filesystem writes as described above.
"""

import os
import sqlite3
from typing import Dict, List, Tuple

DB_NAME = "myapp.db"
DB_USER = "kaviasqlite"  # Not used for SQLite, but kept for consistency
DB_PASSWORD = "kaviadefaultpassword"  # Not used for SQLite, but kept for consistency
DB_PORT = "5000"  # Not used for SQLite, but kept for consistency


# PUBLIC_INTERFACE
def init_database(db_name: str = DB_NAME) -> None:
    """Initialize or upgrade the SQLite database schema deterministically.

    Args:
        db_name: SQLite database file name (relative path).

    Raises:
        sqlite3.Error: if SQLite reports an unrecoverable error.
        OSError: if writing helper files fails.
    """
    print("Starting SQLite setup...")

    db_exists = os.path.exists(db_name)
    if db_exists:
        print(f"SQLite database already exists at {db_name}")
        _verify_database_accessible(db_name)
    else:
        print("Creating new SQLite database...")

    conn = sqlite3.connect(db_name)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Enable foreign keys (good practice even if not used yet).
        cursor.execute("PRAGMA foreign_keys = ON")

        _apply_pragmas(cursor)

        # Deterministic schema creation / upgrade.
        _ensure_core_schema(cursor)
        _ensure_notes_schema(cursor)

        _seed_app_info(cursor)

        conn.commit()

        _print_database_statistics(cursor)

    finally:
        conn.close()

    _write_connection_info_files(db_name)

    print("\nSQLite setup complete!")
    current_dir = os.getcwd()
    connection_string = f"sqlite:///{current_dir}/{db_name}"
    print(f"Database: {db_name}")
    print(f"Location: {current_dir}/{db_name}")
    print("")
    print("To connect to the database, use one of the following methods:")
    print(f"1. Python: sqlite3.connect('{db_name}')")
    print(f"2. Connection string: {connection_string}")
    print(f"3. Direct file access: {current_dir}/{db_name}")
    print("")
    _print_sqlite_cli_hint(db_name)
    print("\nScript completed successfully.")


def _verify_database_accessible(db_name: str) -> None:
    """Verify an existing database file is accessible."""
    try:
        conn = sqlite3.connect(db_name)
        conn.execute("SELECT 1")
        conn.close()
        print("Database is accessible and working.")
    except Exception as e:
        # Do not silently ignore; user should know corruption is possible.
        print(f"Warning: Database exists but may be corrupted/unreadable: {e}")


def _apply_pragmas(cursor: sqlite3.Cursor) -> None:
    """Apply pragmatic defaults for better concurrency/safety."""
    # WAL improves concurrent reads while writing and is safe for local usage.
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.execute("PRAGMA synchronous = NORMAL")


def _ensure_core_schema(cursor: sqlite3.Cursor) -> None:
    """Create baseline tables needed by the container."""
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS app_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            value TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Kept from template (may be unused by the notes app) but is harmless.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _get_table_columns(cursor: sqlite3.Cursor, table_name: str) -> Dict[str, Dict]:
    """Return a mapping of column_name -> PRAGMA table_info row dict."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    cols = {}
    for row in cursor.fetchall():
        # PRAGMA table_info returns: cid, name, type, notnull, dflt_value, pk
        cols[row[1]] = {
            "cid": row[0],
            "name": row[1],
            "type": row[2],
            "notnull": row[3],
            "dflt_value": row[4],
            "pk": row[5],
        }
    return cols


def _ensure_notes_schema(cursor: sqlite3.Cursor) -> None:
    """Create/upgrade the notes schema with search support.

    Notes table design:
    - id: integer primary key
    - title: short label
    - content: full note text
    - created_at: timestamp set at creation time
    - updated_at: timestamp updated on each UPDATE (via trigger)
    - deleted_at: nullable timestamp for soft-delete compatibility (optional future use)

    Search support:
    - Basic LIKE search is supported via indexes on title and created_at/updated_at.
      (SQLite cannot index arbitrary LIKE patterns well; but title prefix queries
       and ordering are improved.)
    - Optional future: migrate to FTS5 virtual table for full-text search; not done
      here to avoid introducing a second storage surface and migration complexity.
    """
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deleted_at TIMESTAMP
        )
        """
    )

    # Deterministic, idempotent upgrades for older DBs that might not have all columns.
    existing_cols = _get_table_columns(cursor, "notes")
    upgrades: List[Tuple[str, str]] = []

    if "title" not in existing_cols:
        upgrades.append(("title", "ALTER TABLE notes ADD COLUMN title TEXT NOT NULL DEFAULT ''"))
    if "content" not in existing_cols:
        upgrades.append(("content", "ALTER TABLE notes ADD COLUMN content TEXT NOT NULL DEFAULT ''"))
    if "created_at" not in existing_cols:
        upgrades.append(
            ("created_at", "ALTER TABLE notes ADD COLUMN created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP")
        )
    if "updated_at" not in existing_cols:
        upgrades.append(
            ("updated_at", "ALTER TABLE notes ADD COLUMN updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP")
        )
    if "deleted_at" not in existing_cols:
        upgrades.append(("deleted_at", "ALTER TABLE notes ADD COLUMN deleted_at TIMESTAMP"))

    for col, ddl in upgrades:
        print(f"Applying schema upgrade: adding notes.{col}")
        cursor.execute(ddl)

    # Ensure updated_at is automatically maintained.
    cursor.execute(
        """
        CREATE TRIGGER IF NOT EXISTS notes_set_updated_at
        AFTER UPDATE ON notes
        FOR EACH ROW
        WHEN NEW.updated_at = OLD.updated_at
        BEGIN
            UPDATE notes
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = NEW.id;
        END
        """
    )

    # Indexes to support common query patterns: list/order, filtering out deleted, and basic search.
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notes_created_at ON notes(created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notes_updated_at ON notes(updated_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notes_deleted_at ON notes(deleted_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notes_title ON notes(title)")

    # Optional helper view for "active" notes (non-deleted). Useful for consistent querying.
    cursor.execute(
        """
        CREATE VIEW IF NOT EXISTS active_notes AS
        SELECT id, title, content, created_at, updated_at
        FROM notes
        WHERE deleted_at IS NULL
        """
    )


def _seed_app_info(cursor: sqlite3.Cursor) -> None:
    """Insert or update baseline app_info values deterministically."""
    cursor.execute(
        "INSERT OR REPLACE INTO app_info (key, value) VALUES (?, ?)",
        ("project_name", "notes_database"),
    )
    cursor.execute(
        "INSERT OR REPLACE INTO app_info (key, value) VALUES (?, ?)",
        ("version", "0.1.0"),
    )
    cursor.execute(
        "INSERT OR REPLACE INTO app_info (key, value) VALUES (?, ?)",
        ("author", "John Doe"),
    )
    cursor.execute(
        "INSERT OR REPLACE INTO app_info (key, value) VALUES (?, ?)",
        ("description", ""),
    )


def _print_database_statistics(cursor: sqlite3.Cursor) -> None:
    """Print basic database statistics."""
    cursor.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    table_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM app_info")
    app_info_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM notes")
    notes_count = cursor.fetchone()[0]

    print("\nDatabase statistics:")
    print(f"  Tables: {table_count}")
    print(f"  App info records: {app_info_count}")
    print(f"  Notes records: {notes_count}")


def _write_connection_info_files(db_name: str) -> None:
    """Write db_connection.txt and db_visualizer/sqlite.env (deterministic outputs)."""
    current_dir = os.getcwd()
    connection_string = f"sqlite:///{current_dir}/{db_name}"

    try:
        with open("db_connection.txt", "w", encoding="utf-8") as f:
            f.write("# SQLite connection methods:\n")
            f.write(f"# Python: sqlite3.connect('{db_name}')\n")
            f.write(f"# Connection string: {connection_string}\n")
            f.write(f"# File path: {current_dir}/{db_name}\n")
        print("Connection information saved to db_connection.txt")
    except Exception as e:
        raise OSError(f"Could not save connection info to db_connection.txt: {e}") from e

    db_path = os.path.abspath(db_name)

    if not os.path.exists("db_visualizer"):
        os.makedirs("db_visualizer", exist_ok=True)
        print("Created db_visualizer directory")

    try:
        with open("db_visualizer/sqlite.env", "w", encoding="utf-8") as f:
            f.write(f'export SQLITE_DB="{db_path}"\n')
        print("Environment variables saved to db_visualizer/sqlite.env")
    except Exception as e:
        raise OSError(f"Could not save environment variables to db_visualizer/sqlite.env: {e}") from e


def _print_sqlite_cli_hint(db_name: str) -> None:
    """Print hint if sqlite3 CLI is available (non-fatal)."""
    try:
        import subprocess

        result = subprocess.run(["which", "sqlite3"], capture_output=True, text=True)
        if result.returncode == 0:
            print("")
            print("SQLite CLI is available. You can also use:")
            print(f"  sqlite3 {db_name}")
    except Exception:
        # Non-critical; ignore.
        pass


if __name__ == "__main__":
    init_database()
