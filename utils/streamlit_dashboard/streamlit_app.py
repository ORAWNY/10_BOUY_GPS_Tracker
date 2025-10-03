# web_dash/streamlit_app.py
# ------------------------------------------------------------
# Streamlit dashboard that mirrors your per-table "Create tabs"
# view: summary, map, base plots, and your saved Custom Charts.
#
# Usage (CLI):
#   streamlit run web_dash/streamlit_app.py -- --db /path/to/Logger_Data.db --project /path/to/xyz.bouyproj.json
#
# Or let the PyQt app launch it (see integration below).
# ------------------------------------------------------------
import os
import sys
import json
import math
import re
import argparse
from dataclasses import dataclass
from typing import Dict, Any, List, Optional, Tuple

import pandas as pd
import numpy as np
import sqlite3

import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
from dataclasses import dataclass, field

# Optional map stack
try:
    import pydeck as pdk
    _HAS_PYDECK = True
except Exception:
    _HAS_PYDECK = False

# --------------------- Models (match your desktop) ---------------------
@dataclass
class AxisSpec:
    label: str = ""
    scale: str = "linear"
    tick_rotation: int = 0
    tick_interval: Optional[float] = None
    dt_unit: str = "auto"
    dt_step: int = 1
    vmin: Optional[float] = None
    vmax: Optional[float] = None
    grid: bool = True
    labelpad: int = 6

@dataclass
class SeriesSpec:
    y_col: str = ""
    label: str = ""
    style: str = "Scatter"    # Scatter|Line|Bar
    color: str = ""           # hex or empty for auto
    marker: str = "o"
    size: float = 6.0         # scatter marker size (px)
    linewidth: float = 1.5
    alpha: float = 1.0
    linestyle: str = "-"
    y_axis: str = "left"
    label_col: str = ""

@dataclass
class ChartSpec:
    id: str = ""
    chart_kind: str = "XY"     # XY|Pie|Gauge
    title: str = "Custom Chart"
    title_align: str = "center"
    legend: bool = True

    axes_facecolor: str = ""
    frame_visible: bool = True
    frame_color: str = ""
    frame_linewidth: float = 1.0

    x_col: str = ""
    x_axis: AxisSpec = field(default_factory=AxisSpec)
    y_left: AxisSpec = field(default_factory=AxisSpec)
    y_right: AxisSpec = field(default_factory=lambda: AxisSpec(grid=False))
    series: List[SeriesSpec] = field(default_factory=list)

    # Pie
    pie_donut: float = 0.4
    pie_autopct: bool = True

    # Gauge
    gauge_value_col: str = ""
    gauge_min: float = 0.0
    gauge_max: float = 100.0
    gauge_color: str = "#4caf50"
    gauge_track_color: str = "#e0e0e0"
    gauge_bg: str = ""
    gauge_show_text: bool = True
    gauge_units: str = "%"

    # Calculator
    calc_mode: str = "none"  # none|availability|group_count|group_sum|group_mean|last|mean|sum|min|max
    calc_column: str = ""
    calc_group_col: str = ""
    ignore_nan: bool = True
    ignore_9999: bool = True
    ignore_neg9999: bool = True
    ignore_zero: bool = True
    availability_show_daily: bool = True
    availability_all_time: bool = True


# --------------------- Utilities ---------------------
def _load_project(path: Optional[str]) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _norm_db_path(p: Optional[str]) -> Optional[str]:
    if not p: return None
    p = os.path.normpath(p)
    return p if os.path.exists(p) else None

