# ui/db_viewer_dialog.py
from __future__ import annotations
import sqlite3, re
from typing import List, Tuple, Dict, Any
from datetime import datetime as _pydt

from PyQt6.QtCore import Qt, QDateTime
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QTableWidget, QTableWidgetItem, QMessageBox, QSpinBox,
    QCheckBox, QFileDialog, QScrollArea, QWidget, QFormLayout, QDateTimeEdit, QLineEdit, QCalendarWidget
)
from PyQt6.QtGui import QTextCharFormat, QColor

from ui.header_editor_dialog import HeaderEditorDialog


def _get_table_columns(conn: sqlite3.Connection, table: str) -> List[Tuple[int, str, str]]:
    cur = conn.cursor()
    cur.execute(f'PRAGMA table_info("{table}")')
    return [(r[0], r[1], r[2] or "") for r in cur.fetchall()]

def _looks_datetime_name(name: str) -> bool:
    name_l = name.lower()
    return any(k in name_l for k in ["datetime", "timestamp", "ts", "date", "received", "sent", "created"])

def _is_numeric_decl(type_decl: str) -> bool:
    t = (type_decl or "").upper()
    return any(k in t for k in ["INT", "REAL", "NUM", "DECIMAL", "DOUBLE", "FLOAT"])

def _slug(s: Any) -> str:
    s = "" if s is None else str(s)
    s = s.strip()
    # Replace spaces and separators with underscore
    s = re.sub(r"[^\w\-\.]+", "_", s, flags=re.UNICODE)
    # collapse repeats
    s = re.sub(r"_+", "_", s)
    return s.strip("_")


class ColumnPicker(QWidget):
    def __init__(self, columns: List[str], parent=None):
        super().__init__(parent)
        self._checks: Dict[str, QCheckBox] = {}
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0)

        btns = QHBoxLayout()
        btn_all = QPushButton("All"); btn_none = QPushButton("None")
        btn_all.clicked.connect(lambda: self._set_all(True))
        btn_none.clicked.connect(lambda: self._set_all(False))
        btns.addWidget(btn_all); btns.addWidget(btn_none); btns.addStretch(1)
        lay.addLayout(btns)

        area = QScrollArea(); area.setWidgetResizable(True)
        inner = QWidget(); grid = QVBoxLayout(inner)
        for c in columns:
            cb = QCheckBox(c); cb.setChecked(True)
            grid.addWidget(cb); self._checks[c] = cb
        grid.addStretch(1)
        area.setWidget(inner); lay.addWidget(area, 1)

    def _set_all(self, val: bool):
        for cb in self._checks.values():
            cb.setChecked(val)

    def selected(self) -> List[str]:
        return [name for name, cb in self._checks.items() if cb.isChecked()]


