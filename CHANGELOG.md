# Changelog

All notable changes to BuoyTools are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Version scheme: `MAJOR.MINOR` — MINOR increments for new features and significant fixes; MAJOR increments for breaking changes to the project database format or alert state schema.

---

## [1.2] — 2026-03-12

### Added

- **Alert email chart — threshold type**: Threshold alert emails now include a PNG chart showing the monitored column's value over the last 12 hours with the configured RED and AMBER threshold lines overlaid. Previously, all alert email attachments used the stale-data gap timeline regardless of alert type.
- **Alert tooltip — latest observed value**: Hovering over an alert badge (S/T/M/D) in the Summary dashboard now shows the alert name and the last observed sensor value alongside the status level (e.g., `Water Temp Alert: ON (amber) — Latest: 23.4`). Requires at least one completed evaluation cycle.
- **`alert_chart_colors()` helper** (`utils/charts/base.py`): Centralised dark/light colour palette for alert viewer charts. Both the threshold and stale viewers now use this shared helper to ensure consistent theming.
- **`read_last_observed()`** (`utils/alerts/store.py`): New function to retrieve the last persisted observed sensor value from the alert state database, used by the summary page tooltip enrichment.

### Fixed

- **Battery column N/A false positive**: The Summary dashboard Battery column was showing "No reading for this sensor" even when a valid voltage reading existed in the database. Root cause: the lookup query retrieved the most recent row by timestamp, which could have a NULL value for the battery column if the latest transmission did not include that sensor. The query now uses `WHERE column IS NOT NULL` to find the most recent non-null reading.
- **Alert list table white background in dark mode**: The Alerts and Alert History tables in the Alerts tab displayed white cell backgrounds when the dark theme was active. Root cause: Qt's QSS sets the table widget background but not individual item cell backgrounds, which default to the system palette (white on Windows). Fixed by programmatically setting `QPalette::Base`, `AlternateBase`, and `Text` on both tables at initialisation. The coloured Status column cells (red/amber/green) are unaffected as explicit `setBackground()` calls take precedence over the palette.
- **Threshold viewer chart light background in dark mode**: The threshold alert inspector dialog rendered its matplotlib chart with a hardcoded white background, figure patch, axis background, spine colours, grid lines, tick labels, axis labels, title, legend background, and status band colours regardless of the active theme. All colours are now sourced from `alert_chart_colors()`.
- **Stale viewer chart light background in dark mode**: Same issue and same fix as the threshold viewer.

### Changed

- `_load_alerts_status()` in `summary_page.py` now returns a 5-tuple `(kind_char, level, enabled, spec_name, observed_str)` instead of a 3-tuple, enabling richer tooltip display. The `AlertsDelegate` paint method updated to use `*_` unpacking for forward compatibility.
- Battery N/A reason text updated from `"Latest transmission had no reading for this sensor"` to `"No readings found for this sensor"` to reflect the updated query semantics (no non-null reading has ever been stored, rather than the most recent row being null).

---

## [1.1] — 2025-11-18

### Added

- **Threshold alert viewer** (`_ThresholdViewer`): Dedicated chart dialog for inspecting threshold alert history. Shows the monitored column plotted against time with coloured GREEN/AMBER/RED background bands, threshold lines, time-weighted status percentages, and a scrollable data table. Accessible via the "View selected" button in the Alerts tab.
- **Stale alert viewer** (`StaleViewDialog`): Equivalent viewer for stale data alerts. Plots transmission gap (minutes) against time with status-coloured line segments and threshold bands.
- **Distance alert** type: Monitors GPS drift from a configured deployment point using geodetic distance (pyproj WGS84). Supports manual deployment coordinates or automatic detection from the first day, week, or month of data. Includes GIS map preview with radius rings.
- **Missing data alert** type: Detects systematic absence of expected data columns across recent transmissions.
- **Alert history log**: Persistent per-table audit log stored in the state database. All status transitions, flag events, email sends, and email skips are recorded with UTC timestamp, observed value, threshold, and notes. Displayed in the history table at the bottom of the Alerts tab.
- **Alert CSV export**: Generate a CSV report from the alert history log via the toolbar "Generate report" button.
- **Alert copy / paste**: Alert configurations can be copied and pasted between tables using the toolbar clipboard buttons.
- **Multi-table stale monitoring**: Stale alerts can monitor multiple tables simultaneously using the `Also monitor` field. Status is RED only when all monitored tables are RED (best-of-all-sources logic).
- **Battery auto-registration**: Any column whose name matches the battery/voltage pattern (e.g., `BattA`, `Voltage`, `bat_1`) is automatically registered as a threshold alert with sensible 12 V defaults (RED ≤ 11.0 V, AMBER ≤ 11.8 V) if not already configured.
- **Dark / light theme**: Full QSS-based application theming switchable from Settings → Appearance without restarting.
- **Drag-and-drop chart board**: Chart cards can be reordered by dragging. Hover overlays expose configure, clone, remove, and move controls per card.
- **Wind rose chart** type.
- **GIS chart** type with Cartopy-based rendering.
- **Gauge chart** type.

### Changed

- Alert state migrated from JSON files to SQLite (`.alerts` side-car database) for reliability and concurrent read access.
- Alert evaluation now runs on a background timer (configurable interval, default 15 minutes) rather than on-demand only.
- Email cooldown tracked per alert key to prevent notification storms after extended outages.

### Fixed

- Timezone-naive datetime comparisons causing incorrect staleness calculations when the host system clock was UTC-offset.
- Memory leak in chart cards when switching projects: canvas and figure objects now explicitly closed on removal.

---

## [1.0] — 2025-06-04

### Initial Release

- **Project management**: create and open SQLite project databases; multiple projects supported via File menu.
- **Data grid**: tabular view of all records in each database table with column sorting and filtering.
- **XY chart**: time-series line chart with dual Y-axis support, multiple series, configurable colours and line styles.
- **Pie chart**: category breakdown visualisation.
- **Stale data alert**: basic configurable AMBER/RED threshold on time since last transmission, with Outlook email notification.
- **Threshold alert**: basic R/A/G value threshold on any numeric column.
- **Email parser**: ingestion of incoming buoy transmission emails into the database.
- **Webhook API**: HTTP endpoint for direct data push from buoys with JWT authentication.
- **Summary dashboard**: overview table showing all database tables with record counts and time since last data.
- **Settings dialog**: timezone selection, Outlook account name, sample limit.
- **Watchdog runner**: script to restart the application process if it exits unexpectedly.
