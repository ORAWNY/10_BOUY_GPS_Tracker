# BuoyTools — Metocean Buoy & GPS Tracker

**Version 1.2** | Windows Desktop Application | Python / PyQt6

BuoyTools is a desktop monitoring application for real-time metocean buoy and GPS tracker data. It ingests sensor transmissions into a local SQLite database, visualises time-series and geographic data, and raises configurable threshold, staleness, distance, and missing-data alerts — with email notifications sent via Outlook.

---

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Application Layout](#application-layout)
- [Alert System](#alert-system)
- [Chart Types](#chart-types)
- [Email Notifications](#email-notifications)
- [Project & Database Structure](#project--database-structure)
- [Configuration](#configuration)
- [Repository Structure](#repository-structure)
- [Known Limitations](#known-limitations)
- [License](#license)

---

## Features

- **Multi-project management** — open and switch between multiple SQLite project databases from a single window.
- **Live data ingestion** — email parser and webhook API ingest incoming buoy transmissions into the database automatically.
- **Summary dashboard** — at-a-glance view of all data tables with record counts, time since last transmission, alert status badges, and battery voltage pills.
- **Per-table alert configuration** — four independent alert types per table, each with its own evaluation schedule, thresholds, email recipients, and cooldown period.
- **Alert history log** — persistent, searchable audit trail of every status transition and email event.
- **Interactive charts** — drag-and-drop chart board with XY time-series, gauge, pie, wind-rose, and GIS map chart types.
- **GIS viewer** — cartopy-based map showing GPS tracks, deployment point, and configurable distance rings.
- **Dark / light theme** — full application theming via QSS stylesheets; matplotlib figures respect the active theme.
- **Timezone-aware display** — all timestamps converted to the configured local timezone; UTC stored internally.
- **CSV export** — alert reports and chart data exportable to CSV from the viewer dialogs.
- **Watchdog process** — optional background watchdog runner keeps the application alive under automated deployments.

---

## Requirements

| Requirement | Version |
|---|---|
| Python | 3.11 or later |
| Windows | 10 / 11 (required for Outlook COM email) |
| Microsoft Outlook | Installed and configured with a send account |

### Python Dependencies

```
PyQt6
pandas
numpy
matplotlib
geopandas
cartopy
shapely
pyproj
scipy
requests
flask
PyJWT
pywin32          # Windows only — Outlook COM automation
tzdata
```

Install all dependencies:

```bash
pip install -r requirements.txt
```

---

## Installation

1. **Clone or extract** the repository to a local directory.
2. **Create a virtual environment** (recommended):
   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   ```
3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
4. **Configure the Outlook send account** — open `utils/constants.py` and set `OUTLOOK_ACCOUNT_DISPLAY_NAME` to the display name of the Outlook account that will send alert emails.
5. **Launch the application**:
   ```bash
   python gui.py
   ```

### Building a Standalone Executable

cx\_Freeze is supported. Un-comment the `cx_Freeze` line in `requirements.txt`, install it, then run the build script in `scripts/build/`.

---

## Quick Start

1. Launch the application with `python gui.py`.
2. Select **File → New Project** or **File → Open Project** to load a SQLite database.
3. Each table in the database appears as a tab. The **Summary** tab gives an overview of all tables.
4. To configure alerts for a table, open that table's tab and click the **Alerts** sub-tab.
5. Use the **+** toolbar button to add an alert, choose a type, and configure thresholds and recipients.
6. Charts are managed from the **Charts** sub-tab — use the **+** button to add a chart card, configure it, and drag cards to reorder.

---

## Application Layout

```
┌─────────────────────────────────────────────────────────────┐
│  Menu bar  (File / View / Settings / Help)                  │
├─────────────────────────────────────────────────────────────┤
│  Summary Tab  |  Table 1  |  Table 2  |  …                  │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Summary Tab:                                               │
│    Project  |  Since last  |  Alerts  |  Battery  |  …     │
│                                                             │
│  Table Tab:                                                 │
│    [Data Grid]  [Charts Board]  [Alerts]                    │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Summary Dashboard Columns

| Column | Description |
|---|---|
| Project | Table name |
| Count | Total row count |
| Since last | Time elapsed since the most recent transmission, colour-coded by staleness |
| Alerts | Status badges (S = Stale, T = Threshold, M = Missing Data, D = Distance) |
| Battery | Latest battery/voltage reading pill, colour-coded by threshold |
| Last DP | Timestamp of the most recent data point |
| First DP | Timestamp of the earliest data point |

---

## Alert System

Four alert types are available per table. Each alert is independent and stores its own state, history, and email cooldown.

### Stale Data Alert (S)

Triggers when no new data has been received within a configurable time window.

| Parameter | Description |
|---|---|
| AMBER threshold | Minutes since last record before entering AMBER |
| RED threshold | Minutes since last record before entering RED |
| Scope | **Since last** (time to now) or **Max gap** (largest historical gap in window) |
| Also monitor | Optional list of additional tables to include in the staleness check |

### Threshold Alert (T)

Triggers when a numeric column value crosses a RED or AMBER threshold.

| Parameter | Description |
|---|---|
| Column | The sensor column to monitor |
| Mode | **Greater** (higher = worse) or **Less** (lower = worse, e.g., battery voltage) |
| RED / AMBER / GREEN | Threshold values |
| Scope | **Most recent**, **max**, **min**, or **mean** over the loaded data window |

### Distance Alert (D)

Triggers when the buoy's GPS position drifts beyond a configurable radius from its deployment point.

| Parameter | Description |
|---|---|
| Latitude / Longitude columns | GPS columns to use |
| Deployment mode | **Manual** coordinates, or auto-detect from the first day / week / month of data |
| AMBER / RED radius | Distance thresholds in metres |

### Missing Data Alert (M)

Triggers when expected data columns are absent or show a systematic gap pattern within recent transmissions.

### Alert Status Levels

| Level | Meaning |
|---|---|
| GREEN | Within normal operating limits |
| AMBER | Warning — approaching limit or outside acceptable range |
| RED | Critical — threshold exceeded |
| OFF | Alert is disabled |

### Email Policy

Emails are sent on **GREEN → AMBER** and **GREEN → RED** transitions by default. Optional per-alert toggles control:

- AMBER → RED escalation emails
- Recovery (→ GREEN) emails
- Per-alert email cooldown (default 4 hours) to prevent notification storms

Each alert email includes:
- Current status and previous status
- Observed sensor value at the time of the transition
- A PNG chart attachment showing the last 12 hours of the relevant data

---

## Chart Types

Charts are added to the **Charts** tab as resizable, draggable cards.

| Type | Description |
|---|---|
| XY | Time-series line chart with optional dual Y-axes and multiple series |
| Gauge | Speedometer-style display of a single current value with configurable zones |
| Pie | Category breakdown of a data column |
| Wind Rose | Directional frequency chart for wind or current data |
| GIS | Geographic map showing GPS track, deployment point, and distance rings |

All charts support interactive pan, zoom, and image export via the matplotlib navigation toolbar. Theme (light / dark) is applied automatically.

---

## Email Notifications

Alert emails are sent via **Microsoft Outlook** using Windows COM automation (pywin32). Outlook must be installed and a send account must be configured.

**Outlook send account display name** is set in `utils/constants.py`:

```python
OUTLOOK_ACCOUNT_DISPLAY_NAME = "Metocean Configuration"
```

Each alert stores its own recipient list. Recipients are entered as comma-separated email addresses in the alert editor dialog.

---

## Project & Database Structure

Each project is a single SQLite `.db` file. BuoyTools creates its own state database alongside the project database (same path, `.alerts` extension) to persist alert states, history logs, and settings without modifying the source data file.

```
MyProject.db          ← Source data (buoy transmissions)
MyProject.alerts      ← BuoyTools state (alert status, history, settings)
```

### State Database Tables

| Table | Contents |
|---|---|
| `alerts_last_status` | Most recent evaluated status and observed value per alert |
| `alerts_flags` | Active flag state (raised / cleared) per alert |
| `alerts_log` | Full audit log of every evaluation event, transition, and email |
| `alerts_settings` | Serialised alert spec JSON per table |
| `alerts_settings_audit` | History of settings changes |

---

## Configuration

Application preferences are stored in the Windows registry under `HKCU\Software\BuoyTools\DBViewer` via `QSettings`.

| Setting | Key | Values |
|---|---|---|
| UI theme | `ui/theme` | `light` (default), `dark` |
| Accent colour | `ui/accent` | `blue`, `green`, `orange`, `purple` |

Theme and accent are changed from **Settings → Appearance** in the application menu.

---

## Repository Structure

```
.
├── gui.py                        # Application entry point and main window
├── Create_tabs.py                # Per-table tab initialisation
├── requirements.txt
├── LICENSE
│
├── utils/
│   ├── alerts/
│   │   ├── __init__.py           # Alert type registry and base protocols
│   │   ├── alerts_tab.py         # Alert evaluation engine and UI tab
│   │   ├── alerts_panel.py       # Per-table alert management dialog
│   │   ├── threshold_alert.py    # Threshold alert type (R/A/G)
│   │   ├── stale_alert.py        # Stale data alert type
│   │   ├── distance_alert.py     # GPS distance alert type
│   │   ├── missing_data_alert.py # Missing data alert type
│   │   ├── emailer.py            # Outlook COM email sender
│   │   ├── evaluator.py          # Alert spec load / save
│   │   ├── store.py              # SQLite alert state persistence
│   │   └── view_helpers.py       # Alert rendering utilities
│   │
│   ├── charts/
│   │   ├── base.py               # Chart protocol, theme helpers
│   │   ├── xy_chart.py           # XY / time-series chart
│   │   ├── gauge_chart.py        # Gauge chart
│   │   ├── pie_chart.py          # Pie chart
│   │   ├── windrose_chart.py     # Wind rose chart
│   │   └── gis_chart.py          # GIS / map chart
│   │
│   ├── Email_parser/             # Incoming email ingestion
│   ├── Web_hook_API/             # Webhook ingestion endpoint
│   ├── summary_page.py           # Summary dashboard widget
│   ├── chart_board.py            # Drag-and-drop chart dashboard
│   ├── chart_builder.py          # Chart card manager
│   ├── local_gis_viewer.py       # Cartopy-based GIS map viewer
│   ├── settings_dialog.py        # Application settings dialog
│   ├── constants.py              # Shared constants and column detection
│   ├── time_settings.py          # Timezone configuration
│   └── time_utils.py             # Duration formatting utilities
│
├── resource/
│   ├── styles/
│   │   ├── dark.qss              # Dark theme stylesheet
│   │   └── light.qss             # Light theme stylesheet
│   ├── icons/                    # Application icons
│   └── splash/                   # Splash screen assets
│
├── ui/
│   ├── db_viewer_dialog.py       # Database viewer dialog
│   └── header_editor_dialog.py   # Column header editor dialog
│
└── scripts/
    └── watchdog_runner.py        # Background watchdog process
```

---

## Known Limitations

- **Windows only** — Outlook COM automation (`pywin32`) is required for email sending and is not available on macOS or Linux.
- **Single-user** — the SQLite state database does not support concurrent writes from multiple application instances against the same project file.
- **Outlook must be running** — the COM interface requires that Outlook is open and the send account is authenticated at the time an alert email is triggered.
- **Data ingestion rate** — the application loads up to a configurable sample limit (default 20,000 rows) into memory per table for real-time evaluation. Very high-frequency sensors may require increasing this limit in Summary Settings.

---

## License

MIT License — see [LICENSE](LICENSE) for full terms.

Copyright (c) 2025 ORAWNY
