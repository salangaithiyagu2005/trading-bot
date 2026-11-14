from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from nsebot import logstore


class ConfigStoreTests(unittest.TestCase):
    def test_partial_credential_updates_preserve_values_and_reject_locked_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self.assertTrue(logstore.save_credentials(data_dir, {
                "KITE_API_KEY": "key-one",
                "KITE_API_SECRET": "secret-one",
            }))
            self.assertFalse(logstore.save_credentials(data_dir, {"XAI_API_KEY": "xai-one"}))
            stored = json.loads((data_dir / "credentials.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["KITE_API_KEY"], "key-one")
            self.assertEqual(stored["KITE_API_SECRET"], "secret-one")
            self.assertEqual(stored["XAI_API_KEY"], "xai-one")
            with self.assertRaises(ValueError):
                logstore.save_credentials(data_dir, {"LIVE_TRADING": "true"})
            self.assertEqual(logstore.mask_secret("ab1234cd"), "ab****cd")

    def test_data_file_validation_and_seed_does_not_overwrite_user_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "repo"
            target = root / "runtime"
            source.mkdir()
            (source / "nse_holidays.txt").write_text("# source\n2026-01-26\n", encoding="utf-8")
            logstore.seed_data_files(source, target)
            self.assertIn("2026-01-26", (target / "nse_holidays.txt").read_text(encoding="utf-8"))
            (target / "nse_holidays.txt").write_text("2026-02-01\n", encoding="utf-8")
            logstore.seed_data_files(source, target)
            self.assertEqual((target / "nse_holidays.txt").read_text(encoding="utf-8"), "2026-02-01\n")
            with self.assertRaises(ValueError):
                logstore.validate_and_save_data_file(target, "nse_holidays.txt", "not-a-date\n")
            with self.assertRaises(ValueError):
                logstore.validate_and_save_data_file(target, "nifty50.txt", "INFY\n")
            logstore.validate_and_save_data_file(target, "nse_holidays.txt", "# official\n2026-01-26\n")


if __name__ == "__main__":
    unittest.main()
