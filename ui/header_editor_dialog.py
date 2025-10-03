# ui/header_editor_dialog.py
from __future__ import annotations
import sqlite3
from typing import Dict, List
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QTableWidget, QTableWidgetItem, QMessageBox
)

RESERVED = {"id", "subject", "sender", "received_time"}

def _pragma_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    cur = conn.cursor()
    cur.execute(f'PRAGMA table_info("{table}")')
    return [r[1] for r in cur.fetchall()]

def _data_columns_only(cols: List[str]) -> List[str]:
    return [c for c in cols if c not in RESERVED]

class HeaderEditorDialog(QDialog):
    """
    Physically renames columns in-place using ALTER TABLE RENAME COLUMN.
    Only allows renaming the 18 data columns; reserved columns are locked.
    """
    def __init__(self, db_path: str, parent=None, preselect_table: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Rename Table Columns (Physical)")
        self.setMinimumWidth(760)
        self.db_path = db_path

        v = QVBoxLayout(self)

        # Picker
        row = QHBoxLayout()
        row.addWidget(QLabel("Table:"))
        self.table_combo = QComboBox()
        row.addWidget(self.table_combo, 1)
        v.addLayout(row)

        # Grid
        self.grid = QTableWidget(0, 2)
        self.grid.setHorizontalHeaderLabels(["Current name", "New name"])
        self.grid.horizontalHeader().setStretchLastSection(True)
        v.addWidget(self.grid, 1)

        # Buttons
        btns = QHBoxLayout()
        btns.addStretch(1)
        self.btn_save = QPushButton("Apply Renames")
        self.btn_close = QPushButton("Close")
        btns.addWidget(self.btn_save)
        btns.addWidget(self.btn_close)
        v.addLayout(btns)

        self.btn_close.clicked.connect(self.close)
        self.btn_save.clicked.connect(self._apply)

        self.table_combo.currentTextChanged.connect(self._load_columns)

        self._load_tables(preselect_table)

    def _load_tables(self, preselect: str | None):
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("""
                SELECT name FROM sqlite_schema
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
            """)
            tables = [r[0] for r in cur.fetchall()]
            conn.close()
        except Exception as e:
            QMessageBox.critical(self, "Database", f"Failed to read tables:\n{e}")
            tables = []

        self.table_combo.clear()
        self.table_combo.addItems(tables)
        if preselect and preselect in tables:
            self.table_combo.setCurrentText(preselect)
        elif tables:
            self._load_columns(tables[0])

    def _load_columns(self, table: str):
        self.grid.setRowCount(0)
        if not table:
            return
        try:
            conn = sqlite3.connect(self.db_path)
            cols = _pragma_columns(conn, table)
            conn.close()
        except Exception as e:
            QMessageBox.critical(self, "Database", f"Failed to read columns:\n{e}")
            return

        # Only data columns can be renamed
        for c in _data_columns_only(cols):
            row = self.grid.rowCount()
            self.grid.insertRow(row)
            cur_item = QTableWidgetItem(c)
            cur_item.setFlags(cur_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.grid.setItem(row, 0, cur_item)
            new_item = QTableWidgetItem(c)
            self.grid.setItem(row, 1, new_item)

    def _apply(self):
        table = self.table_combo.currentText().strip()
        if not table:
            return

        renames: Dict[str, str] = {}
        for r in range(self.grid.rowCount()):
            old = (self.grid.item(r, 0).text() or "").strip()
            new = (self.grid.item(r, 1).text() or "").strip()
            if not old or not new or old == new:
                continue
            if new in RESERVED:
                QMessageBox.critical(self, "Invalid name", f"'{new}' is reserved.")
                return
            renames[old] = new

        if not renames:
            QMessageBox.information(self, "Headers", "No changes.")
            self.accept()
            return

        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            # Validate that no duplicates will be created
            cols_now = _pragma_columns(conn, table)
            data_now = _data_columns_only(cols_now)
            new_names = [renames.get(c, c) for c in data_now]
            if len(set(new_names)) != len(new_names):
                raise RuntimeError("Duplicate column names in result; please ensure all names are unique.")

            # Apply renames one by one (SQLite supports this since 3.25+)
            for old, new in renames.items():
                cur.execute(f'ALTER TABLE "{table}" RENAME COLUMN "{old}" TO "{new}"')

            conn.commit()
            conn.close()
            QMessageBox.information(self, "Headers", "Column names updated.")
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Headers", f"Failed to apply renames:\n{e}")
