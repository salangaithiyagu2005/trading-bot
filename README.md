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

### 2. Start the API and engine

In one PowerShell terminal, set local environment values and start both processes:

```powershell
cd "C:\Users\Arun\Desktop\Trading bot"
.\.venv\Scripts\Activate.ps1
$env:UI_PASSWORD = "choose-a-long-local-password"
$env:LIVE_TRADING = "false"
$env:DATA_DIR = Join-Path (Get-Location) "var"
$env:REPO_DATA_DIR = Join-Path (Get-Location) "data"
nsebot
```

If `UI_PASSWORD` is not set, `nsebot` prompts for it without echoing the input. It starts the API and engine as separate processes in the same terminal; press `Ctrl+C` to stop both. The launcher defaults `LIVE_TRADING` to `false` and the data directories to this repository's `var` and `data` folders when those variables are not set. Environment variables are per-terminal, and the app does not load a `.env` file automatically.

### 3. Run the UI

There is no separate UI process to start. With `nsebot` still running, open `http://127.0.0.1:8000` in your browser and sign in with username `operator` and the `UI_PASSWORD` you chose. Keep the browser on this address so the UI can communicate with the local API.

### 4. Configure the app

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

Run only one engine process at a time. In the UI, check the **System Health Strip** and session state. Verify that the token is valid and the engine is healthy before arming. New entries are limited to the strategy's NSE market hours; outside market hours the UI may correctly show **Market Closed**. You can also start the API and engine separately with `nsebot-api` and `nsebot-engine` when needed.

If a command such as `nsebot` is not found, confirm the virtual environment is active and rerun `python -m pip install -e .`.

## Initial EC2 setup and deployment

The EC2 setup script assumes a systemd-based Linux host, Python 3.12 or newer, and this repository checked out at `/home/ec2-user/trading-bot`. It creates the `algo` service account if needed, deploys the checkout to `/opt/nsebot`, creates `/var/lib/nsebot` for persistent runtime data, installs the Python project in a virtual environment, provisions the configured DynamoDB table, installs/enables the API and engine systemd units, and starts them. It does not install Python or invent the missing NSE calendar and Nifty 50 data.

Run from the EC2 checkout:

```bash
cd /home/ec2-user/trading-bot
sudo bash deploy/setup-ec2.sh
```

The first run creates `/etc/algo/env` with DynamoDB selected. Set `AWS_REGION` in that file and ensure the EC2 instance profile can access DynamoDB before rerunning setup. The script is safe to rerun after source changes: it preserves `/etc/algo/env` and `/var/lib/nsebot`, redeploys source to `/opt/nsebot`, and restarts the services. Opening or editing the source alone does not update the running services; rerun the script to deploy. It excludes Git metadata, virtual environments, local runtime files, SSH connection notes, and private key files from the deployment copy.

The first run generates a UI password in the protected `/etc/algo/env` file. Retrieve it when needed with `sudo grep '^UI_PASSWORD=' /etc/algo/env`; the UI username is `operator`. The API binds to `127.0.0.1:8000`, so connect from your computer using an SSH tunnel rather than opening port 8000 publicly:

```bash
ssh -L 8000:127.0.0.1:8000 ec2-user@<EC2_PUBLIC_DNS>
```

Then open `http://127.0.0.1:8000` locally. Configure Kite credentials through the UI after starting the API. Check service logs with `sudo journalctl -u algo-api -u algo-engine -f` and status with `sudo systemctl status algo-api algo-engine`.

The required verified `nse_holidays.txt` and `nifty50.txt` files are not included in this repository; configure valid files in the UI before expecting a healthy engine. Keep `LIVE_TRADING=false`. This setup does not make the project ready for live trading.

### Use Amazon DynamoDB

EC2 deployments use a single DynamoDB table with on-demand billing. Its logical records (sessions, trades, rules, orders, events, reviews, commands, settings, and engine status) use partition/sort keys and a secondary index for pending commands. Local development and tests continue to use SQLite by default; set `DATABASE_BACKEND=dynamodb` to use DynamoDB.

1. In AWS, choose a region and attach an EC2 instance profile that grants the app `GetItem`, `PutItem`, `UpdateItem`, `Query`, and `Scan` access to the chosen table. Setup also needs `DescribeTable`, `CreateTable`, and `UpdateTable` to provision the table and index. Do not put AWS access keys in the repository or environment file; boto3 uses the instance role on EC2.
2. Set `AWS_REGION` and `DYNAMODB_TABLE` in `/etc/algo/env`. The setup script creates the table if needed. If the table must be initialized manually, use:

   ```bash
   sudo -u algo bash -c 'set -a; . /etc/algo/env; set +a; /opt/nsebot/.venv/bin/nsebot-dynamodb-init'
   ```

3. If migrating an existing SQLite install, stop the services, back up SQLite, and copy its records to DynamoDB:

   ```bash
   sudo systemctl stop algo-api algo-engine
   sudo -u algo cp -p /var/lib/nsebot/algo.db "/var/lib/nsebot/algo.db.before-dynamodb.$(date +%Y%m%d%H%M%S)"
   sudo -u algo bash -c 'set -a; . /etc/algo/env; set +a; /opt/nsebot/.venv/bin/nsebot-migrate-sqlite --source "$DATA_DIR/algo.db"'
   ```

4. Start and check both services:

   ```bash
   sudo systemctl start algo-api algo-engine
   sudo systemctl status algo-api algo-engine
   ```

The migration preserves record IDs and can be rerun. It migrates `algo.db` records only; credentials, Kite token, and NSE files remain in `/var/lib/nsebot`. This implementation scans DynamoDB for some all-history reports and event views, so usage and charges can grow with stored data. On-demand DynamoDB is not guaranteed to be free; eligibility and pricing depend on the AWS account and current AWS terms, so check pricing before enabling it.

### Local debugging

- Check that the API is responding: `Invoke-RestMethod http://127.0.0.1:8000/healthz`.
- If the page does not load or looks unstyled, open browser developer tools with **F12**. In **Network**, verify `/static/styles.css` and `/static/app.js` return `200`; in **Console**, inspect the first JavaScript error. The API also serves these directly at `http://127.0.0.1:8000/static/styles.css` and `http://127.0.0.1:8000/static/app.js`.
- If `nsebot` reports that `UI_PASSWORD` must be set, set it in the terminal or start the launcher interactively so it can prompt you.
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
