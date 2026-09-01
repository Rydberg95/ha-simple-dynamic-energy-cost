# Simple Dynamic Energy Cost

A Home Assistant custom component that calculates the accumulated cost of a device based on its energy consumption (kWh) and a dynamic electricity price sensor (like Nordpool).

## Features
* Configurable via the UI (Config Flow).
* Takes any accumulating energy sensor (kWh).
* Takes any dynamic price sensor.
* Creates separate sensors for Hourly, Daily, and Monthly accumulated costs.

## Installation via HACS

1. Go to HACS -> Integrations.
2. Click the three dots in the top right corner and select **Custom repositories**.
3. Add the URL of this repository and select **Integration** as the category.
4. Click **Add**, then close the modal.
5. Click **Explore & Download Repositories** and search for "Dynamic Energy Cost".
6. Download the integration and restart Home Assistant.

## Configuration
Go to **Settings** -> **Devices & Services** -> **Add Integration**, search for "Dynamic Energy Cost", and follow the UI prompts to select your sensors and desired accumulation periods.

### Export & monthly report
Each config entry creates a **Last Export** sensor and an **Export Month** button. Pressing the button (or calling the `simple_dynamic_energy_cost.export_month` service with a `start_date`) writes a CSV and a PDF bookkeeping report for the previous completed month to the `www/` folder and exposes direct download links (`/local/...`) on the Last Export sensor.

### Monthly ntfy notifications
You can have the download links for each completed month pushed straight to your devices via [ntfy](https://ntfy.sh) — no automations needed.

**Prerequisites:**
1. Install the ntfy app on each device that should receive the notification.
2. In the ntfy app, subscribe each device to a topic. Pick long, unguessable topic names (e.g. `energy-report-x7k2m9q4`) — anyone who knows a topic name can send to and read from it.
3. In Home Assistant, go to **Settings** -> **System** -> **Network** and set your **external URL**, so the notification can contain fully clickable download links.

**Setup:**
1. Go to the integration's **Configure** (options) dialog.
2. Enable **Send ntfy notification after each month**.
3. Add one topic per device under **Ntfy topics** (type the name and press Enter).
4. Optionally change the **Ntfy server** (leave as `https://ntfy.sh` unless you self-host ntfy) and the **Notification time** (default 09:00).

On the 1st of every month at the configured time, the integration exports the previous month's report (CSV + PDF) and pushes a notification with the cost summary and direct download links to each configured topic. If the notification can't be delivered, a persistent notification with the links is created in Home Assistant instead. If Home Assistant restarts on the morning of the 1st after the scheduled time, the notification is sent once as a catch-up (never twice for the same month).