# Create_tabs.py
import datetime
import pandas as pd
from typing import Optional
import os, time, sqlite3, traceback


from PyQt6.QtCore import Qt, QTime, QDateTime, QTimer, QDate, QPoint
from PyQt6.QtGui import QTextCharFormat, QBrush, QColor, QIcon, QPixmap, QPainter, QPen
from PyQt6.QtWidgets import (
    QWidget, QLabel, QDateEdit, QSlider, QMessageBox,
    QScrollArea, QVBoxLayout, QTabWidget, QGroupBox, QGridLayout,
    QToolButton, QInputDialog, QFrame, QStackedLayout, QSizePolicy,
    QStackedWidget, QSplitter, QHBoxLayout, QListWidget, QListWidgetItem
)
import uuid


from utils.alerts.alerts_tab import AlertsTab
from utils.chart_board import ChartBoard
# Ensure alert types self-register
from utils.alerts import distance_alert, threshold_alert, stale_alert, REGISTRY
from utils.time_settings import local_zone, offset_label, parse_series_to_local_naive
from utils.time_settings import get_config




def _clean_db_path(p) -> str:
    """Make sure the DB path is a clean, normal Windows path."""
    if not isinstance(p, (str, os.PathLike)):
        p = str(p)
    p = os.fspath(p)
    # strip wrapping quotes/whitespace and normalize separators
    p = p.strip().strip('"').strip("'")
    p = os.path.normpath(p)
    return p

def _connect_db(db_path: str, *, ro: bool = False) -> sqlite3.Connection:
    """Open SQLite robustly; try URI fallback on Windows if needed."""
    p = _clean_db_path(db_path)
    try:
        if ro:
            # explicit read-only prevents accidental file creation
            return sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        return sqlite3.connect(p)
    except OSError as e:
        # Errno 22: try a simpler URI open as a fallback
        if getattr(e, "errno", None) == 22:
            return sqlite3.connect(f"file:{p}", uri=True)
        raise



# --- put near top of Create_tabs.py (after imports) ---
def _make_legacy_board(db_path: str):
    """Adapter with .tiles -> objects that expose .config().table"""
    class _Cfg:
        def __init__(self, table): self.table = table

    class _Tile:
        def __init__(self, table): self._table = table
        def config(self): return _Cfg(self._table)

    class _Board:
        def __init__(self, tables): self.tiles = [ _Tile(t) for t in tables ]

    return _Board(_list_user_tables(db_path))


def _make_summary_page(parent, db_path: str, alerts_provider):
    """
    Try the new utils.summary_page.SummaryPage API first,
    then the old API, and finally fall back to our local SummaryTab.
    """
    try:
        from utils.summary_page import SummaryPage as SP
        try:
            # NEW API
            return SP(db_path=db_path, alerts_provider=alerts_provider, parent=parent)
        except TypeError:
            # OLD API
            from utils.time_settings import get_config
            return SP(
                get_config(),                # cfg
                alerts_provider,             # alerts_provider
                _make_legacy_board(db_path), # board
                db_path,                     # db_path
                parent                       # parent
            )
    except Exception as e:
        # Anything goes wrong? Use our minimal, safe SummaryTab.
        print(f"[SummaryPage] falling back to local SummaryTab: {e!r}")
        return SummaryTab(db_path)


def _connect_sqlite_robust(db_path: str, retries: int = 5, base_sleep: float = 0.2) -> sqlite3.Connection:
    """
    Connect to SQLite with short retries to dodge transient Windows races:
    - [Errno 22] Invalid argument
    - database is locked / busy / unable to open
    """
    abs_path = os.path.abspath(db_path)
    last_err = None
    for attempt in range(1, int(retries) + 1):
        try:
            return sqlite3.connect(abs_path, timeout=10)
        except (sqlite3.OperationalError, OSError) as e:
            last_err = e
            msg = str(e).lower()
            # transient cases worth retrying
            if (isinstance(e, OSError) and getattr(e, "errno", None) == 22) or \
               ("locked" in msg) or ("busy" in msg) or ("unable to open" in msg):
                time.sleep(base_sleep * attempt)  # backoff
                continue
            raise
    # out of retries
    raise last_err or RuntimeError("SQLite connect failed")




def _local_tzinfo():
    return local_zone()

def _utc_offset_label() -> str:
    return offset_label()

# -------- Overlay panel (semi-invisible arrows + center “＋”) ----------
class _BoardOverlay(QWidget):
    """
    Edge hover controls to add/move panels on a ChartBoard.
    Shows a big center “＋” only when the board is empty.
    """
    def __init__(
        self,
        on_add_chart_center,
        on_add_edge: dict,
        on_move_edge: dict,
        parent=None,
    ):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

        self.on_add_chart_center = on_add_chart_center
        self.on_add_edge = on_add_edge
        self.on_move_edge = on_move_edge

        self._last_zone: str | None = None
        self._hide_armed = False


        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(120)
        self._hide_timer.timeout.connect(self._hide_all_now)

        self.center_plus = QToolButton(self)
        self.center_plus.setText("＋")
        self.center_plus.setToolTip("Add chart")
        self.center_plus.setCursor(Qt.CursorShape.PointingHandCursor)
        self.center_plus.clicked.connect(lambda: self.on_add_chart_center() if self.on_add_chart_center else None)
        self.center_plus.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)

        self.edge_widgets = {}

        def mk_edge_pair(edge: str, arrow_type: Qt.ArrowType):
            move_btn = QToolButton(self)
            move_btn.setArrowType(arrow_type)
            move_btn.setFixedSize(28, 28)
            move_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            move_btn.setVisible(False)
            move_btn.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)
            add_btn = QToolButton(self)
            add_btn.setText("＋")
            add_btn.setFixedSize(22, 22)
            add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            add_btn.setVisible(False)
            add_btn.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)

            move_btn.clicked.connect(lambda e=None, d=edge: self.on_move_edge.get(d, lambda: None)())
            add_btn.clicked.connect(lambda e=None, d=edge: self.on_add_edge.get(d, lambda: None)())

            self.edge_widgets[edge] = {"move": move_btn, "add": add_btn}

        mk_edge_pair("up", Qt.ArrowType.UpArrow)
        mk_edge_pair("down", Qt.ArrowType.DownArrow)
        mk_edge_pair("left", Qt.ArrowType.LeftArrow)
        mk_edge_pair("right", Qt.ArrowType.RightArrow)

    def set_empty(self, is_empty: bool):
        self.center_plus.setVisible(bool(is_empty))

    def resizeEvent(self, _):
        r = self.rect()
        cx, cy = r.center().x(), r.center().y()
        self.center_plus.setGeometry(cx - 20, cy - 20, 40, 40)

        pad = 6
        up = self.edge_widgets["up"];    up["move"].move(cx - 28, r.top() + pad);           up["add"].move(cx + 4,  r.top() + pad + 3)
        down = self.edge_widgets["down"];down["move"].move(cx - 28, r.bottom() - 28 - pad); down["add"].move(cx + 4, r.bottom() - 28 - pad + 3)
        left = self.edge_widgets["left"];left["move"].move(r.left() + pad, cy - 28);        left["add"].move(r.left() + pad + 3, cy + 4)
        right = self.edge_widgets["right"]; right["move"].move(r.right() - 28 - pad, cy - 28); right["add"].move(r.right() - 28 - pad + 3, cy + 4)

    def enterEvent(self, e):
        self._hide_timer.stop()
        self._hide_armed = False
        self._update_zone_from_pos(e.position().toPoint())

    def mouseMoveEvent(self, e):
        p = e.position().toPoint()
        if self._point_in_widget(self.center_plus, p):
            return
        for edge in self.edge_widgets.values():
            if self._point_in_widget(edge["move"], p) or self._point_in_widget(edge["add"], p):
                return
        self._update_zone_from_pos(p)

    def leaveEvent(self, _):
        self._hide_armed = True
        self._hide_timer.start()

    def _hide_all_now(self):
        if not self._hide_armed:
            return
        for edge in self.edge_widgets.values():
            if edge["move"].isVisible(): edge["move"].setVisible(False)
            if edge["add"].isVisible():  edge["add"].setVisible(False)
        self._last_zone = None
        self._hide_armed = False
        self.update()

    @staticmethod
    def _point_in_widget(w: QWidget, p) -> bool:
        return bool(w and w.isVisible() and w.geometry().contains(p))

    def _update_zone_from_pos(self, pos):
        r = self.rect()
        margin = 40
        zone = None
        if pos.y() < margin: zone = "up"
        elif pos.y() > r.height() - margin: zone = "down"
        elif pos.x() < margin: zone = "left"
        elif pos.x() > r.width() - margin: zone = "right"

        if zone == self._last_zone:
            return
        self._last_zone = zone

        for k, pair in self.edge_widgets.items():
            want = (k == zone)
            if pair["move"].isVisible() != want: pair["move"].setVisible(want)
            if pair["add"].isVisible()  != want: pair["add"].setVisible(want)
        self.update()


