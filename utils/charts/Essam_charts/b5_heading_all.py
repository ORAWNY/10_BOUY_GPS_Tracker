#!/usr/bin/env python
# coding: utf-8

from config import DATASET_10MIN, DATASET_30MIN
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from ipywidgets import GridBox, Layout, Output, DatePicker, HBox, VBox, HTML
from IPython.display import display
from math import radians, cos, sin, sqrt, atan2

DEPLOY_LAT, DEPLOY_LON = 58.799902, -6.349444

def create_heading_buoy_dashboard(csv_path=None):
    """Create Buoy-5 Heading, Displacement, and GPS Dashboard."""

    # --- Load dataset ---
    df = pd.read_csv(DATASET_10MIN, low_memory=False)
    df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
    df.dropna(subset=['timestamp'], inplace=True)
    df.set_index('timestamp', inplace=True)

    # --- Compute displacement ---
    def haversine(lat1, lon1, lat2, lon2):
        R = 6371000
        phi1, phi2 = map(radians, [lat1, lat2])
        dphi = radians(lat2 - lat1)
        dlam = radians(lon2 - lon1)
        a = sin(dphi/2)**2 + cos(phi1)*cos(phi2)*sin(dlam/2)**2
        return 2 * R * atan2(sqrt(a), sqrt(1 - a))

    df['displacement_m'] = df.apply(
        lambda row: haversine(row['dgps_avg_gpslat_3_ddeg'], row['dgps_avg_gpslon_3_ddeg'], DEPLOY_LAT, DEPLOY_LON),
        axis=1
    )

    # --- Outputs ---
    out_heading = Output()
    out_displacement = Output()
    out_gps = Output()

    # --- Plot 1: Heading Comparison ---
    def plot_heading(df_plot, out_widget):
        cols = [
            'dgps_avg_heading_3_deg',
            'adcp_avg_heading_-3_deg',
            'met_avg_compassbearing_4_deg',
            'metlidar1_avg_compassbearing_4_deg',
            'metlidar2_avg_compassbearing_4_deg'
        ]
        cols = [c for c in cols if c in df_plot.columns]
        if not cols: return

        df_plot = df_plot[cols].copy()
        df_plot.rename(columns={
            'dgps_avg_heading_3_deg': 'DGPS Heading (°)',
            'adcp_avg_heading_-3_deg': 'ADCP Heading (°)',
            'met_avg_compassbearing_4_deg': 'Met Compass (°)',
            'metlidar1_avg_compassbearing_4_deg': 'MetLidar1 Compass (°)',
            'metlidar2_avg_compassbearing_4_deg': 'MetLidar2 Compass (°)'
        }, inplace=True)

        fig, ax = plt.subplots(figsize=(12, 6))
        colors = ['blue', 'black', 'orange', 'red', 'purple']
        for idx, col in enumerate(df_plot.columns):
            ax.scatter(df_plot.index, df_plot[col], label=col, color=colors[idx % len(colors)], s=10, alpha=0.6)

        ax.set_facecolor('#cccccc')
        ax.set_title("Heading Comparison Over Time", fontweight='bold', fontsize=14, pad=12)
        ax.set_ylabel("Heading (°)", fontweight='bold')
        ax.set_ylim(0, 360)
        ax.set_yticks(range(0, 361, 60))
        ax.grid(True, color='white', linewidth=1)
        ax.legend(title="Source", fontsize='small', loc='upper right')
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        fig.autofmt_xdate(rotation=0, ha="center")
        fig.tight_layout()

        out_widget.clear_output(wait=True)
        with out_widget:
            display(fig)
        plt.close(fig)

    # --- Plot 2: Displacement over time ---
    def plot_displacement(df_plot, out_widget):
        fig, ax = plt.subplots(figsize=(12, 6))
        ax.scatter(df_plot.index, df_plot['displacement_m'], s=20, alpha=0.5, label='Displacement (m)', color='blue')
        ax.axhline(25, color='black', linestyle='--', label='25m ring')
        ax.axhline(50, color='red', linestyle='--', label='50m ring')
        ax.set_ylabel("Displacement from Deployment (m)", fontweight='bold')
        ax.set_facecolor('#cccccc')
        ax.set_title("Magnora FLS Movement Over Time", fontweight='bold', fontsize=14)
        ax.grid(True, color='white', linewidth=1)
        ax.legend(loc='upper right', fontsize=10)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        fig.autofmt_xdate(rotation=0, ha="center")
        fig.tight_layout()

        out_widget.clear_output(wait=True)
        with out_widget:
            display(fig)
        plt.close(fig)

    # --- Plot 3: GPS Density Map (Updated for better visibility) ---
    def plot_gps(df_plot, out_widget):
        lon_col, lat_col = 'dgps_avg_gpslon_3_ddeg', 'dgps_avg_gpslat_3_ddeg'
        if lon_col not in df_plot.columns or lat_col not in df_plot.columns:
            return
    
        lat_c, lon_c = df_plot[lat_col].mean(), df_plot[lon_col].mean()
        distance = haversine(DEPLOY_LAT, DEPLOY_LON, lat_c, lon_c)
    
        lat_deg_per_m = 1 / 111111
        lon_deg_per_m = 1 / (111111 * cos(radians(DEPLOY_LAT)))
        r25_lon, r50_lon = 25 * lon_deg_per_m, 50 * lon_deg_per_m
    
        fig, ax = plt.subplots(figsize=(8, 5))
        
        # Increase gridsize for larger hex cells or adjust s for scatter
        hb = ax.hexbin(df_plot[lon_col], df_plot[lat_col], gridsize=40, cmap='hot', mincnt=1,
                       edgecolors='face', linewidths=0.1, alpha=0.8)
        cb = fig.colorbar(hb, ax=ax)
        cb.set_label('Point Density')
    
        # Increase marker size
        ax.scatter(DEPLOY_LON, DEPLOY_LAT, c='green', s=120, marker='X', label='Deployed')
        ax.scatter(lon_c, lat_c, c='cyan', s=120, marker='o', label='Centroid')
        ax.plot([DEPLOY_LON, lon_c], [DEPLOY_LAT, lat_c], 'k--', linewidth=1.5)
    
        circle25 = plt.Circle((DEPLOY_LON, DEPLOY_LAT), r25_lon, color='green', fill=False, linewidth=2, label='25m Radius')
        circle50 = plt.Circle((DEPLOY_LON, DEPLOY_LAT), r50_lon, color='red', fill=False, linewidth=2, label='50m Radius')
        ax.add_patch(circle25)
        ax.add_patch(circle50)
        ax.text(lon_c, lat_c, f"{distance:.1f} m", fontsize=10, bbox=dict(facecolor='white', alpha=0.7))
    
        ax.set_facecolor('#cccccc')
        ax.set_title("Buoy GPS Density Map", fontweight='bold', fontsize=14)
        ax.set_xlabel("Longitude", fontweight='bold')
        ax.set_ylabel("Latitude", fontweight='bold')
        ax.legend(loc='upper right')
        ax.grid(True, color='white', linewidth=0.5)
        plt.axis('equal')
        plt.tight_layout()
    
        out_widget.clear_output(wait=True)
        with out_widget:
            display(fig)
        plt.close(fig)


    # --- Date pickers ---
    first_date, last_date = df.index.min().date(), df.index.max().date()
    start_picker = DatePicker(description='Start Date', value=first_date)
    end_picker = DatePicker(description='End Date', value=last_date)

    def on_date_change(change=None):
        start, end = pd.to_datetime(start_picker.value), pd.to_datetime(end_picker.value)
        if start > end:
            return
        df_filtered = df.loc[start:end]
        plot_heading(df_filtered, out_heading)
        plot_displacement(df_filtered, out_displacement)
        plot_gps(df_filtered, out_gps)

    start_picker.observe(on_date_change, names='value')
    end_picker.observe(on_date_change, names='value')

    # --- Initial plots ---
    df_filtered = df.loc[first_date:last_date]
    plot_heading(df_filtered, out_heading)
    plot_displacement(df_filtered, out_displacement)
    plot_gps(df_filtered, out_gps)

    # --- Layout ---
    controls = HBox([start_picker, end_picker])

    def create_card(title, widget):
        header = HTML(f"<h4 style='margin:0; padding:5px;'>{title}</h4>")
        return VBox([header, widget], layout=Layout(
            border='2px solid #007acc',
            padding='8px',
            margin='5px',
            box_shadow='3px 3px 8px rgba(0,0,0,0.2)',
            width='650px'
        ))

    cards = [
        create_card("Heading Comparison", out_heading),
        create_card("Displacement Over Time", out_displacement),
        create_card("Buoy GPS Map", out_gps)
    ]

    dashboard = GridBox(children=cards, layout=Layout(
        grid_template_columns="1fr 1fr", grid_auto_rows="min-content", grid_gap="10px"
    ))

    header_html = HTML("""
    <div style="text-align:center;">
        <h2 style="color:#007acc; margin-bottom:5px;">Heading, Displacement & GPS</h2>
    </div>
    """)

    return VBox([header_html, controls, dashboard])


# --- Wrapper ---
def build():
    return create_heading_buoy_dashboard()


# --- Standalone ---
# if __name__ == "__main__":
#     dashboard_widget = create_heading_buoy_dashboard()
#     display(dashboard_widget)
