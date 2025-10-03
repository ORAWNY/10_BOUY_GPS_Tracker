#!/usr/bin/env python
# coding: utf-8

from config import DATASET_10MIN, DATASET_30MIN
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from ipywidgets import VBox, HBox, HTML, Button, DatePicker, Output
from IPython.display import display
import matplotlib.patches as patches

def create_ws_daily_availability_dashboard():
    out = Output()

    # ==============================
    # 1) Load dataset
    # ==============================
    df = pd.read_csv(DATASET_10MIN, low_memory=False)
    EXPECTED_PER_DAY = 144  # 10-min data → 144 samples per day

    df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
    df = df.dropna(subset=['timestamp']).set_index('timestamp')
    df = df[~df.index.duplicated(keep='first')]

    wind_cols = [
        f'lidar{i}_avg_hws_{h}_m/s' for i in [1, 2] 
        for h in [13, 41, 80, 100, 120, 150, 170, 200, 240, 270, 300]
    ]
    wind_cols = [c for c in wind_cols if c in df.columns]
    wdf = df[wind_cols].copy()

    # ==============================
    # 2) Compute daily availability
    # ==============================
    daily_counts = wdf.resample('D').count()
    availability = daily_counts.astype(float) / EXPECTED_PER_DAY * 100.0
    availability.index.name = 'date'

    # ==============================
    # 3) Helper functions
    # ==============================
    def lidar_table_columns_as_y(availability_df, lidar_prefix):
        cols = [c for c in availability_df.columns if c.startswith(lidar_prefix)]
        if not cols:
            return pd.DataFrame()
        sub = availability_df[cols].copy()
        longf = sub.stack().reset_index()
        longf.columns = ['date', 'column', 'availability']
        table = longf.pivot_table(index='column', columns='date', values='availability', aggfunc='mean')
        table = table.reindex(cols)
        table = table.reindex(sorted(table.columns), axis=1)
        return table

    def plot_heatmap(table, title, frame_color='#007acc'):
        if table.empty:
            plt.figure(figsize=(10, 4))
            plt.text(0.5, 0.5, "No data", ha='center', va='center')
            plt.axis('off')
            plt.show()
            return

        data = table.values
        rows = table.index.values
        dates = pd.to_datetime(table.columns)

        fig, ax = plt.subplots(figsize=(16, max(6, len(rows)*0.3)))
        fig.patch.set_facecolor('#f7f7f7')

        # Outer colored frame
        rect = patches.Rectangle(
            (0, 0), 1, 1, transform=fig.transFigure,
            facecolor='none', edgecolor=frame_color, linewidth=2, zorder=1000
        )
        fig.patches.append(rect)

        # Heatmap
        im = ax.imshow(data, aspect='auto', cmap='viridis_r', origin='upper')

        ax.set_xticks(np.arange(len(dates)))
        ax.set_xticklabels([d.strftime('%Y-%m-%d') for d in dates], rotation=90)
        ax.set_yticks(np.arange(len(rows)))
        ax.set_yticklabels(rows)
        ax.invert_yaxis()

        # Values inside cells
        for i in range(len(rows)):
            for j in range(len(dates)):
                val = data[i, j]
                if not np.isnan(val):
                    ax.text(j, i, f"{val:.0f}", ha='center', va='center', fontsize=8, color='white')

        ax.set_title(title, fontsize=14, fontweight='bold')
        cbar = fig.colorbar(im, ax=ax)
        cbar.set_label("Availability (%)")

        plt.tight_layout()
        plt.show()

    # ==============================
    # 4) Widgets and callbacks
    # ==============================
    header_html = HTML("<h2 style='color:#007acc; text-align:center;'>Daily Availability by Height</h2>")

    start_date_picker = DatePicker(description='Start Date', value=availability.index.min().date())
    end_date_picker = DatePicker(description='End Date', value=availability.index.max().date())
    run_btn = Button(description="Run", button_style='primary')
    status = HTML("")

    def on_run_clicked(b):
        out.clear_output()
        status.value = "<b>Running...</b>"

        start = pd.to_datetime(start_date_picker.value) if start_date_picker.value else availability.index.min()
        end = pd.to_datetime(end_date_picker.value) if end_date_picker.value else availability.index.max()
        subset = availability.loc[start:end]

        table1 = lidar_table_columns_as_y(subset, 'lidar1_avg_hws_')
        table2 = lidar_table_columns_as_y(subset, 'lidar2_avg_hws_')

        with out:
            plot_heatmap(table1, "ZX1899 – Daily Availability by Height and Date")
            plot_heatmap(table2, "ZX1970 – Daily Availability by Height and Date")

        status.value = "<b>Done.</b>"

    run_btn.on_click(on_run_clicked)

    # ==============================
    # 5) Return dashboard VBox
    # ==============================
    dashboard_ui = VBox([
        header_html,
        HBox([start_date_picker, end_date_picker]),
        run_btn,
        status,
        out
    ])

    return dashboard_ui

# --- Wrapper ---
def build():
    return create_ws_daily_availability_dashboard()



# --- Standalone ---
#if __name__ == "__main__":
#    dashboard_widget = create_ws_daily_availability_dashboard()
#    display(dashboard_widget)
