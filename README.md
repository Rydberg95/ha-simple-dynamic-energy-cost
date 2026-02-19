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