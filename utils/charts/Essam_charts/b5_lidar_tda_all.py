#!/usr/bin/env python
# coding: utf-8

from config import DATASET_10MIN, DATASET_30MIN
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from ipywidgets import VBox, HBox, HTML, Button, DatePicker, Output
from IPython.display import display
import matplotlib.patches as patches

# ------------------------------
# Shared height map (103 m only on Lidar-2)
# ------------------------------
HEIGHT_MAP = {
    13:  ("lidar1_avg_hws_13_m/s",  "lidar2_avg_hws_13_m/s"),
    41:  ("lidar1_avg_hws_41_m/s",  "lidar2_avg_hws_41_m/s"),
    80:  ("lidar1_avg_hws_80_m/s",  "lidar2_avg_hws_80_m/s"),
    100: ("lidar1_avg_hws_100_m/s", "lidar2_avg_hws_100_m/s"),
    103: (None,                     "lidar2_avg_hws_103_m/s"),
    120: ("lidar1_avg_hws_120_m/s", "lidar2_avg_hws_120_m/s"),
    150: ("lidar1_avg_hws_150_m/s", "lidar2_avg_hws_150_m/s"),
    170: ("lidar1_avg_hws_170_m/s", "lidar2_avg_hws_170_m/s"),
    200: ("lidar1_avg_hws_200_m/s", "lidar2_avg_hws_200_m/s"),
    240: ("lidar1_avg_hws_240_m/s", "lidar2_avg_hws_240_m/s"),
    270: ("lidar1_avg_hws_270_m/s", "lidar2_avg_hws_270_m/s"),
    300: ("lidar1_avg_hws_300_m/s", "lidar2_avg_hws_300_m/s"),
}

FREQ = "10T"  # 10-minute expected cadence

def create_lidar_tda_dashboard():
    out = Output()

    # ------------------------------
    # 1) Load dataset
    # ------------------------------
    df = pd.read_csv(DATASET_10MIN, low_memory=False)
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"]).set_index("timestamp")
    df = df[~df.index.duplicated(keep="first")].sort_index()

    all_cols = [c for pair in HEIGHT_MAP.values() for c in pair if c is not None]
    existing_cols = [c for c in all_cols if c in df.columns]
    if not existing_cols:
        raise ValueError("No expected wind speed columns found in CSV.")

    # ------------------------------
    # 2) Availability calculator
    # ------------------------------
    def availability_percent(col: str, start: pd.Timestamp, end: pd.Timestamp):
        if col is None or col not in df.columns:
            return (np.nan, None, None, 0, 0)
        s = df[col].dropna()
        if s.empty:
            return (np.nan, None, None, 0, 0)
        col_first, col_last = s.index.min(), s.index.max()
        win_start, win_end = max(pd.to_datetime(start), col_first), min(pd.to_datetime(end), col_last)
        if win_start > win_end:
            return (np.nan, col_first, col_last, 0, 0)
        s_overlap = s.loc[win_start:win_end]
        n_present = int(s_overlap.notna().sum())
        start_aligned = win_start.floor(FREQ)
        end_aligned = win_end.ceil(FREQ)
        expected_index = pd.date_range(start_aligned, end_aligned, freq=FREQ)
        n_expected = int(len(expected_index))
        pct = 100.0 * n_present / n_expected if n_expected > 0 else np.nan
        return float(np.clip(pct, 0, 100)), col_first, col_last, n_present, n_expected

    def lidar_availability_series(start, end, lidar=1):
        vals = {}
        for h, (c1, c2) in HEIGHT_MAP.items():
            col = c1 if lidar == 1 else c2
            pct, *_ = availability_percent(col, start, end)
            vals[h] = pct
        return pd.Series(vals).sort_index(ascending=False)

    # ------------------------------
    # 3) Plotting function with colored frame
    # ------------------------------
    def plot_bar_side_by_side(series1, series2, title1, title2,
                              color1='steelblue', color2='darkorange',
                              frame_color='#007acc'):
        all_heights = sorted(set(series1.index).union(series2.index), reverse=True)
        series1 = series1.reindex(all_heights)
        series2 = series2.reindex(all_heights)
        y_labels = [f"{h} m" for h in all_heights]
        y_positions = np.arange(len(all_heights))

        fig, axes = plt.subplots(1, 2, figsize=(18, max(6, len(all_heights) * 0.35)), sharey=False)
        fig.patch.set_facecolor('#f7f7f7')

        # Outer colored frame
        rect = patches.Rectangle((0, 0), 1, 1, transform=fig.transFigure,
                                 facecolor='none', edgecolor=frame_color,
                                 linewidth=2, zorder=1000)
        fig.patches.append(rect)

        for ax in axes:
            ax.grid(axis='x', linestyle='--', alpha=0.5)
            ax.set_yticks(y_positions)
            ax.set_yticklabels(y_labels)
            ax.invert_yaxis()
            ax.tick_params(axis='y', labelsize=10)
            ax.tick_params(axis='x', labelsize=10)

        # Lidar 1
        axes[0].barh(y_positions, series1.fillna(0).values, color=color1, height=0.6)
        axes[0].set_xlabel("Average Availability (%)", fontsize=11, fontweight='bold')
        axes[0].set_title(title1, fontsize=12, fontweight='bold')
        for i, v in enumerate(series1):
            axes[0].text(v + 1 if pd.notna(v) else 1, i, f"{v:.0f}%" if pd.notna(v) else "N/A",
                         va='center', fontsize=9, fontweight='bold', color='black' if pd.notna(v) else 'red')

        # Lidar 2
        axes[1].barh(y_positions, series2.fillna(0).values, color=color2, height=0.6)
        axes[1].set_xlabel("Average Availability (%)", fontsize=11, fontweight='bold')
        axes[1].set_title(title2, fontsize=12, fontweight='bold')
        for i, v in enumerate(series2):
            axes[1].text(v + 1 if pd.notna(v) else 1, i, f"{v:.0f}%" if pd.notna(v) else "N/A",
                         va='center', fontsize=9, fontweight='bold', color='black' if pd.notna(v) else 'red')

        plt.tight_layout(pad=3.0)
        plt.show()

    # ------------------------------
    # 4) Widgets & callbacks
    # ------------------------------
    header_html = HTML("<h2 style='color:#007acc; text-align:center;'>Average Availability by Height</h2>")

    global_start, global_end = df.index.min().date(), df.index.max().date()
    start_picker = DatePicker(description="Start Date", value=global_start)
    end_picker = DatePicker(description="End Date", value=global_end)
    run_btn = Button(description="Run", button_style="primary")
    status = HTML("")

    def on_run_clicked(_):
        out.clear_output()
        status.value = "<b>Running...</b>"
        start = pd.to_datetime(start_picker.value) if start_picker.value else df.index.min()
        end = pd.to_datetime(end_picker.value) if end_picker.value else df.index.max()
        if start > end:
            status.value = "<b style='color:red;'>Invalid date range.</b>"
            return
        s1 = lidar_availability_series(start, end, 1)
        s2 = lidar_availability_series(start, end, 2)
        with out:
            plot_bar_side_by_side(s1, s2, "ZX1899", "ZX1970")
        status.value = "<b>Done.</b>"

    run_btn.on_click(on_run_clicked)

    ui = VBox([
        header_html,
        HBox([start_picker, end_picker]),
        run_btn,
        status,
        out
    ])
    return ui

# --- Wrapper ---
def build():
    return create_lidar_tda_dashboard()


# --- Standalone ---
# if __name__ == "__main__":
#     dashboard_widget = create_lidar_tda_dashboard()
#     display(dashboard_widget)
