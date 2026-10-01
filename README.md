# Simple Dynamic Energy Cost

A Home Assistant custom component that calculates the accumulated cost of a device based on its energy consumption (kWh) and a dynamic electricity price sensor (like Nordpool).

## Features
* Configurable via the UI (Config Flow).
* Takes any accumulating energy sensor (kWh).
* Takes any dynamic price sensor.
* Creates separate sensors for Hourly, Daily, and Monthly accumulated costs.
* Writes a monthly bookkeeping report as CSV and PDF, with a ready-made dashboard card.
* Optionally pushes the report and its download links to your devices with ntfy.

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
Each config entry creates a **Last Export** sensor and an **Export Month** button. Pressing the button (or calling the `simple_dynamic_energy_cost.export_month` service with a `start_date`) writes a CSV and a PDF bookkeeping report for the previous completed month to the `www/` folder and exposes the download links on the Last Export sensor as full URLs, or as relative `/local/...` paths when Home Assistant has no external or internal URL configured.

The Last Export sensor keeps a history of the last 24 exported months:
* `reports`: month key with `pdf`, `csv`, `total_cost`, `currency`, `energy_consumed_kwh`, `spot_cost`, `fixed_cost`, `complete`, `split_recovered`, `data_from` and `data_to`.
* `available_months`: the month keys in that history.
* `files`: the download links for the most recent export.
* `card_url`: link to the generated dashboard card file.

After every export (button, service, or the automatic monthly run) the integration also creates a persistent notification in Home Assistant with the total for the month, links to the PDF and the CSV, and a warning if the data coverage for the month is incomplete, if the split between spot price and fixed addition could not be recovered, or if the download link is not publicly reachable. The notification uses a fixed id, so every new month replaces the previous one instead of stacking up.

### Seeing the links in Home Assistant
On setup and after every export the integration writes a ready-made Lovelace card to `config/www/energy_cost_report_card.yaml`, served at `/local/energy_cost_report_card.yaml`. It shows a list of every exported month with the month, the total cost, the consumption in kWh and clickable PDF and CSV links, and it updates itself as new months are exported. The `card_url` attribute on the Last Export sensor points at the file.

An integration cannot add cards to your dashboard, so you add it once yourself:
* UI mode: **Settings** -> **Dashboards**, edit the view, click **Add card** -> **Manual** (called "YAML" in some versions), then paste the contents of the file.
* Raw dashboard mode: if you edit the view in raw YAML mode, paste the card configuration straight into the view.

To get the contents, open `/local/energy_cost_report_card.yaml` in a browser (or in your editor). Opening it directly only shows the YAML text, not a rendered card, so use it to copy the configuration into the **Manual** card, and then delete or keep the file as you like.

### Monthly ntfy notifications
You can have the download links for each completed month pushed straight to your devices via [ntfy](https://ntfy.sh) — no automations needed.

**Prerequisites:**
1. Install the ntfy app on each device that should receive the notification.
2. In the ntfy app, subscribe each device to a topic. Pick long, unguessable topic names (e.g. `energy-report-x7k2m9q4`) — anyone who knows a topic name can send to and read from it. A topic name may only contain letters, numbers, underscores and dashes, and can be up to 64 characters. Anything else is replaced with a dash, and a topic that ends up without any letter, number or underscore is skipped and logged as a warning.
3. In Home Assistant, go to **Settings** -> **System** -> **Network** and set your **external URL**, so the notification can contain fully clickable download links. The URL has to be reachable from the phone. Without a reachable URL the links in the notification are relative, tapping them does nothing, and the integration logs a warning and says so in the notification.

**Setup:**
1. Go to the integration's **Configure** (options) dialog.
2. Enable **Send ntfy notification after each month**.
3. Add one topic per device under **Ntfy topics** (type the name and press Enter).
4. Optionally change the **Ntfy server** (leave as `https://ntfy.sh` unless you self-host ntfy) and the **Notification time** (default 09:00).

On the 1st of every month at the configured time, the integration exports the previous completed month's report (CSV + PDF) and pushes the summary and the download links to each configured topic. Tapping the notification opens the PDF, and the notification has separate "Open PDF report" and "Open CSV report" actions.

**Delivery is reliable:** Home Assistant also checks once an hour, and once at start-up, whether last month's report is still undelivered. A month is only marked as notified after ntfy confirms the delivery, so a send that failed or timed out (wrong topic name, network problem, self-hosted server down) is retried instead of being dropped, and a report that was missed while Home Assistant was off is sent as soon as Home Assistant is back. The same month is never delivered twice.

If delivery fails, the report data is still written and a persistent notification with the links is created in Home Assistant instead. A second notification then tells you that the ntfy push failed and that it will be retried. The Last Export sensor exposes `notify_last_attempt`, `notify_last_ok` and `notify_error` for the same information.

**Known limitation:** if Home Assistant is down across more than one month boundary, only the immediately previous month is sent automatically. Earlier gaps need a manual `simple_dynamic_energy_cost.export_month` service call with a `start_date`.

**Testing without waiting for the 1st:** call the `simple_dynamic_energy_cost.send_test_notification` entity service on the Last Export sensor (Developer Tools → Actions, or via an automation). It runs the exact same pipeline: exports the previous completed month, pushes the notification, and creates the persistent notification with the links, including the dashboard card update. It does not mark the month as notified, so you can run it as often as you like. If your self-hosted ntfy server uses a certificate Home Assistant doesn't trust, disable **Verify SSL certificate** in the options (only do this on a trusted LAN).
