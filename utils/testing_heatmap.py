import sqlite3
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from math import radians, sin, cos, sqrt, atan2
from scipy.stats import gaussian_kde
from scipy.spatial import cKDTree

db_path = r"D:\04_Met_Ocean\02_Python\10_BOUY_GPS_Tracker\Logger_Data\Logger_Data.db"
table_name = "Inbox_Logger_Data_SEAI_L8"

conn = sqlite3.connect(db_path)
query = f"SELECT Lat, Lon, received_time FROM {table_name}"
df = pd.read_sql_query(query, conn)
conn.close()

print("Data types:\n", df.dtypes)
print("\nFirst 10 rows:\n", df.head(10))

# Convert lat, lon to numeric, coerce errors and drop rows with missing values
df['Lat'] = pd.to_numeric(df['Lat'], errors='coerce')
df['Lon'] = pd.to_numeric(df['Lon'], errors='coerce')
df.dropna(subset=['Lat', 'Lon'], inplace=True)
df.rename(columns={"Lat": "lat", "Lon": "lon"}, inplace=True)

# Convert received_time to datetime and drop rows with invalid dates
df['received_time'] = pd.to_datetime(df['received_time'], errors='coerce')
df.dropna(subset=['received_time'], inplace=True)

deployed_lat = 54.2708
deployed_lon = -10.2767

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = map(radians, [lat1, lat2])
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2)**2 + cos(phi1) * cos(phi2) * sin(dlambda / 2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))

lat_c = df['lat'].mean()
lon_c = df['lon'].mean()
distance = haversine(deployed_lat, deployed_lon, lat_c, lon_c)

lat_deg_per_m = 1 / 111111
lon_deg_per_m = 1 / (111111 * cos(radians(deployed_lat)))

# Convert lat/lon to meters relative to deployed location
df['x'] = (df['lon'] - deployed_lon) / lon_deg_per_m
df['y'] = (df['lat'] - deployed_lat) / lat_deg_per_m

# Create values array for KDE and KDTree
values = np.vstack([df['x'], df['y']])

# Initialize KDE
kde = gaussian_kde(values)

# Create grid with ~1m resolution within buffered extent of data points
buffer_m = 20
x_min = df['x'].min() - buffer_m
x_max = df['x'].max() + buffer_m
y_min = df['y'].min() - buffer_m
y_max = df['y'].max() + buffer_m

x_grid = np.arange(x_min, x_max, 0.5)
y_grid = np.arange(y_min, y_max, 0.5)
xx, yy = np.meshgrid(x_grid, y_grid)

# Evaluate KDE on grid points
density = kde(np.vstack([xx.ravel(), yy.ravel()])).reshape(xx.shape)

# Build KDTree for distance masking
tree = cKDTree(values.T)
distances, _ = tree.query(np.vstack([xx.ravel(), yy.ravel()]).T)
distances = distances.reshape(xx.shape)
density_masked = np.ma.masked_where(distances > buffer_m, density)

# Get last known point based on received_time
last_point = df.loc[df['received_time'].idxmax()]
last_x = (last_point['lon'] - deployed_lon) / lon_deg_per_m
last_y = (last_point['lat'] - deployed_lat) / lat_deg_per_m

fig, ax = plt.subplots(figsize=(8, 6))

ax.set_facecolor('#19217d')  # Ocean-blue background

c = ax.imshow(
    density_masked,
    origin='lower',
    extent=(x_min, x_max, y_min, y_max),
    cmap='jet',
    alpha=0.8,
    interpolation='bilinear'
)

cb = fig.colorbar(c, ax=ax)
cb.set_label('Interpolated Point Density')

ax.scatter(0, 0, c='green', s=100, marker='X', label='Deployed (0,0)')
ax.scatter(
    (lon_c - deployed_lon) / lon_deg_per_m,
    (lat_c - deployed_lat) / lat_deg_per_m,
    c='cyan',
    s=100,
    marker='o',
    label='Centroid'
)
ax.plot(
    [0, (lon_c - deployed_lon) / lon_deg_per_m],
    [0, (lat_c - deployed_lat) / lat_deg_per_m],
    'k--'
)

ax.scatter(last_x, last_y, c='yellow', s=120, edgecolors='black', marker='*', label='Last Known Point')
ax.plot([0, last_x], [0, last_y], color='yellow', linestyle='-', linewidth=2, label='Deployed to Last Known')

circle25 = plt.Circle((0, 0), 25, color='green', fill=False, linestyle='-', linewidth=2, label='25m Radius')
circle50 = plt.Circle((0, 0), 50, color='red', fill=False, linestyle='-', linewidth=2, label='50m Radius')
ax.add_patch(circle25)
ax.add_patch(circle50)

ax.text(
    (lon_c - deployed_lon) / lon_deg_per_m,
    (lat_c - deployed_lat) / lat_deg_per_m,
    f"{distance:.1f} m",
    fontsize=10,
    bbox=dict(facecolor='white', alpha=0.7)
)

ax.set_title('Interpolated Buoy Location Density (~10m radius)', fontsize=14, fontweight='bold')
ax.set_xlabel('Meters East of Deployed')
ax.set_ylabel('Meters North of Deployed')
ax.legend()
ax.grid(True, color='white', linewidth=0.5)
ax.set_aspect('equal')
plt.tight_layout()
plt.show()
