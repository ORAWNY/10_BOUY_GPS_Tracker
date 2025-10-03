#!/usr/bin/env python
# coding: utf-8

from config import DATASET_10MIN, DATASET_30MIN
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
from ipywidgets import GridBox, Layout, Output, DatePicker, HBox, VBox, HTML
from IPython.display import display


def create_wind_dashboard():
    # --- Load dataset ---
    df = pd.read_csv(DATASET_10MIN, low_memory=False)
    df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
    df.dropna(subset=['timestamp'], inplace=True)
    df.set_index('timestamp', inplace=True)

    # --- Utility functions ---
    def get_available_heights(prefix, suffix):
        cols = [col for col in df.columns if col.startswith(prefix) and col.endswith(suffix)]
        return sorted({int(col.split('_')[-2]) for col in cols}, reverse=True)

    def get_columns(prefix, suffix):
        heights = get_available_heights(prefix, suffix)
        cols = [f"{prefix}_{h}_{suffix}" for h in heights]
        return [col for col in cols if col in df.columns]

    # --- Plot definitions ---
    plots_info = [
        ("ZX1899: Horizontal Wind Speed Over Time", "Wind Speed (m/s)", get_columns("lidar1_avg_hws", "m/s"), False),
        ("ZX1970: Horizontal Wind Speed Over Time", "Wind Speed (m/s)", get_columns("lidar2_avg_hws", "m/s"), False),
        ("ZX1899: Wind Direction Over Time", "Wind Direction (°)", get_columns("lidar1_avg_wd", "deg"), True),
        ("ZX1970: Wind Direction Over Time", "Wind Direction (°)", get_columns("lidar2_avg_wd", "deg"), True)
    ]

    def create_wind_plot(df, title, y_label, is_scatter=False):
        fig, ax = plt.subplots(figsize=(12, 6), facecolor="#f8f8f8")
        colors = ['black', 'blue', 'green', 'orange', 'purple',
                  'brown', 'cyan', 'magenta', 'olive', 'navy', 'slategray', 'red']

        for idx, col in enumerate(df.columns):
            label = col.split('_')[-2] + ' m'
            if is_scatter:
                ax.scatter(df.index, df[col], label=label, color=colors[idx % len(colors)], alpha=0.6, s=10, edgecolor='k')
            else:
                line_width = 2.5 if label in ['42 m', '41 m', '300 m'] else 1.5
                ax.plot(df.index, df[col], label=label, color=colors[idx % len(colors)], linewidth=line_width, alpha=0.9)

        ax.set_facecolor('#cccccc')
        ax.set_title(title, fontweight='bold', fontsize=16, pad=15)
        ax.set_ylabel(y_label, fontweight='bold', fontsize=12)
        ax.tick_params(axis='both', labelsize=11)
        ax.grid(True, color='white', linewidth=1)
        ax.legend(title='Height (AMSL)', bbox_to_anchor=(0.01, 1), loc='upper left', fontsize='small')

        if is_scatter:
            ax.set_ylim(0, 360)
            ax.set_yticks(np.arange(0, 361, 60))
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))

        fig.autofmt_xdate(rotation=0, ha="center")
        fig.tight_layout()
        return fig

    # --- Outputs ---
    outs_wind = [Output() for _ in plots_info]

    def update_wind_plots(start, end):
        df_filtered = df.loc[start:end]
        for out, (title, ylabel, cols, scatter) in zip(outs_wind, plots_info):
            df_plot = df_filtered[cols].copy()
            fig = create_wind_plot(df_plot, title, ylabel, is_scatter=scatter)
            out.clear_output(wait=True)
            with out:
                display(fig)
            plt.close(fig)

    # --- Date pickers ---
    first_date = df.index.min().date()
    last_date = df.index.max().date()
    start_picker = DatePicker(description='Start Date', value=first_date)
    end_picker = DatePicker(description='End Date', value=last_date)

    def on_date_change(change=None):
        start = pd.to_datetime(start_picker.value)
        end = pd.to_datetime(end_picker.value)
        if start > end:
            return
        update_wind_plots(start, end)

    start_picker.observe(on_date_change, names='value')
    end_picker.observe(on_date_change, names='value')

    # Initial plots
    update_wind_plots(first_date, last_date)

    # --- Layout ---
    controls = HBox([start_picker, end_picker])

    def create_card(title, output_widget):
        header = HTML(f"<h4 style='margin:0; padding:5px;'>{title}</h4>")
        return VBox([header, output_widget],
                    layout=Layout(
                        border='2px solid #007acc',       # colored outer frame
                        padding='8px',
                        margin='5px',
                        box_shadow='3px 3px 8px rgba(0,0,0,0.2)',
                        width='650px'
                    ))

    # Wrap each plot in a card with colored border
    wind_cards = [create_card(title, out_widget)
                  for (title, _, _, _), out_widget in zip(plots_info, outs_wind)]

    dashboard_wind = GridBox(
        children=wind_cards,
        layout=Layout(grid_template_columns="1fr 1fr",
                      grid_template_rows="1fr 1fr",
                      grid_gap="10px")
    )

    header_html = HTML("""
    <div style="text-align:center;">
        <h2 style="color:#007acc; margin-bottom:5px;">Wind Monitoring at All Heights</h2>
    </div>
    """)

    return VBox([header_html, controls, dashboard_wind])


# --- Public entry point ---
def build():
    return create_wind_dashboard()


# remove it later
#if __name__ == "__main__":
#    # create and display the dashboard when run directly
#    dashboard_widget = create_wind_dashboard()
#    display(dashboard_widget)
