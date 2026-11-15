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

Leave this terminal open. The API also serves the UI and its static files; the variables are per-terminal, and the app does not load a `.env` file automatically.

### 3. Run the UI

There is no separate UI process to start. With the API still running, open `http://127.0.0.1:8000` in your browser and sign in with username `operator` and the `UI_PASSWORD` you chose. Keep the browser on this address so the UI can communicate with the local API.

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

### 5. Start the engine

The engine is a separate process from the API/UI. Open a second PowerShell terminal, activate the same virtual environment, and set the same `UI_PASSWORD`, `LIVE_TRADING`, `DATA_DIR`, and `REPO_DATA_DIR` values before starting it:

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

### Deploy over SSH from Windows

The script deploys the current working tree to the EC2 development host and restarts the API and engine. It does not configure a new EC2 instance or upload runtime data, credentials, or `/etc/algo/env`. Perform the following steps before deploying:

1. **Restrict access.** In the EC2 security group, allow SSH (port 22) only from your trusted IP. Do not expose port 8000; the API listens on loopback and should be reached through an SSH tunnel.
2. **Prepare the host.** Use a Linux host with systemd, Python 3.12+, `tar`, and an SSH account that can run `sudo -n` commands without a password prompt. Create the `algo` user and group if they do not exist. Create `/opt/nsebot` and `/var/lib/nsebot`, with the service able to read and write `/var/lib/nsebot`, and create `/opt/nsebot/.venv` using Python 3.12+.
3. **Configure the protected environment file.** Create `/etc/algo/env` on the host (for example, based on `deploy/env.example`) and protect it with `sudo chmod 600 /etc/algo/env`. Set `LIVE_TRADING=false`, a non-placeholder `UI_PASSWORD` of at least 20 characters, `DATA_DIR=/var/lib/nsebot`, and `REPO_DATA_DIR=/opt/nsebot/data`. Do not put credentials in the deployment command or commit them to the repository.
4. **Install verified data files.** Put current, validated `nse_holidays.txt`, `nifty50.txt`, and `sector_fno_map.yaml` in `DATA_DIR` and ensure the `algo` service can read them. The holiday and symbol files are not included in this repository. The deployment script checks that all three files exist and are non-empty before it changes the running services; do not make up missing data.
5. **Create or select an EC2 key pair.** In the AWS console, open **EC2 → Network & Security → Key Pairs → Create key pair**. Choose **RSA** and the **.pem** private-key format for broad compatibility, then save the downloaded file somewhere private, such as `C:\Users\<you>\.ssh\nsebot-ec2.pem`. AWS only provides the private-key download when the key pair is created; keep it secret and never commit or send it. When launching a new instance, select this key pair. Creating a key pair later does not automatically grant access to an already-running instance; use existing admin access, EC2 Instance Connect, or Systems Manager to add its public key to the intended Linux user's `~/.ssh/authorized_keys`.
6. **Secure and test the key on Windows.** Enable the Windows OpenSSH Client and ensure `ssh.exe`, `scp.exe`, and `tar.exe` are available in PowerShell. The EC2 `.pem` is a private key, not an SSH certificate; do not paste it into this README or share it. Restrict its file permissions (replace the sample path with yours):

```powershell
$key = "$HOME\.ssh\nsebot-ec2.pem"
$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
icacls $key /inheritance:r
icacls $key /grant:r "${account}:(R)"
```

   Use the EC2 public DNS name (or IP). The usual SSH username is `ec2-user` for Amazon Linux and `ubuntu` for Ubuntu; it is not the app's `algo` service account. Before deploying, connect once interactively so OpenSSH can save the host key:

```powershell
ssh -i $key -o IdentitiesOnly=yes ec2-user@ec2-your-instance.compute.amazonaws.com
```

   Replace the username and hostname for your AMI. On the first connection, verify the displayed server host-key fingerprint through a trusted out-of-band channel before accepting it. The deployment script uses the saved host key and does not disable host-key checking.
7. **Run a dry run, then deploy.** From the repository root, replace the example hostname and key path:

```powershell
.\deploy\deploy-ec2.ps1 -HostName "ec2-your-instance.compute.amazonaws.com" -KeyFile "$HOME\.ssh\nsebot-ec2.pem" -WhatIf
.\deploy\deploy-ec2.ps1 -HostName "ec2-your-instance.compute.amazonaws.com" -KeyFile "$HOME\.ssh\nsebot-ec2.pem"
```

   The default SSH username is `ec2-user`; if your AMI uses another one, such as `ubuntu`, add `-SshUser ubuntu` to both commands. Review and confirm the deployment prompt. The script packages the application, installs and enables the systemd units, restarts both services, and checks that the API health endpoint responds. It refuses to deploy unless the environment keeps live trading disabled and the required configuration and data files are present.
8. **Check the services and open the UI.** Inspect the service status and recent logs:

```powershell
ssh -i "$HOME\.ssh\nsebot-ec2.pem" ec2-user@ec2-your-instance.compute.amazonaws.com "sudo systemctl status algo-api algo-engine --no-pager"
ssh -i "$HOME\.ssh\nsebot-ec2.pem" ec2-user@ec2-your-instance.compute.amazonaws.com "sudo journalctl -u algo-api -u algo-engine -n 100 --no-pager"
```

   If your SSH username differs, replace `ec2-user`. To use the UI without exposing port 8000, open an SSH tunnel and leave that terminal running:

```powershell
ssh -i "$HOME\.ssh\nsebot-ec2.pem" -L 18000:127.0.0.1:8000 ec2-user@ec2-your-instance.compute.amazonaws.com
```

   Then browse to `http://127.0.0.1:18000` and sign in as `operator` with `UI_PASSWORD`. Register `http://127.0.0.1:18000/kite/callback` as the Kite redirect URL and set the same URL in the app settings. Verify the **System Health Strip**, valid Kite token, and engine state before arming. Keep the bot in paper mode and do not deploy updates during market hours.

Complete and test the live order lifecycle, obtain verified seed files, and validate multiple continuous paper sessions before considering production deployment.