# ---------------- Utility helpers ----------------

def _find_column_case(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    lower = {c.lower(): c for c in df.columns}
    for name in candidates:
        if name in lower:
            return lower[name]
    return None

def _last_known_lat_lon(df: pd.DataFrame, dt_col: Optional[str]) -> tuple[Optional[float], Optional[float]]:
    if df.empty:
        return None, None
    lat_name = _find_column_case(df, ["lat", "latitude", "lat_dd", "lat_deg"])
    lon_name = _find_column_case(df, ["lon", "longitude", "long", "lon_dd", "lon_deg"])
    if not lat_name or not lon_name:
        return None, None
    dff = df.copy()
    if dt_col and dt_col in dff.columns:
        try:
            dff = dff.sort_values(by=dt_col, kind="stable")
        except Exception:
            pass
    dff = dff.dropna(subset=[lat_name, lon_name])
    if dff.empty:
        return None, None
    row = dff.iloc[-1]
    try:
        return float(row[lat_name]), float(row[lon_name])
    except Exception:
        return None, None

def _list_user_tables(db_path: str) -> list[str]:
    try:
        with _connect_sqlite_robust(db_path) as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            return [r[0] for r in cur.fetchall()]
    except Exception:
        return []



# ---------------- TableTab (per-project) ----------------
class TableTab(QWidget):
    """
    Per-table workspace:
      - Project overview (summary + configurable ChartBoard)
      - Charts (filters + configurable ChartBoard)
      - Alerts
      - trailing “＋” to add custom tabs (filters + board)
    """
    def __init__(self, db_path: str, table_name: str):
        super().__init__()
        self.db_path = db_path
        self.table_name = table_name

        self.inner_tabs: QTabWidget | None = None
        self.start_date_edit: QDateEdit | None = None
        self.end_date_edit: QDateEdit | None = None
        self.start_time_slider: QSlider | None = None
        self.end_time_slider: QSlider | None = None
        self.start_time_label: QLabel | None = None
        self.end_time_label: QLabel | None = None

        self.ov_start_val: QLabel | None = None
        self.ov_now_val: QLabel | None = None
        self.ov_last_val: QLabel | None = None

        self.overview_board: ChartBoard | None = None
        self.charts_board: ChartBoard | None = None
        self.extra_tabs: list[tuple[str, ChartBoard]] = []

        self.timer: QTimer | None = None

        self.df = self.load_data()
        self.datetime_col: str | None = None
        self.start_datetime = None
        self.end_datetime = None

        self._detect_datetime_column()
        if self.datetime_col and self.datetime_col in self.df.columns:
            col = parse_series_to_local_naive(self.df[self.datetime_col]).dropna()
            if not col.empty:
                self.start_datetime = col.min()
                self.end_datetime = col.max()

        self.init_ui()
        self.destroyed.connect(self._cleanup)

    @staticmethod
    def _alive(obj) -> bool:
        if obj is None:
            return False
        try:
            _ = obj.parent()
            return True
        except Exception:
            return False

    def _parse_dt_flex_best(self, s: pd.Series) -> pd.Series:
        # only convert tz-aware ones to local, and return tz-naive local datetimes.
        return parse_series_to_local_naive(s)

    def _choose_best_datetime_column(self) -> tuple[Optional[str], Optional[pd.Series]]:
        """
        Pick a datetime column, preferring common names (timestamp/received_time/datetime/time/date).
        Treat naive strings as local Irish time; only convert tz-aware ones.
        """
        if self.df.empty:
            return None, None

        cols = list(self.df.columns)
        lower = {c.lower(): c for c in cols}
        preferred_names = ["timestamp", "received_time", "datetime", "time", "date"]
        preferred = [lower[n] for n in preferred_names if n in lower]

        # Build trial order: preferred first, then already-datetime dtypes, then the rest.
        rest = [c for c in cols if c not in preferred]
        dt_dtypes = [c for c in rest if pd.api.types.is_datetime64_any_dtype(self.df[c])]
        remaining = [c for c in rest if c not in dt_dtypes]
        trial = preferred + dt_dtypes + remaining

        best_col = None
        best_series = None
        best_ok = -1
        best_is_preferred = False
        pref_set = set(preferred)

        for c in trial:
            s = self.df[c]
            parsed = s if pd.api.types.is_datetime64_any_dtype(s) else parse_series_to_local_naive(s)
            ok = int(pd.Series(parsed).notna().sum())
            is_pref = c in pref_set

            # Prefer more parsed values; on ties, prefer preferred-name columns
            if ok > best_ok or (ok == best_ok and is_pref and not best_is_preferred):
                best_ok = ok
                best_col = c
                best_series = parsed
                best_is_preferred = is_pref

        if best_series is None or best_ok <= 0:
            return None, None

        # Ensure plain tz-naive local datetimes
        try:
            best_series = pd.to_datetime(best_series, errors="coerce")
        except Exception:
            pass
        return best_col, best_series

    def load_data(self) -> pd.DataFrame:
        try:
            conn = _connect_sqlite_robust(self.db_path)
            df = pd.read_sql_query(f'SELECT * FROM "{self.table_name}"', conn)
            conn.close()
            return df
        except Exception as e:
            QMessageBox.critical(self, "DB Error", f"Failed to load table {self.table_name}:\n{e}")
            return pd.DataFrame()

    def init_ui(self):
        outer_layout = QVBoxLayout(self)
        self.inner_tabs = QTabWidget(self)

        # ===== Project overview =====
        overview_tab = QWidget(self)
        overview_v = QVBoxLayout(overview_tab)

        dates = QGridLayout()
        self.ov_start_val = QLabel("—")
        self.ov_now_val = QLabel("—")
        self.ov_last_val = QLabel("—")
        self.ov_since_val = QLabel("—")

        dates.addWidget(QLabel("Start date:"), 0, 0);
        dates.addWidget(self.ov_start_val, 0, 1)
        # Removed the explicit UTC offset label here
        dates.addWidget(QLabel("Current date:"), 0, 2);
        dates.addWidget(self.ov_now_val, 0, 3)

        dates.addWidget(QLabel("Last data received:"), 1, 0);
        dates.addWidget(self.ov_last_val, 1, 1)
        dates.addWidget(QLabel("Time since last email/avg:"), 1, 2);
        dates.addWidget(self.ov_since_val, 1, 3)

        overview_v.addLayout(dates)

        ov_scroll, self.overview_board, self._overview_overlay = self._build_board_with_overlay()
        overview_v.addWidget(ov_scroll)

        # ===== Charts =====
        charts_tab = QWidget(self)
        charts_v = QVBoxLayout(charts_tab)

        filters = self._build_filters_widget(charts_tab)
        charts_v.addWidget(filters)

        charts_scroll, self.charts_board, self._charts_overlay = self._build_board_with_overlay()
        charts_v.addWidget(charts_scroll)

        # Alerts
        self.alerts_tab = AlertsTab(self, self.db_path, logger=None)

        self.inner_tabs.addTab(overview_tab, "Project overview")
        self.inner_tabs.addTab(charts_tab, "Charts")
        self.inner_tabs.addTab(self.alerts_tab, "Alerts")

        # Trailing “＋” tab
        self.plus_tab = QWidget(self)
        self.inner_tabs.addTab(self.plus_tab, "＋")
        self.inner_tabs.currentChanged.connect(self._maybe_add_new_tab)

        outer_layout.addWidget(self.inner_tabs)
        self.setLayout(outer_layout)

        self.init_timer()
        self.update_time_labels()
        self._refresh_overview()
        self.shade_calendar_dates()

    def _build_board_with_overlay(self):
        container = QWidget()
        stack = QStackedLayout(container)
        stack.setStackingMode(QStackedLayout.StackingMode.StackAll)

        host = QWidget()
        host_v = QVBoxLayout(host)
        host_v.setContentsMargins(0, 0, 0, 0)

        board = None
        try:
            board = ChartBoard(
                get_df=lambda: self.filter_df_by_datetime(),
                columns=list(self.df.columns),
                get_df_full=lambda: self.df,
                parent=host,
            )
            if hasattr(board, "disable_builtin_empty_message"):
                board.disable_builtin_empty_message()
            # let the chart board shrink freely
            board.setMinimumSize(0, 0)
            board.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            host_v.addWidget(board)
        except Exception as e:
            err = QLabel(f"ChartBoard failed to load:\n{e}")
            host_v.addWidget(err)

        overlay = _BoardOverlay(
            on_add_chart_center=(lambda: board._add_chart_dialog()) if board else None,
            on_add_edge={
                "up": (lambda: board.add_panel("up")) if board else None,
                "down": (lambda: board.add_panel("down")) if board else None,
                "left": (lambda: board.add_panel("left")) if board else None,
                "right": (lambda: board.add_panel("right")) if board else None,
            },
            on_move_edge={
                "left": (lambda: board.move_focused("left")) if board else None,
                "right": (lambda: board.move_focused("right")) if board else None,
                "up": (lambda: board.move_focused("up")) if board else None,
                "down": (lambda: board.move_focused("down")) if board else None,
            },
        )
        overlay.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        overlay.setMinimumHeight(100)  # was 280; allow the page to get smaller

        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.NoFrame)
        fvl = QVBoxLayout(frame)
        fvl.setContentsMargins(0, 0, 0, 0)
        fvl.addWidget(overlay)

        stack.addWidget(host)
        stack.addWidget(frame)

        self._sync_overlay_visibility(board, stack, overlay)
        if board and hasattr(board, "changed"):
            board.changed.connect(lambda: self._sync_overlay_visibility(board, stack, overlay))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        # critical so the scroll area doesn’t enforce a large minimum
        scroll.setMinimumSize(0, 0)
        scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        return scroll, board, overlay

    def _sync_overlay_visibility(self, board: ChartBoard | None, _stack: QStackedLayout,
                                 overlay: _BoardOverlay | None = None):
        try:
            if not board:
                if overlay: overlay.set_empty(True)
                return
            state = board.export_state()
            has_any = False
            if isinstance(state, dict):
                if "charts" in state and isinstance(state["charts"], list):
                    has_any = len(state["charts"]) > 0
                elif "rows" in state and isinstance(state["rows"], list):
                    has_any = any(len(r.get("charts", [])) > 0 for r in state["rows"])
            if overlay: overlay.set_empty(not has_any)
        except Exception:
            if overlay: overlay.set_empty(True)

    def _board_add_chart(self, board: ChartBoard | None):
        if not board: return
        if hasattr(board, "_add_chart_dialog"):
            board._add_chart_dialog()
        elif hasattr(board, "add_chart"):
            board.add_chart()
        else:
            QMessageBox.information(self, "Add chart", "This board doesn't expose an add-chart API.")

    def _board_add_row(self, board: ChartBoard | None, above: bool):
        if not board: return
        if hasattr(board, "_add_row_internal"):
            board._add_row_internal()
        elif hasattr(board, "add_row"):
            board.add_row(position=("above" if above else "below"))
        else:
            self._board_add_chart(board)

    def _build_filters_widget(self, parent: QWidget):
        box = QGroupBox("Filters", parent)
        h = QHBoxLayout(box)

        self.start_date_edit = QDateEdit(parent); self.start_date_edit.setCalendarPopup(True)
        self.end_date_edit   = QDateEdit(parent); self.end_date_edit.setCalendarPopup(True)

        if self.start_datetime is not None and pd.notna(self.start_datetime):
            sd = QDate(self.start_datetime.year, self.start_datetime.month, self.start_datetime.day)
            self.start_date_edit.setDate(sd)
        if self.end_datetime is not None and pd.notna(self.end_datetime):
            ed = QDate(self.end_datetime.year, self.end_datetime.month, self.end_datetime.day)
            self.end_date_edit.setDate(ed)

        self.start_time_slider = QSlider(Qt.Orientation.Horizontal, parent)
        self.start_time_slider.setRange(0, 86399); self.start_time_slider.setValue(0)
        self.end_time_slider = QSlider(Qt.Orientation.Horizontal, parent)
        self.end_time_slider.setRange(0, 86399); self.end_time_slider.setValue(86399)
        self.start_time_label = QLabel("00:00:00", parent)
        self.end_time_label = QLabel("23:59:59", parent)

        h.addWidget(QLabel("Start Date:", parent)); h.addWidget(self.start_date_edit)
        h.addWidget(QLabel("Start Time:", parent)); h.addWidget(self.start_time_slider); h.addWidget(self.start_time_label)
        h.addSpacing(12)
        h.addWidget(QLabel("End Date:", parent)); h.addWidget(self.end_date_edit)
        h.addWidget(QLabel("End Time:", parent)); h.addWidget(self.end_time_slider); h.addWidget(self.end_time_label)

        self.start_date_edit.dateChanged.connect(self.on_date_time_changed)
        self.end_date_edit.dateChanged.connect(self.on_date_time_changed)
        self.start_time_slider.valueChanged.connect(self.on_time_slider_changed)
        self.end_time_slider.valueChanged.connect(self.on_time_slider_changed)
        return box

    def _maybe_add_new_tab(self, index: int):
        if self.inner_tabs is None:
            return
        if index != self.inner_tabs.count() - 1:
            return
        name, ok = QInputDialog.getText(self, "New tab", "Tab name:")
        if not ok or not name.strip():
            self.inner_tabs.setCurrentIndex(0)
            return
        tab_name = name.strip()

        tab = QWidget(self)
        v = QVBoxLayout(tab)
        filters_box = self._build_filters_widget(tab)
        v.addWidget(filters_box)
        scroll, board, _overlay = self._build_board_with_overlay()
        v.addWidget(scroll)

        if board:
            self.extra_tabs.append((tab_name, board))

        plus_idx = self.inner_tabs.count() - 1
        self.inner_tabs.insertTab(plus_idx, tab, tab_name)
        self.inner_tabs.setCurrentIndex(plus_idx)

    def _cleanup(self, *_):
        if self.timer is not None:
            try:
                self.timer.stop()
            except Exception:
                pass
            self.timer = None

        for w in (self.start_date_edit, self.end_date_edit, self.start_time_slider, self.end_time_slider):
            if self._alive(w):
                try:
                    w.disconnect()
                except Exception:
                    pass

        self.start_date_edit = None
        self.end_date_edit = None
        self.start_time_slider = None
        self.end_time_slider = None
        self.start_time_label = None
        self.end_time_label = None
        self.ov_start_val = None
        self.ov_now_val = None
        self.ov_last_val = None
        self.ov_since_val = None
        self.overview_board = None
        self.charts_board = None
        self.inner_tabs = None

    def _detect_datetime_column(self):
        name, parsed = self._choose_best_datetime_column()
        if name is None or parsed is None or parsed.notna().sum() == 0:
            self.datetime_col = None
            return
        self.df["__dt_iso"] = parsed
        self.datetime_col = "__dt_iso"
        try:
            non_na = parsed.dropna()
            if not non_na.empty:
                self.start_datetime = non_na.min()
                self.end_datetime = non_na.max()
        except Exception:
            pass

    def init_timer(self):
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._refresh_overview)
        # Only run the timer when the tab is actually visible
        if self.isVisible():
            self.timer.start()

    def reload_df_light(self):
        """
        Reload this table's DataFrame and refresh UI widgets/boards
        without tearing down/recreating the tab.
        """
        try:
            new_df = self.load_data()
        except Exception:
            new_df = pd.DataFrame()

        self.df = new_df if isinstance(new_df, pd.DataFrame) else pd.DataFrame()

        # Re-detect datetime column + date bounds (used by filters/labels)
        self._detect_datetime_column()

        # Refresh overview labels + calendars
        try:
            self.update_time_labels()
        except Exception:
            pass
        try:
            self._refresh_overview()
        except Exception:
            pass
        try:
            self.shade_calendar_dates()
        except Exception:
            pass

        # Refresh boards if present
        for b in (self.overview_board, self.charts_board):
            try:
                if b:
                    b.refresh_all()
            except Exception:
                pass

        # Nudge Alerts tab if it exposes a refresh hook
        try:
            if hasattr(self, "alerts_tab") and hasattr(self.alerts_tab, "refresh"):
                self.alerts_tab.refresh()
        except Exception:
            pass

    def showEvent(self, e):
        super().showEvent(e)
        try:
            if self.timer and not self.timer.isActive():
                self.timer.start()
        except Exception:
            pass

    def hideEvent(self, e):
        super().hideEvent(e)
        try:
            if self.timer and self.timer.isActive():
                self.timer.stop()
        except Exception:
            pass

    @staticmethod
    def _format_timedelta(td: Optional[datetime.timedelta]) -> str:
        if td is None:
            return "—"
        total = int(td.total_seconds())
        if total < 0:
            total = 0
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        if days > 0:
            return f"{days}d {hours:02}:{minutes:02}:{seconds:02}"
        return f"{hours:02}:{minutes:02}:{seconds:02}"

    def _to_local_naive_dt(self, ts_like) -> datetime.datetime:
        ts = pd.Timestamp(ts_like)
        # If tz-aware, convert to local then drop tz info
        if ts.tzinfo is not None or getattr(ts, "tz", None) is not None:
            try:
                ts = ts.tz_convert(_local_tzinfo())
            except Exception:
                try:
                    ts = ts.tz_localize("UTC").tz_convert(_local_tzinfo())
                except Exception:
                    pass
            try:
                ts = ts.tz_localize(None)
            except Exception:
                pass
        return ts.to_pydatetime()

    def _refresh_overview(self):
        if not (self._alive(self.ov_start_val) and self._alive(self.ov_now_val) and self._alive(self.ov_last_val)):
            return

        # Pull times
        if self.datetime_col and self.datetime_col in self.df.columns:
            times = self.df[self.datetime_col].dropna()
        else:
            times = pd.Series(dtype="datetime64[ns]")

        now_local = datetime.datetime.now()
        self.ov_now_val.setText(now_local.strftime("%Y-%m-%d %H:%M:%S"))

        if not times.empty:
            try:
                first_py = self._to_local_naive_dt(times.min())
                last_py = self._to_local_naive_dt(times.max())
            except Exception:
                # Fallback: treat as naive
                first_py = pd.Timestamp(times.min()).to_pydatetime()
                last_py = pd.Timestamp(times.max()).to_pydatetime()

            self.ov_start_val.setText(first_py.strftime("%Y-%m-%d %H:%M:%S"))
            self.ov_last_val.setText(last_py.strftime("%Y-%m-%d %H:%M:%S"))

            # Safe subtraction (both are local & naive)
            since_td = now_local - last_py

            # Average gap between samples (if we have ≥2 points)
            avg_td = None
            try:
                diffs = times.sort_values().diff().dropna()
                if not diffs.empty:
                    mean_td = diffs.mean()
                    # pandas Timedelta -> python timedelta
                    avg_td = (mean_td.to_pytimedelta()
                              if hasattr(mean_td, "to_pytimedelta")
                              else datetime.timedelta(seconds=float(mean_td.total_seconds())))
            except Exception:
                avg_td = None

            if self._alive(self.ov_since_val):
                self.ov_since_val.setText(f"{self._format_timedelta(since_td)} / {self._format_timedelta(avg_td)}")
        else:
            self.ov_start_val.setText("—")
            self.ov_last_val.setText("—")
            if self._alive(self.ov_since_val):
                self.ov_since_val.setText("— / —")

    def on_date_time_changed(self):
        if not (self._alive(self.start_date_edit) and self._alive(self.end_date_edit)):
            return
        self.update_time_labels()
        for b in (self.charts_board, self.overview_board):
            if b:
                try: b.refresh_all()
                except Exception: pass

    def on_time_slider_changed(self):
        if not (self._alive(self.start_time_slider) and self._alive(self.end_time_slider)):
            return
        self.update_time_labels()
        for b in (self.charts_board, self.overview_board):
            if b:
                try: b.refresh_all()
                except Exception: pass

    def update_time_labels(self):
        if not (self._alive(self.start_time_slider) and self._alive(self.end_time_slider)):
            return
        start_secs = self.start_time_slider.value()
        end_secs = self.end_time_slider.value()
        if end_secs < start_secs and self._alive(self.end_time_slider):
            self.end_time_slider.setValue(start_secs)
            end_secs = start_secs
        if self._alive(self.start_time_label):
            self.start_time_label.setText(QTime(0, 0).addSecs(start_secs).toString("HH:mm:ss"))
        if self._alive(self.end_time_label):
            self.end_time_label.setText(QTime(0, 0).addSecs(end_secs).toString("HH:mm:ss"))

    def get_datetime_from_widgets(self):
        if self._alive(self.start_date_edit):
            start_date = self.start_date_edit.date()
        else:
            start_date = QDate.currentDate()
        if self._alive(self.end_date_edit):
            end_date = self.end_date_edit.date()
        else:
            end_date = QDate.currentDate()

        start_secs = self.start_time_slider.value() if self._alive(self.start_time_slider) else 0
        end_secs = self.end_time_slider.value() if self._alive(self.end_time_slider) else 86399
        start_dt = QDateTime(start_date, QTime(0, 0).addSecs(start_secs))
        end_dt = QDateTime(end_date, QTime(0, 0).addSecs(end_secs))
        return start_dt.toPyDateTime(), end_dt.toPyDateTime()

    def filter_df_by_datetime(self) -> pd.DataFrame:
        if self.df.empty or not self.datetime_col or self.datetime_col not in self.df.columns:
            return self.df
        start_dt, end_dt = self.get_datetime_from_widgets()
        col = self.df[self.datetime_col]
        mask = col.between(start_dt, end_dt, inclusive="both")
        return self.df.loc[mask].copy()

    def shade_calendar_dates(self):
        if not (self._alive(self.start_date_edit) and self._alive(self.end_date_edit)):
            return
        if not self.datetime_col or self.df.empty or self.datetime_col not in self.df.columns:
            return

        dt = self.df[self.datetime_col].dropna()
        if dt.empty:
            return

        data_dates = set(dt.dt.date)
        fmt_no_data = QTextCharFormat(); fmt_no_data.setBackground(QBrush(QColor(200, 200, 200)))
        fmt_with_data = QTextCharFormat(); fmt_with_data.setBackground(QBrush(QColor(144, 238, 144)))

        min_date = min(data_dates); max_date = max(data_dates)
        current_date = min_date
        while current_date <= max_date:
            qd = QDate(current_date.year, current_date.month, current_date.day)
            fmt = fmt_with_data if current_date in data_dates else fmt_no_data
            try:
                self.start_date_edit.calendarWidget().setDateTextFormat(qd, fmt)
                self.end_date_edit.calendarWidget().setDateTextFormat(qd, fmt)
            except Exception:
                break
            current_date += datetime.timedelta(days=1)

    # Persistence (per-tab boards)
    def export_charts_settings(self) -> dict:
        payload = {"overview": {}, "charts": {}, "extra": []}
        try:
            if self.overview_board: payload["overview"] = self.overview_board.export_state()
        except Exception: pass
        try:
            if self.charts_board: payload["charts"] = self.charts_board.export_state()
        except Exception: pass
        for name, board in getattr(self, "extra_tabs", []):
            try: payload["extra"].append({"name": name, "state": board.export_state()})
            except Exception: pass
        return payload

    def import_charts_settings(self, data: dict):
        if not data: return
        if isinstance(data, dict) and ("rows" in data or "row_sizes" in data) and "charts" not in data:
            try:
                if self.charts_board: self.charts_board.import_state(data)
            except Exception: pass
            return

        ov = data.get("overview"); ch = data.get("charts"); extra = data.get("extra") or []
        try:
            if ov is not None and self.overview_board: self.overview_board.import_state(ov)
        except Exception: pass
        try:
            if ch is not None and self.charts_board: self.charts_board.import_state(ch)
        except Exception: pass

        for item in extra:
            try:
                tab_name = (item.get("name") or "Custom").strip() or "Custom"
                state = item.get("state") or {}
                tab = QWidget(self); v = QVBoxLayout(tab)
                filters_box = self._build_filters_widget(tab); v.addWidget(filters_box)
                scroll, board, _overlay = self._build_board_with_overlay(); v.addWidget(scroll)
                if board:
                    try: board.import_state(state)
                    except Exception: pass
                    self.extra_tabs.append((tab_name, board))
                plus_idx = self.inner_tabs.count() - 1
                self.inner_tabs.insertTab(plus_idx, tab, tab_name)
            except Exception:
                continue


