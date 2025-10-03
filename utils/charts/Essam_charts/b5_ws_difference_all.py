#!/usr/bin/env python
# coding: utf-8

from config import DATASET_10MIN, DATASET_30MIN
import pandas as pd
import matplotlib.pyplot as plt
import ipywidgets as widgets
from IPython.display import display
import matplotlib.dates as mdates

def create_ws_comparison_dashboard():
    # --- Load dataset ---
    df = pd.read_csv(DATASET_10MIN, low_memory=False)
    df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
    df.dropna(subset=['timestamp'], inplace=True)
    df.set_index('timestamp', inplace=True)

    # --- Define heights ---
    heights = [13, 41, 80, 100, 103, 120, 150, 170, 200, 240, 270, 300]

    # --- HTML Header ---
    header_html = widgets.HTML("""
    <div style="text-align:center;">
        <h2 style="color:#007acc; font-size:2em; margin-bottom:5px;">
            Wind Speed Difference at All Heights
        </h2>
    </div>
    """)

    # --- Widgets ---
    height_dropdown = widgets.Dropdown(options=heights, value=100, description="Height (m):")
    start_date_picker = widgets.DatePicker(description="Start Date", value=df.index.min().date())
    end_date_picker = widgets.DatePicker(description="End Date", value=df.index.max().date())

    # --- Card helper ---
    def create_card(title, widget):
        header = widgets.HTML(f"<h4 style='margin:0; padding:5px;'>{title}</h4>")
        return widgets.VBox([header, widget], layout=widgets.Layout(
            border='2px solid #007acc',
            padding='10px',
            margin='5px',
            box_shadow='3px 3px 8px rgba(0,0,0,0.2)',
            width='100%'
        ))

    # --- Update plots function ---
    def update_plots(height, start_date, end_date):
        if height == 103:
            col1 = "lidar1_avg_hws_100_m/s"
            col2 = "lidar2_avg_hws_103_m/s"
            label1 = "ZX1899 @ 100m"
            label2 = "ZX1970 @ 103m"
        else:
            col1 = f"lidar1_avg_hws_{height}_m/s"
            col2 = f"lidar2_avg_hws_{height}_m/s"
            label1 = f"ZX1899 @ {height}m"
            label2 = f"ZX1970 @ {height}m"

        if col1 not in df.columns or col2 not in df.columns:
            print(f"Columns missing: {col1}, {col2}")
            return

        start = pd.to_datetime(start_date) if start_date else df.index.min()
        end = pd.to_datetime(end_date) if end_date else df.index.max()
        df_sel = df.loc[start:end, [col1, col2]].dropna()
        if df_sel.empty:
            print("No data available for this range/height.")
            return

        df_sel = df_sel.rename(columns={col1: label1, col2: label2})
        df_sel["Diff (ZX1899 - ZX1970) [m/s]"] = df_sel[label1] - df_sel[label2]
        df_filtered = df_sel[df_sel["Diff (ZX1899 - ZX1970) [m/s]"] >= 2].copy()
        df_filtered.reset_index(inplace=True)

        # --- Create plots ---
        fig, axes = plt.subplots(1, 2, figsize=(15, 6), facecolor="#f8f8f8")
        fig.subplots_adjust(top=0.85, wspace=0.3)

        # Scatter plot
        axes[0].scatter(df_sel[label1], df_sel[label2], alpha=0.6, edgecolor='k', facecolor='#1f77b4', s=40)
        axes[0].plot([df_sel[label1].min(), df_sel[label1].max()],
                     [df_sel[label1].min(), df_sel[label1].max()],
                     color="red", linestyle="--", linewidth=1.5, label="1:1 Line")
        axes[0].set_title(f"Scatter: {label1} vs {label2}", fontsize=14, fontweight="bold")
        axes[0].set_xlabel(label1 + " (m/s)", fontsize=12)
        axes[0].set_ylabel(label2 + " (m/s)", fontsize=12)
        axes[0].grid(True, linestyle='--', alpha=0.5)
        axes[0].legend(frameon=True, facecolor="white", edgecolor="black")

        # Time series plot
        axes[1].plot(df_sel.index, df_sel[label1], "o", markersize=5, alpha=0.7, label=label1, color="#1f77b4")
        axes[1].plot(df_sel.index, df_sel[label2], "o", markersize=5, alpha=0.7, label=label2, color="#ff7f0e")
        axes[1].set_title(f"Time Series: Wind Speed @ {height}m", fontsize=14, fontweight="bold")
        axes[1].set_ylabel("Wind Speed (m/s)", fontsize=12)
        axes[1].grid(True, linestyle='--', alpha=0.5)
        axes[1].legend(frameon=True, facecolor="white", edgecolor="black")
        axes[1].xaxis.set_major_locator(mdates.MonthLocator(interval=1))
        axes[1].xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        fig.autofmt_xdate(rotation=0, ha="center")

        plt.tight_layout()
        plt.show()

        if not df_filtered.empty:
            display(df_filtered.head(20))
        else:
            print("No differences >= 2 m/s found for this selection.")

    # --- Interactive output ---
    out = widgets.interactive_output(update_plots,
                                     {"height": height_dropdown,
                                      "start_date": start_date_picker,
                                      "end_date": end_date_picker})

    # --- Dashboard layout ---
    controls_card = create_card("Controls", widgets.VBox([height_dropdown,
                                                          widgets.HBox([start_date_picker, end_date_picker])]))
    plots_card = create_card("Plots & Table", out)

    dashboard_ui = widgets.VBox([header_html, controls_card, plots_card])
    return dashboard_ui

# --- Public entry point ---
def build():
    return create_ws_comparison_dashboard()

# --- Standalone test ---
# if __name__ == "__main__":
#     dashboard_widget = create_ws_comparison_dashboard()
#     display(dashboard_widget)