def list_tables(db_path: str) -> List[str]:
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT name FROM sqlite_schema
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
        """)
        return [r[0] for r in cur.fetchall()]

def read_table(db_path: str, table: str) -> pd.DataFrame:
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(f"SELECT * FROM [{table}]", conn)
    # parse datetime-ish columns
    for c in df.columns:
        if c.lower() in ("received_time", "timestamp", "time", "date", "datetime"):
            try:
                df[c] = pd.to_datetime(df[c], errors="coerce")
            except Exception:
                pass
    return df

def guess_datetime_col(df: pd.DataFrame) -> Optional[str]:
    # Priority
    preferred = ["received_time", "Timestamp", "datetime", "Date", "Time"]
    for p in preferred:
        if p in df.columns:
            try:
                pd.to_datetime(df[p], errors="coerce")
                return p
            except Exception:
                pass
    # any datetime dtype?
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]): return c
    return None

def _safe_numeric(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")

def _availability_mask(vals: pd.Series, spec: ChartSpec) -> pd.Series:
    mask = vals.notna() if spec.ignore_nan else pd.Series(True, index=vals.index)
    if spec.ignore_9999: mask &= vals != 9999
    if spec.ignore_neg9999: mask &= vals != -9999
    if spec.ignore_zero: mask &= vals != 0
    return mask

def availability_pct(vals: pd.Series, spec: ChartSpec) -> Tuple[float, int, int]:
    v = _safe_numeric(vals)
    mask = _availability_mask(v, spec)
    total = int(mask.size)
    valid = int(mask.sum())
    pct = float(valid/total*100.0) if total > 0 else 0.0
    return pct, valid, total

def daily_availability(df: pd.DataFrame, col: str, dtcol: Optional[str], spec: ChartSpec) -> Optional[pd.Series]:
    if not dtcol or dtcol not in df.columns: return None
    v = _safe_numeric(df[col])
    mask = _availability_mask(v, spec)
    g = pd.DataFrame({"ok": mask, "dt": pd.to_datetime(df[dtcol], errors="coerce")}).dropna(subset=["dt"])
    if g.empty: return None
    day = g["dt"].dt.floor("D")
    agg = g.groupby(day)["ok"].agg(["sum","count"])
    return (agg["sum"]/agg["count"]*100.0).rename("availability_%")

# --------------------- Charts from ChartSpec (Plotly) ---------------------
def render_chart(spec: ChartSpec, df: pd.DataFrame, df_full: Optional[pd.DataFrame]) -> Optional[go.Figure]:
    kind = (spec.chart_kind or "XY").lower()
    if df is None or df.empty:
        fig = go.Figure()
        fig.add_annotation(text="No data in selected range", showarrow=False, x=0.5, y=0.5)
        return fig

    if kind == "xy":
        if not spec.series or not spec.x_col or spec.x_col not in df.columns:
            fig = go.Figure()
            fig.add_annotation(text="Configure chart… (missing X or Series)", showarrow=False, x=0.5, y=0.5)
            return fig

        fig = go.Figure()
        for s in spec.series:
            if not s.y_col or s.y_col not in df.columns: continue
            yaxis = "y2" if s.y_axis == "right" else "y"
            name = s.label or s.y_col
            color = s.color if s.color else None
            opacity = max(0.05, min(1.0, s.alpha))
            if s.style == "Bar":
                fig.add_bar(x=df[spec.x_col], y=df[s.y_col], name=name, marker_color=color, opacity=opacity, yaxis=yaxis)
            elif s.style == "Line":
                fig.add_scatter(x=df[spec.x_col], y=df[s.y_col], mode="lines+markers",
                                name=name,
                                line=dict(width=max(0.1, s.linewidth), color=color),
                                opacity=opacity,
                                yaxis=yaxis)
            else:  # Scatter
                fig.add_scatter(x=df[spec.x_col], y=df[s.y_col], mode="markers",
                                name=name,
                                marker=dict(size=max(2, int(s.size)), color=color),
                                opacity=opacity,
                                yaxis=yaxis)

        fig.update_layout(
            xaxis_title=spec.x_axis.label or spec.x_col,
            yaxis_title=spec.y_left.label or "",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0) if spec.legend else dict(visible=False),
            title=dict(text=spec.title or "", x={"left":0.0,"center":0.5,"right":1.0}.get(spec.title_align,0.5))
        )
        if any((s.y_axis == "right") for s in (spec.series or [])):
            fig.update_layout(yaxis2=dict(title=spec.y_right.label or "", overlaying="y", side="right"))

        # scales
        if (spec.x_axis.scale or "linear") == "log":
            fig.update_xaxes(type="log")
        if (spec.y_left.scale or "linear") == "log":
            fig.update_yaxes(type="log")
        if any((s.y_axis == "right") for s in (spec.series or [])) and (spec.y_right.scale or "linear") == "log":
            fig.update_yaxes(type="log", secondary_y=True)

        # limits
        if spec.x_axis.vmin is not None or spec.x_axis.vmax is not None:
            fig.update_xaxes(range=[spec.x_axis.vmin, spec.x_axis.vmax])
        if spec.y_left.vmin is not None or spec.y_left.vmax is not None:
            fig.update_yaxes(range=[spec.y_left.vmin, spec.y_left.vmax])
        if any((s.y_axis == "right") for s in (spec.series or [])):
            if (spec.y_right.scale or "linear") == "log":
                fig.update_layout(yaxis2=dict(type="log"))
            if spec.y_right.vmin is not None or spec.y_right.vmax is not None:
                fig.update_layout(yaxis2=dict(range=[spec.y_right.vmin, spec.y_right.vmax]))

        return fig

    if kind == "pie":
        # availability or grouping or simple
        mode = spec.calc_mode or "none"
        if mode == "availability":
            col = spec.calc_column or (spec.series[0].y_col if spec.series else "")
            if not col or col not in df.columns:
                return _msg_fig("Select a value column for availability pie")
            pct, valid, total = availability_pct(df[col], spec)
            values = [valid, max(0, total-valid)]
            labels = [f"Available ({pct:.1f}%)", f"Missing ({100-pct:.1f}%)"]
            fig = go.Figure(go.Pie(
                values=values, labels=labels, hole=max(0.0, min(0.95, spec.pie_donut)),
                textinfo="percent+label" if spec.pie_autopct else "label"
            ))
            if spec.availability_all_time and df_full is not None and col in df_full.columns:
                apct, aval, atot = availability_pct(df_full[col], spec)
                fig.add_annotation(text=f"All-time: {apct:.1f}% ({aval}/{atot})",
                                   x=0.5, y=-0.15, showarrow=False)
            fig.update_layout(title=spec.title or "")
            return fig

        if mode in ("group_count","group_sum","group_mean"):
            group_col = spec.calc_group_col or ((spec.series[0].label_col) if spec.series else "")
            if not group_col or group_col not in df.columns:
                return _msg_fig("Pick a 'Group by' column for pie")
            dfg = df[[group_col]].copy()
            if mode == "group_count":
                agg = dfg.groupby(group_col).size()
            else:
                val_col = spec.calc_column or (spec.series[0].y_col if spec.series else "")
                if not val_col or val_col not in df.columns:
                    return _msg_fig("Pick a numeric value column for grouped pie")
                vals = _safe_numeric(df[val_col])
                dfg[val_col] = vals
                dfg = dfg.dropna(subset=[val_col])
                agg = dfg.groupby(group_col)[val_col].sum() if mode == "group_sum" else dfg.groupby(group_col)[val_col].mean()
            if agg.empty: return _msg_fig("No data for grouping")
            fig = go.Figure(go.Pie(
                values=agg.values, labels=[str(x) for x in agg.index],
                hole=max(0.0, min(0.95, spec.pie_donut)),
                textinfo="percent+label" if spec.pie_autopct else "label"
            ))
            fig.update_layout(title=spec.title or "")
            return fig

        # simple: use first series values (labels optional)
        s = (spec.series or [SeriesSpec()])[0]
        if not s.y_col or s.y_col not in df.columns:
            return _msg_fig("Select value column (Series Y)")
        vals = pd.to_numeric(df[s.y_col], errors="coerce").dropna()
        if vals.empty: return _msg_fig("No numeric values for pie")
        labels = None
        if s.label_col and s.label_col in df.columns:
            labels = df.loc[vals.index, s.label_col].astype(str)
        fig = go.Figure(go.Pie(
            values=vals.values,
            labels=(labels.values.tolist() if labels is not None else None),
            hole=max(0.0, min(0.95, spec.pie_donut)),
            textinfo="percent+label" if spec.pie_autopct else "label"
        ))
        fig.update_layout(title=spec.title or "")
        return fig

    if kind == "gauge":
        val_col = spec.calc_column or spec.gauge_value_col
        if not val_col or val_col not in df.columns:
            return _msg_fig("Pick gauge value column")
        vals = _safe_numeric(df[val_col]).dropna()
        mode = spec.calc_mode or "last"
        if mode == "last": val = float(vals.iloc[-1]) if not vals.empty else np.nan
        elif mode == "mean": val = float(vals.mean()) if not vals.empty else np.nan
        elif mode == "sum":  val = float(vals.sum())  if not vals.empty else np.nan
        elif mode == "min":  val = float(vals.min())  if not vals.empty else np.nan
        elif mode == "max":  val = float(vals.max())  if not vals.empty else np.nan
        elif mode == "availability":
            pct, _, _ = availability_pct(df[val_col], spec); val = pct
            spec.gauge_min, spec.gauge_max, spec.gauge_units = 0.0, 100.0, "%"
        else:
            val = float(vals.iloc[-1]) if not vals.empty else np.nan
        if np.isnan(val): return _msg_fig("No numeric value for gauge")

        fig = go.Figure(go.Indicator(
            mode="gauge+number" if spec.gauge_show_text else "gauge",
            value=val,
            title={'text': spec.title or ""},
            number={'suffix': spec.gauge_units or ""},
            gauge={
                'axis': {'range': [spec.gauge_min, spec.gauge_max]},
                'bar': {'color': spec.gauge_color},
                'bgcolor': spec.gauge_bg or "white",
                'borderwidth': max(0, int(spec.frame_linewidth)),
                'bordercolor': spec.frame_color or "#aaa",
                'threshold': {
                    'line': {'color': spec.frame_color or "#666", 'width': 2},
                    'thickness': 0.75,
                    'value': val
                }
            }
        ))
        return fig

    return _msg_fig(f"Unknown chart type: {spec.chart_kind}")

def _msg_fig(text: str) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=text, showarrow=False, x=0.5, y=0.5)
    return fig

def _load_chart_settings_for_table(project_obj: Dict[str, Any], table_name: str) -> List[ChartSpec]:
    charts_root = (project_obj or {}).get("charts") or {}
    table_blob = charts_root.get(table_name) or {}
    chart_list = table_blob.get("charts") or []
    specs: List[ChartSpec] = []
    for d in chart_list:
        try:
            # tolerant loader
            sers = [SeriesSpec(**sd) for sd in (d.get("series") or [])]
            spec = ChartSpec(
                id=d.get("id",""),
                chart_kind=d.get("chart_kind","XY"),
                title=d.get("title","Custom Chart"),
                title_align=d.get("title_align","center"),
                legend=bool(d.get("legend", True)),
                axes_facecolor=d.get("axes_facecolor",""),
                frame_visible=bool(d.get("frame_visible", True)),
                frame_color=d.get("frame_color",""),
                frame_linewidth=float(d.get("frame_linewidth",1.0)),
                x_col=d.get("x_col",""),
                x_axis=AxisSpec(**(d.get("x_axis") or {})),
                y_left=AxisSpec(**(d.get("y_left") or {})),
                y_right=AxisSpec(**(d.get("y_right") or {})),
                series=sers,
                pie_donut=float(d.get("pie_donut",0.4)),
                pie_autopct=bool(d.get("pie_autopct", True)),
                gauge_value_col=d.get("gauge_value_col",""),
                gauge_min=float(d.get("gauge_min",0.0)),
                gauge_max=float(d.get("gauge_max",100.0)),
                gauge_color=d.get("gauge_color","#4caf50"),
                gauge_track_color=d.get("gauge_track_color","#e0e0e0"),
                gauge_bg=d.get("gauge_bg",""),
                gauge_show_text=bool(d.get("gauge_show_text", True)),
                gauge_units=d.get("gauge_units","%"),
                calc_mode=d.get("calc_mode","none"),
                calc_column=d.get("calc_column",""),
                calc_group_col=d.get("calc_group_col",""),
                ignore_nan=bool(d.get("ignore_nan", True)),
                ignore_9999=bool(d.get("ignore_9999", True)),
                ignore_neg9999=bool(d.get("ignore_neg9999", True)),
                ignore_zero=bool(d.get("ignore_zero", True)),
                availability_show_daily=bool(d.get("availability_show_daily", True)),
                availability_all_time=bool(d.get("availability_all_time", True)),
            )
            specs.append(spec)
        except Exception:
            continue
    return specs

# --------------------- Streamlit ui ---------------------
def _parse_args() -> argparse.Namespace:
    # Streamlit passes unknown args; parse only after "--"
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--")+1:]
    else:
        argv = []
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--db")
    ap.add_argument("--project")
    ap.add_argument("--title", default="Buoy DB Dashboard")
    return ap.parse_args(argv)

def main():
    args = _parse_args()
    # Precedence: CLI (desktop app) → env → query params (manual override when testing)
    # NOTE: Streamlit sometimes adds ?db=... when reloading; we only use that if CLI/env are missing.
    qp = st.query_params  # Streamlit >= 1.32

    db_path = args.db or os.environ.get("BUOY_DB") or (qp.get("db", [None])[0])
    project_path = args.project or os.environ.get("BUOY_PROJECT") or (qp.get("project", [None])[0])

    st.set_page_config(page_title=args.title or "DB Dashboard", layout="wide")
    st.title(args.title or "Buoy DB Dashboard")

    with st.sidebar:
        st.header("Data Source")
        st.caption("Provided by desktop app (CLI) when launched from the GUI.")
        st.text_input("SQLite DB path", db_path or "", key="db_path_sidebar_display", disabled=True)
        st.text_input("Project (.bouyproj.json)", project_path or "", key="proj_path_sidebar_display", disabled=True)
        st.markdown("---")
        show_map = st.checkbox("Show Map (Lat/Lon)", value=True, help="Requires Lat & Lon columns")
        st.caption("This dashboard is read-only; manage parsers/DB in the desktop app (or the Parsers tab here).")

    db_ok = _norm_db_path(db_path)
    if not db_ok:
        st.warning("Select a valid SQLite DB path (via sidebar or app launcher).")
        st.stop()

    proj_obj = _load_project(project_path) if project_path else {}

    tables = list_tables(db_ok)
    if not tables:
        st.error("No tables found in database.")
        st.stop()

    tab_objs = st.tabs(tables)

    for tname, panel in zip(tables, tab_objs):
        with panel:
            df = read_table(db_ok, tname)
            if df is None or df.empty:
                st.info("No data in this table.")
                continue

            dt_col = guess_datetime_col(df)

            # ---- UNIQUE KEYS PER TABLE ----
            tkey = re.sub(r'[^0-9A-Za-z_]+', '_', tname)  # safe suffix for keys

            left, right = st.columns([3, 2])
            with left:
                st.subheader("Filters")
                if dt_col:
                    min_dt = pd.to_datetime(df[dt_col]).min()
                    max_dt = pd.to_datetime(df[dt_col]).max()

                    # Give widgets unique keys per table
                    d_start, d_end = st.date_input(
                        "Date range",
                        value=(min_dt.date(), max_dt.date()),
                        key=f"date_range_{tkey}",
                    )
                    time_start = st.slider(
                        "Start time",
                        0, 24 * 60 - 1, 0,
                        step=1, format="%d min",
                        key=f"time_start_{tkey}",
                    )
                    time_end = st.slider(
                        "End time",
                        0, 24 * 60 - 1, 24 * 60 - 1,
                        step=1, format="%d min",
                        key=f"time_end_{tkey}",
                    )

                    # build datetimes
                    ts_start = pd.Timestamp.combine(
                        pd.to_datetime(d_start), pd.to_datetime("00:00:00").time()
                    ) + pd.Timedelta(minutes=time_start)
                    ts_end = pd.Timestamp.combine(
                        pd.to_datetime(d_end), pd.to_datetime("00:00:00").time()
                    ) + pd.Timedelta(minutes=time_end)

                    mask = (
                                   pd.to_datetime(df[dt_col], errors="coerce") >= ts_start
                           ) & (
                                   pd.to_datetime(df[dt_col], errors="coerce") <= ts_end
                           )
                    dff = df.loc[mask].copy()
                else:
                    st.caption("No datetime column found; showing all rows.")
                    dff = df.copy()

            # --- Summary (right) ---
            with right:
                st.subheader("Summary")
                if dt_col:
                    last_received = pd.to_datetime(df[dt_col], errors="coerce").max()
                    st.metric("Last Data Received", str(last_received))

                    if pd.notna(last_received):
                        # Make both sides tz-aware in UTC, then subtract
                        now_utc = pd.Timestamp.now(tz="UTC")
                        last_utc = pd.to_datetime(last_received, utc=True)  # coerces to UTC; if naive, assumes UTC
                        delta = now_utc - last_utc
                        st.metric("Time Since Last Data", str(delta).split(".")[0])

                    # average interval
                    times = pd.to_datetime(df[dt_col], errors="coerce").sort_values()
                    if times.notna().sum() > 1:
                        intervals = times.diff().dropna().dt.total_seconds()
                        st.metric("Avg Interval (sec)", f"{intervals.mean():.2f}")
                    else:
                        st.metric("Avg Interval (sec)", "N/A")
                st.metric("Rows in range", f"{len(dff)}")

            # Map (pydeck)
            if show_map and _HAS_PYDECK and "Lat" in dff.columns and "Lon" in dff.columns:
                try:
                    lat_med = pd.to_numeric(dff["Lat"], errors="coerce").median()
                    lon_med = pd.to_numeric(dff["Lon"], errors="coerce").median()
                    layer = pdk.Layer(
                        "ScatterplotLayer",
                        data=dff.rename(columns={"Lon":"lon","Lat":"lat"}),
                        get_position='[lon, lat]',
                        get_radius=50,
                        get_fill_color=[0, 128, 255, 160],
                        pickable=True
                    )
                    view_state = pdk.ViewState(latitude=float(lat_med or 0.0),
                                               longitude=float(lon_med or 0.0),
                                               zoom=8)
                    st.pydeck_chart(pdk.Deck(layers=[layer], initial_view_state=view_state, tooltip={"text": "{lon}, {lat}"}))
                except Exception as e:
                    st.info(f"Map unavailable: {e}")

            # Base plots: Volt and Battery
            with st.expander("Standard Plots", expanded=True):
                if dt_col and "Volt" in dff.columns:
                    fig_v = px.line(dff, x=dt_col, y="Volt", title=f"{tname} — Volt")
                    fig_v.update_layout(height=350, margin=dict(l=20,r=20,b=40,t=40))
                    st.plotly_chart(fig_v, use_container_width=True)
                bats = [c for c in ["Bat1","Bat2","Bat3"] if c in dff.columns]
                if dt_col and bats:
                    fig_b = go.Figure()
                    for b in bats:
                        fig_b.add_scatter(x=dff[dt_col], y=dff[b], mode="lines", name=b)
                    fig_b.update_layout(title=f"{tname} — Battery", height=350, margin=dict(l=20,r=20,b=40,t=40))
                    st.plotly_chart(fig_b, use_container_width=True)

            # Custom charts (from project file)
            specs = _load_chart_settings_for_table(proj_obj, tname)
            if specs:
                st.subheader("Custom Charts")
                for spec in specs:
                    fig = render_chart(spec, dff, df_full=df)
                    st.plotly_chart(fig, use_container_width=True)

            # Admin (Parsers) — optional simple runner if project has parsers
            parsers = (proj_obj.get("parsers") if proj_obj else None) or []
            if parsers:
                with st.expander("Parsers (run locally on this host)", expanded=False):
                    st.caption("Runs Outlook/COM locally. Same machine & permissions required.")
                    for i, p in enumerate(parsers):
                        mailbox = p.get("mailbox","")
                        dbp = p.get("db_path","")
                        folders = p.get("folder_paths") or []
                        cols = st.columns([3,4,2,1])
                        with cols[0]:
                            st.write(f"**Mailbox:** {mailbox}")
                        with cols[1]:
                            st.write(f"**DB:** {dbp}")
                        with cols[2]:
                            st.write(f"{len(folders)} folder(s)")
                        with cols[3]:
                            if st.button("Run", key=f"run_parser_{tname}_{i}"):
                                # Intentionally minimal: best to trigger via desktop app to keep code single-source.
                                st.warning("Trigger the parser from the desktop app for now (ensures single code path).")

    st.markdown("---")
    st.caption("Tip: Launch from the desktop app so DB and project paths are pre-wired.")

if __name__ == "__main__":
    main()
