from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient

from nsebot.api.app import create_app


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env = patch.dict(os.environ, {
            "DATA_DIR": str(self.root / "runtime"),
            "REPO_DATA_DIR": str(self.root / "empty-seed"),
            "UI_PASSWORD": "test-password",
            "LIVE_TRADING": "false",
        })
        self.env.start()
        self.client = TestClient(create_app())
        self.client.__enter__()
        self.auth = ("operator", "test-password")

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.env.stop()
        self.temporary.cleanup()

    def test_operator_page_loads_linked_stylesheet_and_script(self) -> None:
        page = self.client.get("/", auth=self.auth)
        self.assertEqual(page.status_code, 200)
        self.assertIn('href="/static/styles.css"', page.text)
        self.assertIn('src="/static/app.js"', page.text)

        styles = self.client.get("/static/styles.css", auth=self.auth)
        script = self.client.get("/static/app.js", auth=self.auth)
        self.assertEqual(styles.status_code, 200)
        self.assertEqual(script.status_code, 200)
        self.assertIn("text/css", styles.headers["content-type"])
        self.assertIn("javascript", script.headers["content-type"])

    def test_api_reads_settings_locally_and_saves_direct_token(self) -> None:
        settings = self.client.get("/api/settings", auth=self.auth)
        self.assertEqual(settings.status_code, 200)
        self.assertEqual(settings.json()["token_status"]["status"], "MISSING")
        self.assertNotIn("KITE_API_SECRET", settings.json()["credentials"].values())

        saved = self.client.post(
            "/api/token",
            json={"mode": "access_token", "token": "local-access", "user_id": "operator"},
            auth=self.auth,
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["token_status"]["status"], "PRESENT")
        token_path = self.root / "runtime" / "token.json"
        token = json.loads(token_path.read_text(encoding="utf-8"))
        self.assertEqual(token["access_token"], "local-access")
        self.assertEqual(token["user_id"], "operator")
        conn = sqlite3_connect(self.root / "runtime" / "algo.db")
        command = conn.execute(
            "SELECT name FROM commands ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertEqual(command[0], "RELOAD_TOKEN")
        verify = self.client.post("/api/token/verify", auth=self.auth)
        self.assertEqual(verify.status_code, 200)
        conn = sqlite3_connect(self.root / "runtime" / "algo.db")
        command = conn.execute(
            "SELECT name FROM commands ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertEqual(command[0], "VERIFY_TOKEN")

    def test_api_reports_stale_engine_status_without_broker_calls(self) -> None:
        import sqlite3
        from nsebot.clock import now_ist

        conn = sqlite3.connect(self.root / "runtime" / "algo.db")
        logstore_status = {
            "heartbeat_at": (now_ist() - timedelta(seconds=20)).isoformat(),
            "state": "IDLE",
            "token_verify_status": "ok",
        }
        from nsebot.logstore import upsert_engine_status
        upsert_engine_status(conn, logstore_status)
        conn.close()
        result = self.client.get("/api/session", auth=self.auth)
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()["health"]["engine_alive"])

    def test_live_mode_and_locked_credentials_are_rejected(self) -> None:
        response = self.client.post(
            "/api/mode", json={"mode": "live", "confirm": True}, auth=self.auth
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "live_execution_not_implemented")
        response = self.client.post(
            "/api/config/credentials", json={"LIVE_TRADING": "true"}, auth=self.auth
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.get("/api/settings")
        self.assertEqual(response.status_code, 401)

    def test_api_startup_requires_ui_password(self) -> None:
        with patch.dict(os.environ, {"UI_PASSWORD": ""}):
            with self.assertRaisesRegex(RuntimeError, "UI_PASSWORD"):
                with TestClient(create_app()):
                    pass


def sqlite3_connect(path: Path):
    import sqlite3
    return sqlite3.connect(path)


if __name__ == "__main__":
    unittest.main()
