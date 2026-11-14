from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

from nsebot.logstore import load_credentials

logger = logging.getLogger(__name__)


def notify(text: str, webhook_url: str | None = None, data_dir: Path | None = None) -> None:
    if not text:
        raise ValueError("alert text must not be empty")
    url = webhook_url
    if not url and data_dir is not None:
        url = load_credentials(data_dir).get("ALERT_WEBHOOK")
    if not url:
        if data_dir is None:
            raise ValueError("data_dir is required when no webhook is configured")
        path = data_dir / "alerts.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(text.replace("\r", " ").replace("\n", " ") + "\n")
        return
    request = urllib.request.Request(
        url,
        data=json.dumps({"text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status >= 400:
                logger.warning("Alert webhook returned HTTP %s", response.status)
    except (urllib.error.URLError, TimeoutError) as exc:
        logger.warning("Alert webhook delivery failed: %s", exc)