class ExportDialog(QDialog):
    """
    Export with range, granularity, columns, format, destination and filename pattern.
    Adds: time-column default to 'received_time', min/max from data,
    and calendar coloring (green = dates with data, red = dates without data).
    """
    def __init__(self, db_path: str, table: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export data…")
        self.db_path = db_path
        self.table = table
        self.save_path: str | None = None

        v = QVBoxLayout(self)
        form = QFormLayout(); v.addLayout(form)

        # DB + columns
        try:
            self.conn = sqlite3.connect(self.db_path)
        except Exception as e:
            QMessageBox.critical(self, "Export", f"Failed to open database:\n{e}")
            self.reject(); return

        cols_info = _get_table_columns(self.conn, self.table)
        self.all_cols = [c for _, c, _ in cols_info]
        self.type_map = {c: t for _, c, t in cols_info}

        # --- Time column (prefer 'received_time' if present)
        self.combo_time = QComboBox()
        ordered = sorted(
            self.all_cols,
            key=lambda c: (0 if c == "received_time" else (1 if _looks_datetime_name(c) else 2), c),
        )
        self.combo_time.addItems(ordered)
        if "received_time" in self.all_cols:
            self.combo_time.setCurrentText("received_time")
        form.addRow("Date/Time column:", self.combo_time)

        # --- Range pickers
        self.dt_start = QDateTimeEdit(); self.dt_end = QDateTimeEdit()
        for w in (self.dt_start, self.dt_end):
            w.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            w.setCalendarPopup(True)

        # Legend
        leg = QLabel("Green = dates with data   •   Red = no data in table")
        leg.setStyleSheet("color:#666; font-size:11px;")

        row_range = QHBoxLayout()
        row_range.addWidget(QLabel("From:")); row_range.addWidget(self.dt_start)
        row_range.addSpacing(12); row_range.addWidget(QLabel("To:")); row_range.addWidget(self.dt_end)
        range_wrap = QWidget(); range_wrap.setLayout(row_range)
        form.addRow("Date range:", range_wrap)
        form.addRow("", leg)

        # --- Output options
        self.combo_grain = QComboBox(); self.combo_grain.addItems(["Data points", "Day", "Week", "Year"])
        self.combo_fmt = QComboBox(); self.combo_fmt.addItems(["CSV (*.csv)", "Text (*.txt)"])
        row_out = QHBoxLayout(); row_out.addWidget(self.combo_grain); row_out.addSpacing(12); row_out.addWidget(self.combo_fmt)
        out_wrap = QWidget(); out_wrap.setLayout(row_out)
        form.addRow("Output:", out_wrap)

        # --- Columns
        self.picker = ColumnPicker(self.all_cols, self)
        form.addRow("Columns to include:", self.picker)

        # --- Destination
        pick = QHBoxLayout()
        self.lbl_path = QLabel("No file chosen")
        self.btn_browse = QPushButton("Choose file…")
        self.btn_browse.clicked.connect(self._choose_path)
        pick.addWidget(self.lbl_path, 1); pick.addWidget(self.btn_browse)
        dest_wrap = QWidget(); dest_wrap.setLayout(pick)
        form.addRow("Save to:", dest_wrap)

        # --- Buttons
        btns = QHBoxLayout(); btns.addStretch(1)
        self.btn_export = QPushButton("Export"); self.btn_cancel = QPushButton("Cancel")
        self.btn_export.clicked.connect(self._do_export); self.btn_cancel.clicked.connect(self.reject)
        btns.addWidget(self.btn_export); btns.addWidget(self.btn_cancel)
        v.addLayout(btns)

        # Seed range + calendars from data coverage
        self._load_coverage_and_seed()

        # React to time-column changes
        self.combo_time.currentTextChanged.connect(self._load_coverage_and_seed)

    # ---------- coverage, calendars, seeding ----------
    def _load_coverage_and_seed(self):
        col = (self.combo_time.currentText() or "").strip()
        if not col: return

        min_s, max_s = None, None
        try:
            cur = self.conn.cursor()
            cur.execute(f'SELECT MIN("{col}"), MAX("{col}") FROM "{self.table}" WHERE "{col}" IS NOT NULL')
            r = cur.fetchone()
            if r and r[0] and r[1]:
                min_s, max_s = str(r[0]), str(r[1])
        except Exception:
            pass

        now = QDateTime.currentDateTime()
        def _qdt(s: str | None, default: QDateTime) -> QDateTime:
            if not s: return default
            ss = s
            if len(ss) == 10: ss += " 00:00:00"
            dt = QDateTime.fromString(ss, "yyyy-MM-dd HH:mm:ss")
            return dt if dt.isValid() else default

        dt_min = _qdt(min_s, now.addDays(-1))
        dt_max = _qdt(max_s, now)

        # Enforce range on both editors
        self.dt_start.setMinimumDateTime(dt_min)
        self.dt_start.setMaximumDateTime(dt_max)
        self.dt_end.setMinimumDateTime(dt_min)
        self.dt_end.setMaximumDateTime(dt_max)

        # Default selection = full extent
        self.dt_start.setDateTime(dt_min)
        self.dt_end.setDateTime(dt_max)

        # Build coverage set of QDates that have data
        dates_with_data = set()
        try:
            cur = self.conn.cursor()
            cur.execute(
                f'SELECT DISTINCT date("{col}") AS d FROM "{self.table}" '
                f'WHERE "{col}" IS NOT NULL AND d IS NOT NULL'
            )
            for (d,) in cur.fetchall():
                qd = QDateTime.fromString(str(d) + " 00:00:00", "yyyy-MM-dd HH:mm:ss").date()
                if qd.isValid():
                    dates_with_data.add(qd)
        except Exception:
            pass

        # Apply colored calendars to both pickers
        self._apply_calendar_colors(self.dt_start, dt_min.date(), dt_max.date(), dates_with_data)
        self._apply_calendar_colors(self.dt_end,   dt_min.date(), dt_max.date(), dates_with_data)

    def _apply_calendar_colors(self, dt_edit: QDateTimeEdit, qmin, qmax, data_dates: set):
        cal = QCalendarWidget()
        cal.setGridVisible(True)
        cal.setDateRange(qmin, qmax)

        fmt_green = QTextCharFormat(); fmt_green.setForeground(QColor("#2f9e44"))  # green
        fmt_red   = QTextCharFormat(); fmt_red.setForeground(QColor("#c92a2a"))    # red

        # Color every day from qmin..qmax
        d = qmin
        while d <= qmax:
            cal.setDateTextFormat(d, fmt_green if d in data_dates else fmt_red)
            d = d.addDays(1)

        dt_edit.setCalendarWidget(cal)

    # ---------- rest (unchanged from your version) ----------
    def _choose_path(self):
        if self.combo_fmt.currentText().startswith("CSV"):
            filt = "CSV files (*.csv);;All files (*.*)"; def_ext = ".csv"
        else:
            filt = "Text files (*.txt);;All files (*.*)"; def_ext = ".txt"
        path, _ = QFileDialog.getSaveFileName(self, "Save export to…", "", filt)
        if path:
            if "." not in path.split("/")[-1] and def_ext:
                path += def_ext
            self.save_path = path
            self.lbl_path.setText(path)

    def _do_export(self):
        if not self.save_path:
            QMessageBox.warning(self, "Export", "Please choose a destination file."); return

        time_col = self.combo_time.currentText().strip()
        cols_selected = self.picker.selected()
        if not cols_selected:
            QMessageBox.warning(self, "Export", "Please select at least one column to include."); return
        if self.combo_grain.currentText() == "Data points" and time_col not in cols_selected:
            cols_selected = [time_col] + cols_selected

        start_s = self.dt_start.dateTime().toString("yyyy-MM-dd HH:mm:ss")
        end_s   = self.dt_end.dateTime().toString("yyyy-MM-dd HH:mm:ss")
        fmt = "csv" if self.combo_fmt.currentText().startswith("CSV") else "txt"
        grain = self.combo_grain.currentText()

        try:
            sql, headers, params = self._build_query(grain, time_col, cols_selected, start_s, end_s)
        except Exception as e:
            QMessageBox.critical(self, "Export", f"Failed to build query:\n{e}"); return

        try:
            cur = self.conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()
        except Exception as e:
            QMessageBox.critical(self, "Export", f"Failed to run export query:\n{e}\n\nSQL:\n{sql}")
            return

        try:
            self._write_file(self.save_path, headers, rows, fmt)
        except Exception as e:
            QMessageBox.critical(self, "Export", f"Failed to write file:\n{e}"); return

        QMessageBox.information(self, "Export", f"Exported {len(rows)} rows to:\n{self.save_path}")
        self.accept()

    def _build_query(self, grain: str, time_col: str, cols_selected: List[str],
                     start_s: str, end_s: str) -> Tuple[str, List[str], Tuple[str, str]]:
        where = f'WHERE "{time_col}" >= ? AND "{time_col}" <= ?'
        params = (start_s, end_s)

        if grain == "Data points":
            cols_sql = ", ".join(f'"{c}"' for c in cols_selected)
            sql = f'SELECT {cols_sql} FROM "{self.table}" {where} ORDER BY "{time_col}" ASC'
            headers = cols_selected
            return sql, headers, params

        if grain == "Day":
            bucket_expr = f'date("{time_col}")'; bucket_name = "day"
        elif grain == "Week":
            bucket_expr = f"strftime('%Y-%W', \"{time_col}\")"; bucket_name = "year_week"
        elif grain == "Year":
            bucket_expr = f"strftime('%Y', \"{time_col}\")"; bucket_name = "year"
        else:
            raise ValueError(f"Unknown granularity: {grain}")

        numeric_cols = [c for c in cols_selected if _is_numeric_decl(self.type_map.get(c, ""))]
        select_aggs = [f"{bucket_expr} AS {bucket_name}", "COUNT(*) AS count"]
        headers = [bucket_name, "count"]
        for c in numeric_cols:
            select_aggs.append(f'AVG("{c}") AS "{c}"'); headers.append(c)

        sql = (
            f'SELECT {", ".join(select_aggs)} FROM "{self.table}" '
            f'{where} GROUP BY {bucket_expr} ORDER BY {bucket_expr} ASC'
        )
        return sql, headers, params

    def _write_file(self, path: str, headers: List[str], rows: List[tuple], fmt: str):
        import csv
        delimiter = "," if fmt == "csv" else "\t"
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, delimiter=delimiter)
            writer.writerow(headers)
            for r in rows:
                writer.writerow(["" if x is None else x for x in r])



