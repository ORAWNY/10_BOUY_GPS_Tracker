#!/usr/bin/env python
# coding: utf-8

from config import DATASET_10MIN, DATASET_30MIN
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from ipywidgets import GridBox, Layout, Output, DatePicker, HBox, VBox, HTML
from IPython.display import display, clear_output

def create_met_adcp_dashboard(csv_path=None):
    """Creates and returns the Met & ADCP monitoring dashboard widget."""
    
    # --- Load dataset ---
    df = pd.read_csv(DATASET_10MIN, low_memory=False)
    df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
    df.dropna(subset=['timestamp'], inplace=True)
    df.set_index('timestamp', inplace=True)

    # --- Outputs ---
    out_pressure = Output()
    out_temperature = Output()
    out_adcp_speed = Output()
    out_adcp_direction = Output()

    # --- Helper Functions ---
    def plot_met(df_plot, title, ylabel, color_map, out_widget):
        fig, ax = plt.subplots(figsize=(12, 6), facecolor="#f8f8f8")
        for col in df_plot.columns:
            ax.plot(df_plot.index, df_plot[col], label=col, color=color_map.get(col, 'gray'),
                    linewidth=1.8, alpha=0.9)
        ax.set_facecolor('#cccccc')
        ax.set_title(title, fontweight='bold', fontsize=16, pad=15)
        ax.set_ylabel(ylabel, fontweight='bold', fontsize=12)
        ax.grid(True, color='white', linewidth=1)
        ax.tick_params(axis='both', labelsize=11)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
        fig.autofmt_xdate(rotation=0, ha="center")
        ax.legend(title='Sensor', fontsize='small')
        fig.tight_layout()
        out_widget.clear_output(wait=True)
        with out_widget:
            display(fig)
        plt.close(fig)

    def plot_adcp(df_plot, depth_col_pairs, title, ylabel, ylim=None, out_widget=None):
        fig, ax = plt.subplots(figsize=(12, 6), facecolor="#f8f8f8")
        handles, labels = [], []
        for depth, col in depth_col_pairs:
            if col in df_plot.columns:
                sc = ax.scatter(df_plot.index, df_plot[col], label=f"{depth:.1f} m", s=10, alpha=0.8, edgecolor='k')
                handles.append(sc)
                labels.append(f"{depth:.1f} m")
        ax.set_facecolor('#cccccc')
        ax.set_title(title, fontweight='bold', fontsize=16, pad=15)
        ax.set_ylabel(ylabel, fontweight='bold', fontsize=12)
        ax.grid(True, color='white', linewidth=1)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
        if ylim:
            ax.set_ylim(*ylim)
        ax.legend(handles[::-1], labels[::-1], title="Depth", loc="upper right", fontsize=8, title_fontsize=9)
        fig.autofmt_xdate(rotation=0, ha="center")
        fig.tight_layout()
        out_widget.clear_output(wait=True)
        with out_widget:
            display(fig)
        plt.close(fig)

    # --- Update Function ---
    def update_selected_plots(start, end):
        df_filtered = df.loc[start:end]

        # Pressure
        pressure_cols = [c for c in [
            'met_avg_pressure_4_hPa',
            'metlidar2_avg_pressure_4_hPa',
            'metlidar1_avg_pressure_4_hPa'
        ] if c in df_filtered.columns]
        df_p = df_filtered[pressure_cols].copy()
        df_p.rename(columns={
            'met_avg_pressure_4_hPa': 'Met Station',
            'metlidar2_avg_pressure_4_hPa': 'Met_ZX1970',
            'metlidar1_avg_pressure_4_hPa': 'Met_ZX1899'
        }, inplace=True)
        plot_met(df_p, 'Pressure Trends Over Time', 'Pressure (hPa)',
                 {'Met Station': '#FF8C00', 'Met_ZX1970': 'green', 'Met_ZX1899': 'blue'},
                 out_pressure)

        # Temperature
        temp_cols = [c for c in [
            'met_avg_temperature_4_degC',
            'metlidar2_avg_temperature_4_degC',
            'metlidar1_avg_temperature_4_degC'
        ] if c in df_filtered.columns]
        df_t = df_filtered[temp_cols].copy()
        df_t.rename(columns={
            'met_avg_temperature_4_degC': 'Met Station',
            'metlidar2_avg_temperature_4_degC': 'Met_ZX1970',
            'metlidar1_avg_temperature_4_degC': 'Met_ZX1899'
        }, inplace=True)
        plot_met(df_t, 'Temperature Trends Over Time', 'Temperature (°C)',
                 {'Met Station': '#FF8C00', 'Met_ZX1970': 'green', 'Met_ZX1899': 'blue'},
                 out_temperature)

        # ADCP
        depths = [-111.0, -107.0, -103.0, -15.0, -11.0, -7.0]
        speed_cols = [f"adcp_avg_currentspeed_{d}_m/s" for d in depths]
        dir_cols = [f"adcp_avg_currentdirection_{d}_deg" for d in depths]
        depth_col_pairs_speed = sorted(zip(depths, speed_cols), key=lambda x: x[0], reverse=True)
        depth_col_pairs_dir = sorted(zip(depths, dir_cols), key=lambda x: x[0], reverse=True)
        plot_adcp(df_filtered, depth_col_pairs_speed, "ADCP Current Speed Over Time", "Current Speed (m/s)", out_widget=out_adcp_speed)
        plot_adcp(df_filtered, depth_col_pairs_dir, "ADCP Current Direction Over Time", "Current Direction (°)", ylim=(0, 360), out_widget=out_adcp_direction)

    # --- DatePickers ---
    first_date = df.index.min().date()
    last_date = df.index.max().date()
    start_picker = DatePicker(description='Start Date', value=first_date)
    end_picker = DatePicker(description='End Date', value=last_date)

    def on_date_change(change=None):
        start = pd.to_datetime(start_picker.value)
        end = pd.to_datetime(end_picker.value)
        if start > end:
            return
        update_selected_plots(start, end)

    start_picker.observe(on_date_change, names='value')
    end_picker.observe(on_date_change, names='value')

    # --- Initial Display ---
    update_selected_plots(first_date, last_date)

    # --- Layout ---
    controls = HBox([start_picker, end_picker])

    def create_card(title, output_widget):
        """Helper for styled plot container cards."""
        header = HTML(f"<h4 style='margin:0; padding:5px;'>{title}</h4>")
        return VBox([header, output_widget],
                    layout=Layout(
                        border='2px solid #007acc',       # colored outer frame
                        padding='8px',
                        margin='5px',
                        box_shadow='3px 3px 8px rgba(0,0,0,0.2)',
                        width='650px'
                    ))

    cards = [
        create_card("Pressure", out_pressure),
        create_card("Temperature", out_temperature),
        create_card("ADCP Current Speed", out_adcp_speed),
        create_card("ADCP Current Direction", out_adcp_direction)
    ]

    dashboard = GridBox(children=cards,
                        layout=Layout(grid_template_columns="1fr 1fr",
                                      grid_template_rows="1fr 1fr",
                                      grid_gap="10px"))

    header_html = HTML("""
    <div style="text-align:center;">
        <h2 style="color:#007acc; margin-bottom:5px;">Met & ADCP Monitoring</h2>
    </div>
    """)

    return VBox([header_html, controls, dashboard])

# --- Build Wrapper for Main App ---
def build():
    return create_met_adcp_dashboard()

#if __name__ == "__main__":
#    from IPython.display import display
#    dashboard_widget = create_met_adcp_dashboard()
#    display(dashboard_widget)
