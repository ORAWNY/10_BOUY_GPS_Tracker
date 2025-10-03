import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import ipywidgets as W
from config import DATASET_30MIN

def build():
    """Build the Waves dashboard tab with two side-by-side plots and start/end DatePickers."""
    # --- Load dataset ---
    df = pd.read_csv(DATASET_30MIN)
    df['timestamp'] = pd.to_datetime(df['timestamp'])

    # Calculate max wave height if missing
    if 'dwr4_avg_maxwaveheight_0_m' not in df.columns:
        df['dwr4_avg_maxwaveheight_0_m'] = (
            df['dwr4_avg_sigwaveheight_0_m'] *
            df['dwr4_avg_maxwaveheight/sigwaveheight_0_']
        )

    # --- DatePickers ---
    min_date = df['timestamp'].min().date()
    max_date = df['timestamp'].max().date()

    start_date_picker = W.DatePicker(description='Start:', value=min_date)
    end_date_picker   = W.DatePicker(description='End:', value=max_date)

    # --- Output areas for the two plots ---
    out1 = W.Output()
    out2 = W.Output()

    def update_plots(change=None):
        start = pd.to_datetime(start_date_picker.value)
        end = pd.to_datetime(end_date_picker.value)
        filtered = df[(df['timestamp'] >= start) & (df['timestamp'] <= end)]

        # --- DWR-MKIV plot ---
        with out1:
            out1.clear_output()
            wave_df = filtered[['timestamp', 'dwr4_avg_sigwaveheight_0_m', 'dwr4_avg_maxwaveheight_0_m']].copy()
            wave_df.rename(columns={
                'dwr4_avg_sigwaveheight_0_m': 'Significant Wave Height',
                'dwr4_avg_maxwaveheight_0_m': 'Maximum Wave Height'
            }, inplace=True)
            wave_df.set_index('timestamp', inplace=True)

            fig, ax = plt.subplots(figsize=(8, 5))
            ax.plot(wave_df.index, wave_df['Significant Wave Height'], label='Significant Wave Height', color='blue', alpha=0.8)
            ax.plot(wave_df.index, wave_df['Maximum Wave Height'], label='Maximum Wave Height', color='green', alpha=0.8)
            ax.set_facecolor('#cccccc')
            ax.set_title('DWR-MKIV: Wave Heights', fontweight='bold', fontsize=14, pad=10)
            ax.set_ylabel('Wave Height (m)', fontweight='bold', fontsize=11)
            ax.tick_params(axis='both', labelsize=10)
            ax.legend()
            ax.grid(True, color='white', linewidth=1)
            ax.xaxis.set_major_locator(mdates.AutoDateLocator())
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %Y'))
            fig.autofmt_xdate(rotation=0, ha="center")
            fig.tight_layout()
            plt.show()

        # --- DWR-G plot ---
        with out2:
            out2.clear_output()
            wave_df2 = filtered[['timestamp', 'dwrg_avg_sigwaveheight_3_m', 'dwrg_avg_maxwaveheight_3_m']].copy()
            wave_df2.rename(columns={
                'dwrg_avg_sigwaveheight_3_m': 'Significant Wave Height',
                'dwrg_avg_maxwaveheight_3_m': 'Maximum Wave Height'
            }, inplace=True)
            wave_df2.set_index('timestamp', inplace=True)

            fig, ax = plt.subplots(figsize=(8, 5))
            ax.plot(wave_df2.index, wave_df2['Significant Wave Height'], label='Significant Wave Height', color='blue', alpha=0.8)
            ax.plot(wave_df2.index, wave_df2['Maximum Wave Height'], label='Maximum Wave Height', color='green', alpha=0.8)
            ax.set_facecolor('#cccccc')
            ax.set_title('DWR-G: Wave Heights', fontweight='bold', fontsize=14, pad=10)
            ax.set_ylabel('Wave Height (m)', fontweight='bold', fontsize=11)
            ax.tick_params(axis='both', labelsize=10)
            ax.legend()
            ax.grid(True, color='white', linewidth=1)
            ax.xaxis.set_major_locator(mdates.AutoDateLocator())
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %Y'))
            fig.autofmt_xdate(rotation=0, ha="center")
            fig.tight_layout()
            plt.show()

    # Connect pickers to update function
    start_date_picker.observe(update_plots, names='value')
    end_date_picker.observe(update_plots, names='value')

    # Initial plot
    update_plots()

    # --- Layout ---
    controls = W.HBox([start_date_picker, end_date_picker], layout=W.Layout(gap='15px'))
    plots = W.HBox([out1, out2], layout=W.Layout(justify_content='space-around'))

    return W.VBox([controls, plots])

# --- Standalone test ---
#if __name__ == "__main__":
#    waves_dashboard = build()
#    display(waves_dashboard)