class DBViewerDialog(QDialog):
    def __init__(self, db_path: str, parent=None, preselect_table: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Database Viewer")
        self.setMinimumSize(900, 600)
        self.db_path = db_path

        v = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Table:"))
        self.table_combo = QComboBox(); top.addWidget(self.table_combo, 1)
        top.addWidget(QLabel("Rows:"))
        self.spin_rows = QSpinBox(); self.spin_rows.setRange(1, 100000); self.spin_rows.setValue(1000)
        top.addWidget(self.spin_rows)
        self.btn_refresh = QPushButton("Refresh"); top.addWidget(self.btn_refresh)
        self.btn_headers = QPushButton("Rename Columns…"); top.addWidget(self.btn_headers)
        self.btn_export = QPushButton("Export…"); top.addWidget(self.btn_export)
        v.addLayout(top)

        self.grid = QTableWidget(0, 0)
        self.grid.setAlternatingRowColors(True)
        self.grid.setSortingEnabled(True)
        v.addWidget(self.grid, 1)

        self.btn_refresh.clicked.connect(self._reload)
        self.btn_headers.clicked.connect(self._open_headers)
        self.btn_export.clicked.connect(self._open_export)
        self.table_combo.currentTextChanged.connect(lambda _: self._reload())

        self._load_tables(preselect_table)
        self._reload()

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
            QMessageBox.critical(self, "Database", f"Failed to list tables:\n{e}")
            tables = []

        self.table_combo.clear()
        self.table_combo.addItems(tables)
        if preselect and preselect in tables:
            self.table_combo.setCurrentText(preselect)

    def _reload(self):
        table = self.table_combo.currentText().strip()
        if not table:
            return
        limit = int(self.spin_rows.value())

        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute(f'SELECT * FROM "{table}" LIMIT {limit}')
            rows = cur.fetchall()
            col_names = [d[0] for d in cur.description]
            conn.close()
        except Exception as e:
            QMessageBox.critical(self, "Database", f"Failed to read rows:\n{e}")
            return

        self.grid.clear()
        self.grid.setColumnCount(len(col_names))
        self.grid.setHorizontalHeaderLabels(col_names)
        self.grid.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                self.grid.setItem(r, c, QTableWidgetItem("" if val is None else str(val)))

    def _open_headers(self):
        table = self.table_combo.currentText().strip()
        if not table:
            return
        dlg = HeaderEditorDialog(self.db_path, self, preselect_table=table)
        if dlg.exec() == dlg.DialogCode.Accepted:
            self._reload()

    def _open_export(self):
        table = self.table_combo.currentText().strip()
        if not table:
            QMessageBox.information(self, "Export", "Please select a table first.")
            return
        dlg = ExportDialog(self.db_path, table, self)
        dlg.exec()
