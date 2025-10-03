#!/usr/bin/env python
# coding: utf-8

from config import DATASET_10MIN, DATASET_30MIN
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from sklearn.linear_model import LinearRegression
import ipywidgets as widgets
from IPython.display import display, clear_output

def create_wd_correlation_dashboard():
    # --- Load dataset ---
    df = pd.read_csv(DATASET_10MIN, low_memory=False)
    df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
    df.dropna(subset=['timestamp'], inplace=True)
    df.set_index('timestamp', inplace=True)

    # --- Define lidar pairs ---
    lidar_pairs = [
        (13, 13), (41, 41), (80, 80),
        (100, 100), (100, 103), (120, 120),
        (150, 150), (170, 170), (200, 200),
        (240, 240), (270, 270), (300, 300)
    ]

    # --- Card helper ---
    def create_card(title, widget):
        header = widgets.HTML(f"<h4 style='margin:0; padding:5px;'>{title}</h4>")
        return widgets.VBox([header, widget], layout=widgets.Layout(
            border='2px solid #007acc',       # colored outer frame
            padding='10px',
            margin='5px',
            box_shadow='3px 3px 8px rgba(0,0,0,0.2)',
            width='100%'
        ))

    # --- Correlation plotting ---
    def plot_wd_correlation(df, lidar_pairs):
        fig, axes = plt.subplots(2, 6, figsize=(20, 9), constrained_layout=True, facecolor="#f8f8f8")
        axes = axes.flatten()

        for i, (h1, h2) in enumerate(lidar_pairs):
            col1 = f'lidar1_avg_wd_{h1}_deg'
            col2 = f'lidar2_avg_wd_{h2}_deg'

            if col1 not in df.columns or col2 not in df.columns:
                continue

            temp_df = df[[col1, col2]].dropna()
            if temp_df.empty:
                continue

            axes[i].scatter(temp_df[col1], temp_df[col2], s=15, alpha=0.7, color="#1f77b4", edgecolor='k')

            # Linear regression
            X = temp_df[col1].values.reshape(-1, 1)
            y = temp_df[col2].values
            reg = LinearRegression().fit(X, y)
            axes[i].plot(temp_df[col1], reg.predict(X), color='red', linestyle='--', linewidth=1.5)

            slope, intercept, r2 = reg.coef_[0], reg.intercept_, reg.score(X, y)
            title_height = f"{h1}m" if h1 == h2 else f"{h1}m vs {h2}m"

            axes[i].set_title(f'Correlation of WD [{title_height}]', fontsize=11)
            axes[i].set_xlabel(f'ZX1899 WD @ {h1}m (°)', fontsize=10)
            axes[i].set_ylabel(f'ZX1970 WD @ {h2}m (°)', fontsize=10)
            axes[i].set_xlim(0, 360)
            axes[i].set_ylim(0, 360)
            axes[i].set_xticks(np.arange(0, 361, 60))
            axes[i].set_yticks(np.arange(0, 361, 60))
            axes[i].grid(True, linestyle='--', alpha=0.5)
            axes[i].set_aspect('equal', adjustable='box')
            axes[i].text(0.95, 0.05,
                         f'y = {slope:.2f}x + {intercept:.2f}\nR² = {r2:.2f}\nN = {len(temp_df)}',
                         transform=axes[i].transAxes,
                         fontsize=9, va='bottom', ha='right',
                         bbox=dict(facecolor='white', alpha=0.8, edgecolor='black', boxstyle='round,pad=0.3'))

        fig.suptitle("Buoy-5 Wind Direction Correlation Dashboard",
                     fontsize=18, fontweight="bold", y=1.04, color="#007acc")
        fig.text(0.5, 0.99,
                 "ZX1899 vs ZX1970 - Wind Direction Correlation at Various Heights",
                 ha='center', fontsize=14, color='gray')
        plt.show()

    # --- Widgets ---
    start_wd = widgets.DatePicker(description="Start", value=df.index.min().date())
    end_wd   = widgets.DatePicker(description="End", value=df.index.max().date())
    out = widgets.Output()

    def update_dashboard(change=None):
        with out:
            clear_output(wait=True)
            df_wd = df.loc[str(start_wd.value):str(end_wd.value)]
            plot_wd_correlation(df_wd, lidar_pairs)

    for w in [start_wd, end_wd]:
        w.observe(update_dashboard, 'value')

    # Initial display
    update_dashboard()

    # --- Dashboard layout with colored frames ---
    controls_card = create_card("Controls", widgets.HBox([start_wd, end_wd]))
    plots_card    = create_card("Correlation Plots", out)

    return widgets.VBox([controls_card, plots_card])

# --- Public entry point ---
def build():
    return create_wd_correlation_dashboard()

# remove it later
#if __name__ == "__main__":
#    dashboard_widget = create_wd_correlation_dashboard()
#    display(dashboard_widget)