# ---------------- SummaryTab (all projects in one dashboard) ----------------
class SummaryTab(QWidget):
    """
    Slim Summary:
      • Global date/time filters
      • Scrollable grid of per-project dashboard cards (First/Last/Count/Last Lat-Lon)

    Removed:
      – header 'Start/Current/Last' row
      – editable projects table
      – unified ChartBoard

    Notes:
      – Reads only likely datetime + lat/lon columns per table.
      – Rebuilds cards when filters change (debounced).
      – Entire page is in a QScrollArea (fully scrollable).
    """

    def __init__(self, db_path: str):
        super().__init__()
        self.db_path = db_path

        # filter widgets
        self.start_date_edit: QDateEdit | None = None
        self.end_date_edit: QDateEdit | None = None
        self.start_time_slider: QSlider | None = None
        self.end_time_slider: QSlider | None = None
        self.start_time_label: QLabel | None = None
        self.end_time_label: QLabel | None = None

        # cards container
        self.cards_box: QGroupBox | None = None
        self.cards_layout: QGridLayout | None = None

        # debounce for rebuilds
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(150)
        self._debounce.timeout.connect(self._rebuild_cards)

        # build UI
        self._init_ui()

    # ---------- UI ----------
    def _init_ui(self):
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # Filters only
        filters_box = self._build_filters_widget(content)
        root.addWidget(filters_box)

        # Cards grid
        self.cards_box = QGroupBox("Project dashboards", content)
        self.cards_layout = QGridLayout(self.cards_box)
        self.cards_layout.setHorizontalSpacing(16)
        self.cards_layout.setVerticalSpacing(12)
        root.addWidget(self.cards_box, 1)
        root.addStretch(1)

        scroll.setWidget(content)

        outer = QVBoxLayout(self)
        outer.addWidget(scroll)

        # initial build
        self._rebuild_cards()
        self._shade_calendar_dates()

    def _build_filters_widget(self, parent: QWidget):
        box = QGroupBox("Filters", parent)
        h = QHBoxLayout(box)

        self.start_date_edit = QDateEdit(parent); self.start_date_edit.setCalendarPopup(True)
        self.end_date_edit   = QDateEdit(parent); self.end_date_edit.setCalendarPopup(True)

        today = QDate.currentDate()
        self.start_date_edit.setDate(today)
        self.end_date_edit.setDate(today)

        self.start_time_slider = QSlider(Qt.Orientation.Horizontal, parent)
        self.start_time_slider.setRange(0, 86399); self.start_time_slider.setValue(0)
        self.end_time_slider = QSlider(Qt.Orientation.Horizontal, parent)
        self.end_time_slider.setRange(0, 86399); self.end_time_slider.setValue(86399)
        self.start_time_label = QLabel("00:00:00", parent)
        self.end_time_label = QLabel("23:59:59", parent)

        h.addWidget(QLabel("Start Date:", parent)); h.addWidget(self.start_date_edit)
        h.addWidget(QLabel("Start Time:", parent)); h.addWidget(self.start_time_slider); h.addWidget(self.start_time_label)
        h.addSpacing(12)
        h.addWidget(QLabel("End Date:", parent)); h.addWidget(self.end_date_edit)
        h.addWidget(QLabel("End Time:", parent)); h.addWidget(self.end_time_slider); h.addWidget(self.end_time_label)

        self.start_date_edit.dateChanged.connect(self._filters_changed)
        self.end_date_edit.dateChanged.connect(self._filters_changed)
        self.start_time_slider.valueChanged.connect(self._filters_changed)
        self.end_time_slider.valueChanged.connect(self._filters_changed)
        return box

    def _filters_changed(self, *_):
        self._update_time_labels()
        self._debounce.start()

    def _update_time_labels(self):
        if not self.start_time_slider or not self.end_time_slider: return
        start_secs = self.start_time_slider.value()
        end_secs = self.end_time_slider.value()
        if end_secs < start_secs:
            self.end_time_slider.setValue(start_secs)
            end_secs = start_secs
        if self.start_time_label:
            self.start_time_label.setText(QTime(0, 0).addSecs(start_secs).toString("HH:mm:ss"))
        if self.end_time_label:
            self.end_time_label.setText(QTime(0, 0).addSecs(end_secs).toString("HH:mm:ss"))

    def _get_widget_dt_range(self):
        sd = self.start_date_edit.date() if self.start_date_edit else QDate.currentDate()
        ed = self.end_date_edit.date() if self.end_date_edit else QDate.currentDate()
        ss = self.start_time_slider.value() if self.start_time_slider else 0
        es = self.end_time_slider.value() if self.end_time_slider else 86399
        start_dt = QDateTime(sd, QTime(0, 0).addSecs(ss)).toPyDateTime()
        end_dt   = QDateTime(ed, QTime(0, 0).addSecs(es)).toPyDateTime()
        return start_dt, end_dt

    # ---------- Cards ----------
    def _rebuild_cards(self):
        if not self.cards_layout: return
        while self.cards_layout.count():
            it = self.cards_layout.takeAt(0)
            w = it.widget()
            if w: w.deleteLater()

        start_dt, end_dt = self._get_widget_dt_range()
        row = col = 0
        max_cols = 2

        for t in _list_user_tables(self.db_path):
            card = self._make_project_card(t, start_dt, end_dt)
            self.cards_layout.addWidget(card, row, col)
            col += 1
            if col >= max_cols:
                col = 0; row += 1

    def _make_project_card(self, table_name: str, start_dt, end_dt) -> QGroupBox:
        df = self._read_minimal_df(table_name)
        start_s = end_s = "—"; count = 0; lat = lon = None

        if not df.empty:
            name, parsed = self._choose_best_datetime_column_for_df(df)
            if name is not None and parsed is not None:
                dff = df.copy()
                dff["__dt_iso_tmp"] = parsed
                dff = dff.dropna(subset=["__dt_iso_tmp"])
                if not dff.empty:
                    mask = dff["__dt_iso_tmp"].between(start_dt, end_dt, inclusive="both")
                    dff = dff.loc[mask]
                    if not dff.empty:
                        count = int(dff.shape[0])
                        start_s = dff["__dt_iso_tmp"].min().strftime("%Y-%m-%d %H:%M:%S")
                        end_s   = dff["__dt_iso_tmp"].max().strftime("%Y-%m-%d %H:%M:%S")
                        lat, lon = _last_known_lat_lon(dff, "__dt_iso_tmp")

        box = QGroupBox(table_name, self)
        g = QGridLayout(box)
        def add_row(r, label, value):
            g.addWidget(QLabel(label), r, 0)
            v = QLabel(value); v.setStyleSheet("font-weight:600;")
            g.addWidget(v, r, 1)

        add_row(0, "First:",  start_s)
        add_row(1, "Last:",   end_s)
        add_row(2, "Count:",  f"{count}")
        add_row(3, "Lat/Lon:", "" if lat is None or lon is None else f"{lat:.5f}, {lon:.5f}")
        return box

    def _read_minimal_df(self, table_name: str) -> pd.DataFrame:
        cand_dt  = ["timestamp", "received_time", "datetime", "time", "date"]
        cand_lat = ["lat", "latitude", "lat_dd", "lat_deg"]
        cand_lon = ["lon", "longitude", "long", "lon_dd", "lon_deg"]

        try:
            conn = _connect_sqlite_robust(self.db_path)
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()]
            lower = {c.lower(): c for c in cols}

            sel = []
            for c in cand_dt:
                if c in lower: sel.append(lower[c]); break
            for c in cand_lat:
                if c in lower: sel.append(lower[c]); break
            for c in cand_lon:
                if c in lower: sel.append(lower[c]); break
            if not sel:
                sel = cols[:1]

            sql = f'SELECT {", ".join(f"""\"{c}\"""" for c in sel)} FROM "{table_name}"'
            df = pd.read_sql_query(sql, conn)
            conn.close()
            return df
        except Exception:
            return pd.DataFrame()

    # ---- date parsing helpers (reused) ----
    def _parse_dt_flex_best(self, s: pd.Series) -> pd.Series:
        raw = s.astype("string", copy=False)
        cands = []
        try:
            cands.append(pd.to_datetime(raw, errors="coerce", utc=True))
        except Exception:
            pass
        try:
            cands.append(pd.to_datetime(raw, errors="coerce", dayfirst=True, utc=True))
        except Exception:
            pass
        try:
            cands.append(pd.to_datetime(raw, errors="coerce", dayfirst=True, utc=True, format="mixed"))
        except Exception:
            cands.append(pd.to_datetime(raw, errors="coerce", dayfirst=True, utc=True))
        for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M"):
            try:
                cands.append(pd.to_datetime(raw, format=fmt, errors="coerce", utc=True))
            except Exception:
                pass

        def score(x: pd.Series) -> int:
            return int(x.notna().sum())

        best = max(cands, key=score)

        try:
            best = best.dt.tz_convert(_local_tzinfo()).dt.tz_localize(None)
        except Exception:
            try:
                best = best.dt.tz_localize(None)
            except Exception:
                pass

        return pd.to_datetime(best, errors="coerce")

    def _choose_best_datetime_column_for_df(self, df: pd.DataFrame) -> tuple[Optional[str], Optional[pd.Series]]:
        if df.empty: return None, None
        preferred = ["timestamp", "received_time", "datetime", "time", "date"]
        cols = list(df.columns)
        trial = []
        if cols: trial.append(cols[0])
        lower_map = {c.lower(): c for c in cols}
        for name in preferred:
            if name in lower_map and lower_map[name] not in trial:
                trial.append(lower_map[name])
        trial += [c for c in cols if pd.api.types.is_datetime64_any_dtype(df[c]) and c not in trial]
        trial += [c for c in cols if c not in trial]

        best_col = None
        best_series = None
        best_ok = -1
        for c in trial:
            s = df[c]
            parsed = s if pd.api.types.is_datetime64_any_dtype(s) else self._parse_dt_flex_best(s)
            ok = int(parsed.notna().sum())
            if ok > best_ok:
                best_ok = ok; best_col = c; best_series = parsed
        if best_series is None or best_ok <= 0:
            return None, None
        return best_col, best_series

    def _shade_calendar_dates(self):
        # Optional: mark today just so calendars aren't blank
        try:
            fmt_with_data = QTextCharFormat(); fmt_with_data.setBackground(QBrush(QColor(144, 238, 144)))
            today = QDate.currentDate()
            if self.start_date_edit: self.start_date_edit.calendarWidget().setDateTextFormat(today, fmt_with_data)
            if self.end_date_edit:   self.end_date_edit.calendarWidget().setDateTextFormat(today, fmt_with_data)
        except Exception:
            pass


