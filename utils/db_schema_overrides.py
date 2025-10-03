# utils/db_schema_overrides.py
from __future__ import annotations
import sqlite3
import re
from typing import Dict, List, Tuple, Optional

META_TABLE = "_schema_header_overrides"   # stores desired display headers per table
SUFFIX_VIEW = "__display"                 # per-table view suffix

_SQL_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

def _quote_ident(name: str) -> str:
    # Double-quote SQL identifiers; allow spaces etc.
    # Also collapse embedded double-quotes per SQLite conventions
    return '"' + (name or "").replace('"', '""') + '"'

def view_name_for(table: str) -> str:
    return f"{table}{SUFFIX_VIEW}"

def ensure_meta_tables(cur: sqlite3.Cursor) -> None:
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {META_TABLE} (
            table_name TEXT NOT NULL,
            internal_name TEXT NOT NULL,   -- the real column name in the base table
            display_name TEXT NOT NULL,    -- the user-friendly header shown in GUI
            PRIMARY KEY (table_name, internal_name)
        )
    """)

def get_internal_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    cur = conn.cursor()
    cur.execute(f'PRAGMA table_info({_quote_ident(table)})')
    cols = [r[1] for r in cur.fetchall()]
    return cols

def get_overrides(conn: sqlite3.Connection, table: str) -> Dict[str, str]:
    cur = conn.cursor()
    ensure_meta_tables(cur)
    cur.execute(f"""
        SELECT internal_name, display_name
        FROM {META_TABLE}
        WHERE table_name = ?
    """, (table,))
    return {r[0]: r[1] for r in cur.fetchall()}

def set_overrides(conn: sqlite3.Connection, table: str, mapping: Dict[str, str]) -> None:
    # mapping: internal_name -> display_name
    cur = conn.cursor()
    ensure_meta_tables(cur)
    for k, v in mapping.items():
        cur.execute(f"""
            INSERT INTO {META_TABLE} (table_name, internal_name, display_name)
            VALUES (?, ?, ?)
            ON CONFLICT(table_name, internal_name) DO UPDATE SET display_name=excluded.display_name
        """, (table, k, v))
    conn.commit()

def compute_effective_headers(internal_cols: List[str], overrides: Dict[str, str]) -> List[Tuple[str, str]]:
    """
    Returns list of (internal_name, display_name) in the same order as internal_cols.
    If no override present, display_name == internal_name.
    """
    result: List[Tuple[str, str]] = []
    for c in internal_cols:
        d = overrides.get(c, c)
        result.append((c, d))
    return result

def rebuild_display_view(conn: sqlite3.Connection, table: str) -> None:
    """
    Drop and recreate the display view:
      CREATE VIEW {table}__display AS
        SELECT col1 AS "Header 1", col2 AS "Header 2", ...
        FROM {table};
    We also include id, subject, sender, received_time if present.
    """
    internal_cols = get_internal_columns(conn, table)
    overrides = get_overrides(conn, table)
    pairs = compute_effective_headers(internal_cols, overrides)

    cur = conn.cursor()
    vname = view_name_for(table)
    cur.execute(f'DROP VIEW IF EXISTS {_quote_ident(vname)}')

    select_exprs = []
    for internal, display in pairs:
        select_exprs.append(f'{_quote_ident(internal)} AS {_quote_ident(display)}')

    sql = f'CREATE VIEW {_quote_ident(vname)} AS SELECT ' + ', '.join(select_exprs) + f' FROM {_quote_ident(table)}'
    cur.execute(sql)
    conn.commit()

def table_or_view_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.cursor()
    cur.execute("""
        SELECT 1
        FROM sqlite_schema
        WHERE (type='table' OR type='view') AND name=?
        LIMIT 1
    """, (name,))
    return cur.fetchone() is not None
