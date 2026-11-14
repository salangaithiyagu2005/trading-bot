# NSE Kite bot

## Readiness

**This project is not ready for live trading or production deployment.** The app currently supports paper trading only; live order entry, protective-order management, broker reconciliation, and restart recovery are not implemented end to end. Live mode is intentionally blocked in the API and engine.

The verified NSE holiday calendar and Nifty 50 symbol list are not included. Supply validated `nse_holidays.txt` and `nifty50.txt` files in `DATA_DIR` before starting a session. The engine fails closed when required files are missing or invalid; do not make up seed data. `sector_fno_map.yaml` is included as a starter mapping and should also be checked against current exchange instruments.

The paper path has automated unit coverage, but has not been validated through a continuous market session. Treat the UI and EC2 service files as development foundations, not a live-trading deployment recipe.

## Run locally on Windows

The API and engine run as two separate processes. The API serves the UI on `127.0.0.1:8000`; the engine reads the same local database and runs the paper strategy. Keep `LIVE_TRADING=false`. The engine does not submit broker orders in paper mode, but it does need a valid Kite token for market data.

### 1. Install

Use Python 3.12 or newer. From PowerShell, open the repository folder:

```powershell
cd "C:\Users\Arun\Desktop\Trading bot"
py -0p
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

If you have Python 3.13 rather than 3.12, replace `py -3.12` with `py -3.13`. If PowerShell blocks activation, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that terminal, then activate again. This policy change applies only to that PowerShell process.

### 2. Start the API

In the first PowerShell terminal, set local environment values and start the API:

```powershell
cd "C:\Users\Arun\Desktop\Trading bot"
.\.venv\Scripts\Activate.ps1
$env:UI_PASSWORD = "choose-a-long-local-password"
$env:LIVE_TRADING = "false"
$env:DATA_DIR = Join-Path (Get-Location) "var"
$env:REPO_DATA_DIR = Join-Path (Get-Location) "data"
nsebot-api
```

Leave this terminal open. Open `http://127.0.0.1:8000` in your browser and sign in with username `operator` and the `UI_PASSWORD` you chose. The variables are per-terminal; repeat the same four assignments in the engine terminal. The app does not load a `.env` file automatically.

### 3. Configure the app

In **Settings**:

1. Save your Kite API key and API secret under **Credentials**. These are stored in the local `DATA_DIR` and are not displayed again.
2. In your Kite developer console, register `http://127.0.0.1:8000/kite/callback` as the redirect URL. Use that same `127.0.0.1` host and port in your browser. If you choose another host or port, register that exact callback URL instead.
3. Configure the three **Data files** before starting the engine:

   - `nse_holidays.txt`: current, verified exchange holidays, one `YYYY-MM-DD` date per line.
   - `nifty50.txt`: exactly 50 unique current NSE equity trading symbols, one per line. At least 48 must resolve from Kite's instrument list.
   - `sector_fno_map.yaml`: the included file is a starter mapping; review it against current instruments.

   Use verified, current data; do not make up missing entries. The engine fails closed if required files are missing or invalid.
4. Click **Regenerate Kite Token**, complete Kite login, then click **Verify Token Now**. The access token expires daily, so regenerate it when it expires. Optional: save `XAI_API_KEY` only if you want to use the AI review button; it is not needed to run the engine.

The local runtime directory is `.\var`. It contains the database, token, credentials, and instrument cache. Do not share or commit it.

### 4. Start the engine

Open a second PowerShell terminal. Activate the same virtual environment and set the same `UI_PASSWORD`, `LIVE_TRADING`, `DATA_DIR`, and `REPO_DATA_DIR` values before starting the engine:

```powershell
cd "C:\Users\Arun\Desktop\Trading bot"
.\.venv\Scripts\Activate.ps1
$env:UI_PASSWORD = "choose-a-long-local-password"
$env:LIVE_TRADING = "false"
$env:DATA_DIR = Join-Path (Get-Location) "var"
$env:REPO_DATA_DIR = Join-Path (Get-Location) "data"
nsebot-engine
```

Run only one engine process at a time. In the UI, check the **System Health Strip** and session state. Verify that the token is valid and the engine is healthy before arming. New entries are limited to the strategy's NSE market hours; outside market hours the UI may correctly show **Market Closed**. Stop both processes with `Ctrl+C`.

If a command such as `nsebot-api` is not found, confirm the virtual environment is active and rerun `python -m pip install -e .`.

### Local debugging

- Check that the API is responding: `Invoke-RestMethod http://127.0.0.1:8000/healthz`.
- If the page does not load or looks unstyled, open browser developer tools with **F12**. In **Network**, verify `/static/styles.css` and `/static/app.js` return `200`; in **Console**, inspect the first JavaScript error. The API also serves these directly at `http://127.0.0.1:8000/static/styles.css` and `http://127.0.0.1:8000/static/app.js`.
- `UI_PASSWORD must be set` means it was not set in the API terminal before starting `nsebot-api`.
- `Engine offline / stale` usually means the engine terminal is not running, exited with an error, or has a different `DATA_DIR`. Compare the four environment assignments in both terminals.
- A blocked state mentioning the calendar, breadth, or seed data means the data files are absent, invalid, stale, or contain symbols Kite cannot resolve. Review the counts and validation messages in **Settings → Data files**.
- Token or Kite errors: verify the API key/secret, callback URL, network connectivity, and daily token expiry. Then regenerate the token and click **Verify Token Now**. Keep the engine terminal open to read its detailed startup error.
- If port 8000 is occupied, stop the other local app or run the API with `python -m uvicorn nsebot.api.app:app --host 127.0.0.1 --port 8001`; register `http://127.0.0.1:8001/kite/callback` with Kite as well.
- If troubleshooting still fails, capture the first error from each terminal and the relevant UI health/status text. Redact API secrets, access tokens, passwords, and webhook URLs before sharing logs.

## Local checks

From the repository root:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q src tests
node --check ui/app.js
git --no-pager diff --check
```

## EC2 development setup

The templates in `deploy/` are intended for a restricted development host:

1. Configure a long random `UI_PASSWORD` and keep `LIVE_TRADING=false` in the protected environment file.
2. Whitelist the instance's Elastic IP in the Kite developer console.
3. Configure and validate the required data files in `DATA_DIR`.
4. Start both systemd units and verify the **System Health Strip** before arming.
5. Keep the bot in paper mode and review paper results manually. Do not enable live trading.
6. Leave the process running through the configured flatten time and do not update the application during market hours.

Complete and test the live order lifecycle, obtain verified seed files, and validate multiple continuous paper sessions before considering production deployment.
