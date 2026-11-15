[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$HostName,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$KeyFile,

    [ValidateNotNullOrEmpty()]
    [string]$SshUser = "ec2-user"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $KeyFile -PathType Leaf)) {
    throw "SSH private key not found: $KeyFile"
}

$ssh = Get-Command ssh.exe -ErrorAction SilentlyContinue
$scp = Get-Command scp.exe -ErrorAction SilentlyContinue
$tar = Get-Command tar.exe -ErrorAction SilentlyContinue
if (-not $ssh -or -not $scp -or -not $tar) {
    throw "OpenSSH (ssh.exe, scp.exe) and tar.exe are required. Enable the Windows OpenSSH Client feature."
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$archiveName = "nsebot-deploy-$([guid]::NewGuid().ToString('N')).tar.gz"
$archivePath = Join-Path ([System.IO.Path]::GetTempPath()) $archiveName
$remoteArchive = "/tmp/$archiveName"
$remoteStage = "/tmp/nsebot-stage-$([guid]::NewGuid().ToString('N'))"
$target = "$SshUser@$HostName"

try {
    $paths = @(
        "README.md",
        "pyproject.toml",
        "src",
        "ui",
        "config",
        "data",
        "deploy/algo-api.service",
        "deploy/algo-engine.service"
    )
    & $tar.Source -czf $archivePath -C $repoRoot @paths
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to package the application source."
    }

    if ($PSCmdlet.ShouldProcess($target, "Deploy paper-trading application and restart API/engine services")) {
        & $scp.Source -i $KeyFile -o BatchMode=yes $archivePath "${target}:$remoteArchive"
        if ($LASTEXITCODE -ne 0) {
            throw "Could not copy the deployment archive to $target."
        }

        $remoteScript = @"
set -eu
archive='$remoteArchive'
stage='$remoteStage'
app='/opt/nsebot'
env_file='/etc/algo/env'

if [ ! -f "`$env_file" ]; then
    echo "Missing `$env_file; configure the protected service environment first." >&2
    exit 1
fi
if ! awk -F= '`$1 == "LIVE_TRADING" { count++; if (`$2 != "false") invalid=1 } END { exit !(count == 1 && !invalid) }' "`$env_file"; then
    echo "Deployment refused: `$env_file must contain exactly LIVE_TRADING=false." >&2
    exit 1
fi
if ! grep -Eq '^UI_PASSWORD=.{20,}$' "`$env_file" || grep -q '^UI_PASSWORD=REPLACE_WITH_' "`$env_file"; then
    echo "Set a non-placeholder UI_PASSWORD of at least 20 characters in `$env_file." >&2
    exit 1
fi
if ! data_dir=`$(awk -F= '`$1 == "DATA_DIR" { count++; value=substr(`$0, index(`$0, "=") + 1) } END { if (count != 1) exit 1; print value }' "`$env_file"); then
    echo "Set exactly one DATA_DIR in `$env_file." >&2
    exit 1
fi
case "`$data_dir" in
    /*) ;;
    *) echo "DATA_DIR must be an absolute path." >&2; exit 1 ;;
esac
for data_file in nse_holidays.txt nifty50.txt sector_fno_map.yaml; do
    if [ ! -s "`$data_dir/`$data_file" ]; then
        echo "Required verified data file is missing: `$data_dir/`$data_file" >&2
        exit 1
    fi
done
if [ ! -x "`$app/.venv/bin/python" ]; then
    echo "Missing `$app/.venv; provision the EC2 host before deploying." >&2
    exit 1
fi

mkdir -p "`$stage" "`$app"
tar -xzf "`$archive" -C "`$stage"
install -m 0644 "`$stage/deploy/algo-api.service" /etc/systemd/system/algo-api.service
install -m 0644 "`$stage/deploy/algo-engine.service" /etc/systemd/system/algo-engine.service
systemctl daemon-reload
systemctl stop algo-engine.service algo-api.service
cp -a "`$stage/." "`$app/"
"`$app/.venv/bin/python" -m pip install -e "`$app"
systemctl enable algo-api.service algo-engine.service
systemctl restart algo-api.service algo-engine.service
systemctl is-active --quiet algo-api.service
systemctl is-active --quiet algo-engine.service
"`$app/.venv/bin/python" -c 'from urllib.request import urlopen; response = urlopen("http://127.0.0.1:8000/healthz", timeout=10); assert response.status == 200'
echo "Deployment succeeded. API health check passed; inspect engine logs and the UI health strip."
"@

        $encodedScript = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remoteScript))
        $remoteCommand = "printf '%s' '$encodedScript' | sudo -n bash"
        & $ssh.Source -i $KeyFile -o BatchMode=yes $target $remoteCommand
        if ($LASTEXITCODE -ne 0) {
            throw "Remote deployment failed. Check the SSH output and the EC2 systemd logs."
        }
    }
}
finally {
    if (Test-Path -LiteralPath $archivePath) {
        [System.IO.File]::Delete($archivePath)
    }
}