# ---------------- ProjectsView (vertical list + pages, horizontal labels) ----------------

class ProjectsView(QWidget):
    def __init__(self, db_path: str):
        super().__init__()
        self.db_path = os.path.abspath(db_path)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setHandleWidth(10)
        root.addWidget(self.splitter)

        # Left panel
        left_panel = QWidget(self)
        left_v = QVBoxLayout(left_panel)
        left_v.setContentsMargins(8, 8, 8, 8)
        left_v.setSpacing(6)

        header = QLabel("Projects", left_panel)
        header.setStyleSheet("font-weight: 600;")
        left_v.addWidget(header)

        self.list = QListWidget(left_panel)
        self.list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.list.setUniformItemSizes(False)
        self.list.setWordWrap(True)
        self.list.setAlternatingRowColors(True)
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.setMinimumWidth(1)
        left_v.addWidget(self.list, 1)

        left_panel.setMinimumWidth(80)
        left_panel.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Expanding)

        # Right side (pages)
        self.pages = QStackedWidget(self)
        self.pages.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.pages.setMinimumSize(1, 1)

        self.splitter.addWidget(left_panel)
        self.splitter.addWidget(self.pages)
        self.splitter.setChildrenCollapsible(True)
        self.splitter.setCollapsible(0, True)
        self.splitter.setCollapsible(1, True)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([280, 1000])

        # Long names & init
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.list.setTextElideMode(Qt.TextElideMode.ElideNone)

        self.summary_page = None

        # ⬇️ Build pages now (after widgets exist)
        # in ProjectsView.__init__
        try:
            self._rebuild_with_retry()
        except Exception as e:
            tb = traceback.format_exc(limit=8)
            # Keep the original error; don’t force errno 22
            raise RuntimeError(
                f"ProjectsView initial build failed ({type(e).__name__}): {e}\n{tb}"
            ) from e

        self.list.currentRowChanged.connect(self.pages.setCurrentIndex)
        if self.list.count() > 0:
            self.list.setCurrentRow(0)

        self.setLayout(root)

        self._flags_timer = QTimer(self)
        self._flags_timer.setInterval(5000)
        self._flags_timer.timeout.connect(self._refresh_project_flags)
        self._flags_timer.start()
        self._refresh_project_flags()
        self._count_flagged_fn = self._resolve_count_flagged()

    # keep a compatibility wrapper
    def _rebuild(self):
        self._rebuild_with_retry()

    def _rebuild_with_retry(self, attempts: int = 6, delay_s: float = 0.2) -> None:
        last_err = None
        for i in range(int(attempts)):
            try:
                self._rebuild_once()
                return
            except (sqlite3.OperationalError, OSError) as e:
                msg = str(e).lower()
                transient = (getattr(e, "errno", None) == 22) or \
                            ("locked" in msg) or ("busy" in msg) or ("unable to open" in msg)
                if not transient:
                    raise  # not transient → bubble up *now*
                last_err = e
                time.sleep(delay_s * (i + 1))
        raise last_err

    def refresh_data_light(self):
        """
        Reload data for all existing tabs/pages without rebuilding the UI.
        Keeps tab switching instant and avoids widget churn.
        """
        # Per-table pages
        for _name, tab in self.iter_tables():
            try:
                tab.reload_df_light()
            except Exception:
                pass

        # Summary page (if it exposes a rebuild method)
        try:
            if getattr(self, "summary_page", None):
                if hasattr(self.summary_page, "_rebuild_cards"):
                    self.summary_page._rebuild_cards()
                elif hasattr(self.summary_page, "reload"):  # older API
                    self.summary_page.reload()
        except Exception:
            pass

    def _project_api(self):
        """
        Bridge used by SummaryPage tiles to pull info from Alerts/Charts.
        """
        from utils.alerts.store import count_flagged, read_last_status
        # We keep read_last_status flexible: it may return a string or (status, observed)

        class _API:
            def __init__(self, outer):
                self.outer = outer

            # helpers
            def _tab(self, table: str):
                for name, page in self.outer.iter_tables():
                    if name == table:
                        return page
                return None

            # ---- Alerts ----
            def list_alerts(self, table: str):
                tab = self._tab(table)
                if not tab: return []
                at = getattr(tab, "alerts_tab", None)
                if not at or not getattr(at, "specs", None):
                    return []
                out = []
                for s in at.specs:
                    out.append({
                        "id": (s.id or s.name or s.kind),
                        "name": (s.name or s.kind or s.id or "Alert"),
                        "kind": s.kind,
                    })
                return out

            # ---- Alerts dropdown data (all alerts, with live status) ----
            def get_alerts_table(self, table: str):
                """
                Return a list of dicts for ALL alerts in a table:
                {id, name, kind, status, summary, active}
                """
                tab = self._tab(table)
                if not tab:
                    return []
                at = getattr(tab, "alerts_tab", None)
                if not at or not getattr(at, "specs", None):
                    return []

                out = []
                for s in at.specs:
                    try:
                        res = REGISTRY[s.kind].evaluate(s, tab)
                        st = res.get("status")
                        status_str = getattr(st, "value", str(st) if st is not None else "unknown")
                        out.append({
                            "id": s.id or s.name or s.kind,
                            "name": s.name or s.kind or s.id or "Alert",
                            "kind": s.kind,
                            "status": status_str,
                            "summary": res.get("summary", "") or "",
                            "active": status_str not in ("GREEN", "OFF"),
                        })
                    except Exception:
                        out.append({
                            "id": s.id or s.name or s.kind,
                            "name": s.name or s.kind or s.id or "Alert",
                            "kind": s.kind,
                            "status": "unknown",
                            "summary": "",
                            "active": False,
                        })
                return out

            # ---- Open the same viewers as AlertsTab for a given alert id ----
            def view_alert_by_id(self, table: str, alert_id: str):
                tab = self._tab(table)
                if not tab:
                    return
                at = getattr(tab, "alerts_tab", None)
                if not at:
                    return
                spec = next((s for s in at.specs if s.id == alert_id or s.name == alert_id), None)
                if not spec:
                    return
                # Use the same dialogs AlertsTab uses
                if spec.kind == "Threshold":
                    at._ThresholdViewDialog(spec, tab, at).exec()
                elif spec.kind == "Distance":
                    at._DistanceViewDialog(spec, tab, at).exec()
                elif spec.kind == "Stale":
                    at._StaleViewDialog(spec, tab, at).exec()
                elif spec.kind == "MissingData":
                    at._MissingDataViewDialog(spec, tab, at).exec()
                else:
                    QMessageBox.information(at, "View alert", f"No viewer available for type: {spec.kind}")

            def configure_alert_by_id(self, table: str, alert_id: str):
                tab = self._tab(table)
                if not tab:
                    return
                at = getattr(tab, "alerts_tab", None)
                if not at:
                    return
                spec = next((s for s in at.specs if s.id == alert_id or s.name == alert_id), None)
                if not spec:
                    return
                at.configure_spec(spec)



            # inside class _API in _project_api()
            def alert_status(self, table: str, key: str):
                tab = self._tab(table)
                if not tab:
                    return {"status": "unknown", "observed": None, "summary": ""}

                at = getattr(tab, "alerts_tab", None)
                if not at or not getattr(at, "specs", None):
                    return {"status": "unknown", "observed": None, "summary": ""}

                # find alert spec by id (preferred) then by name/kind as a soft fallback
                spec = next((s for s in at.specs if getattr(s, "id", None) == key), None)
                if spec is None:
                    spec = next((s for s in at.specs if (s.name or s.kind or "") == key), None)
                if spec is None:
                    return {"status": "unknown", "observed": None, "summary": ""}

                try:
                    # Evaluate NOW against the current data in memory
                    res = REGISTRY[spec.kind].evaluate(spec, tab)
                    st = res.get("status")
                    status_str = getattr(st, "value", str(st) if st is not None else "unknown")
                    return {
                        "status": status_str,  # e.g. "GREEN" / "AMBER" / "RED" / "OFF"
                        "observed": res.get("observed", None),
                        "summary": res.get("summary", "") or "",
                        # optional hint; SummaryTile doesn't rely on it but it's harmless to include
                        "active": status_str not in ("GREEN", "OFF"),
                    }
                except Exception:
                    return {"status": "unknown", "observed": None, "summary": ""}

            def count_flagged(self, table: str) -> int:
                try:
                    return int(count_flagged(self.outer.db_path, table) or 0)
                except Exception:
                    return 0



            # ---- Charts ----
            def list_charts(self, table: str):
                tab = self._tab(table)
                if not tab: return []
                board = getattr(tab, "charts_board", None)
                if not board or not hasattr(board, "export_state"):
                    return []
                try:
                    st = board.export_state() or {}
                    items = []
                    if "charts" in st and isinstance(st["charts"], list):
                        for i, c in enumerate(st["charts"]):
                            title = (c.get("title") or c.get("y") or f"Chart {i + 1}")
                            items.append({"id": f"flat/{i}", "title": str(title)})
                    elif "rows" in st and isinstance(st["rows"], list):
                        for r_i, row in enumerate(st["rows"]):
                            for c_i, c in enumerate(row.get("charts", []) or []):
                                title = (c.get("title") or c.get("y") or f"Chart {r_i + 1}.{c_i + 1}")
                                items.append({"id": f"r{r_i}/c{c_i}", "title": str(title)})
                    return items
                except Exception:
                    return []

        return _API(self)

    def reload(self, db_path: str | None = None):
        if db_path:
            self.db_path = db_path
        self._rebuild()

    def iter_tables(self):
        for i in range(1, self.pages.count()):
            w = self.pages.widget(i)
            name = self.list.item(i).text() if i < self.list.count() else f"Table {i}"
            yield name, w

    def current_table_name(self) -> Optional[str]:
        idx = self.list.currentRow()
        if idx <= 0:
            return None
        item = self.list.item(idx)
        return item.text() if item else None

    def _resolve_count_flagged(self):
        """Return utils.alerts.store.count_flagged or a no-op fallback."""
        try:
            from utils.alerts.store import count_flagged
            return count_flagged
        except Exception:
            return lambda *_a, **_k: 0

    def _count_flagged(self, table: str) -> int:
        try:
            return int(self._count_flagged_fn(self.db_path, table) or 0)
        except Exception:
            return 0

    def _flag_icon(self, red: bool) -> QIcon:
        # draw a tiny flag (pole + triangle)
        pm = QPixmap(18, 14)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # pole
        p.setPen(QPen(QColor("#495057"), 2))
        p.drawLine(3, 3, 3, 12)

        # flag triangle
        color = QColor("#f03e3e" if red else "#adb5bd")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        pts = [QPoint(4, 3), QPoint(13, 7), QPoint(4, 10)]
        p.drawPolygon(*pts)

        p.end()
        return QIcon(pm)

    def _refresh_project_flags(self):
        try:
            tables = _list_user_tables(self.db_path)
        except Exception:
            tables = []

        # Per-table counts
        counts = {}
        total = 0
        for t in tables:
            try:
                c = self._count_flagged(t)
            except Exception:
                c = 0
            counts[t] = c
            total += c  # instead of calling _count_flagged again

        # Row 0 is "Summary"
        if self.list.count() > 0:
            sum_item = self.list.item(0)
            sum_item.setIcon(self._flag_icon(total > 0))
            tip_lines = [f"{t}: {n}" for t, n in counts.items()]
            sum_item.setToolTip(f"Total flagged: {total}\n" + ("\n".join(tip_lines) if tip_lines else ""))

        # Table rows: 1..N
        for i in range(1, self.list.count()):
            item = self.list.item(i)
            if not item:
                continue
            name = item.text()
            c = counts.get(name, 0)
            item.setIcon(self._flag_icon(c > 0))
            item.setToolTip(f"{name} — {c} flagged alert{'s' if c != 1 else ''}")

        # Keep the Summary page's big flagged badge in sync with the sidebar counts
        try:
            if getattr(self, "summary_page", None):
                self.summary_page._refresh_badge()
        except Exception:
            pass


    def _rebuild_once(self):
        self.list.clear()
        while self.pages.count():
            w = self.pages.widget(0)
            self.pages.removeWidget(w)
            w.deleteLater()

        # Summary first
        sum_item = QListWidgetItem("Summary")
        sum_item.setToolTip("Summary")
        self.list.addItem(sum_item)
        try:
            self.summary_page = _make_summary_page(self, self.db_path, self._project_api())
        except Exception as e:
            print(f"[ProjectsView] Summary construction failed, using fallback SummaryTab: {e!r}")
            self.summary_page = SummaryTab(self.db_path)
        self.pages.addWidget(self.summary_page)

        # Then one TableTab per table
        tables = _list_user_tables(self.db_path)
        for t in tables:
            item = QListWidgetItem(t)
            item.setToolTip(t)
            self.list.addItem(item)
            self.pages.addWidget(TableTab(db_path=self.db_path, table_name=t))
        self._refresh_project_flags()

