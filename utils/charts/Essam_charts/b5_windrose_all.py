import pandas as pd
import matplotlib.pyplot as plt
from windrose import WindroseAxes
import ipywidgets as W
from IPython.display import display
from config import DATASET_10MIN

def build():
    """Build the WindRose dashboard tab with two side-by-side windrose plots and date range selectors."""
    
    # --- Load dataset ---
    df = pd.read_csv(DATASET_10MIN)
    df['timestamp'] = pd.to_datetime(df['timestamp'])

    # --- Output widgets for the plots ---
    out1 = W.Output()
    out2 = W.Output()

    # --- Date range selectors ---
    min_date = df['timestamp'].min().date()
    max_date = df['timestamp'].max().date()

    start_picker = W.DatePicker(description='Start Date', value=min_date)
    end_picker = W.DatePicker(description='End Date', value=max_date)

    def update_plots(change=None):
        start = pd.to_datetime(start_picker.value)
        end = pd.to_datetime(end_picker.value)
        if start > end:
            return
        filtered = df[(df['timestamp'] >= start) & (df['timestamp'] <= end)]

        def plot_windrose(out_widget, column_dir, column_speed, title):
            with out_widget:
                out_widget.clear_output(wait=True)
                fig = plt.figure(figsize=(6, 6))
                ax = WindroseAxes.from_ax()
                ax.bar(filtered[column_dir], filtered[column_speed], bins=8, normed=True, opening=0.8, edgecolor='white')
                ax.set_yticks(range(0, 28, 4))
                ax.set_yticklabels(range(0, 28, 4), fontsize=10)
                ax.grid(color='white', linestyle='-', linewidth=0.5)
                ax.set_facecolor('#cccccc')
                ax.set_legend()
                ax.set_title(title, y=1.05)
                plt.show()

        plot_windrose(out1, 'lidar1_avg_wd_150_deg', 'lidar1_avg_hws_150_m/s', 'LiDAR1 Windrose at 150m AMSL')
        plot_windrose(out2, 'lidar2_avg_wd_150_deg', 'lidar2_avg_hws_150_m/s', 'LiDAR2 Windrose at 150m AMSL')

    # --- Connect pickers to update function ---
    start_picker.observe(update_plots, names='value')
    end_picker.observe(update_plots, names='value')

    # --- Initial plot ---
    update_plots()

    # --- Layout ---
    controls = W.HBox([start_picker, end_picker], layout=W.Layout(justify_content='flex-start', gap='10px'))
    plots = W.HBox([out1, out2], layout=W.Layout(justify_content='space-around'))
    
    return W.VBox([controls, plots])

# --- Standalone test ---
# if __name__ == "__main__":
#     windrose_tab = build()
#     display(windrose_tab)
