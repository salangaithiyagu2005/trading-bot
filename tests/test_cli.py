from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import patch

from nsebot import cli


class _Process:
    def __init__(self, return_code: int | None = None) -> None:
        self.return_code = return_code
        self.terminated = False

    def poll(self) -> int | None:
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True
        self.return_code = -1

    def wait(self, timeout: int | None = None) -> int:
        return self.return_code or 0

    def kill(self) -> None:
        self.return_code = -9


class LauncherTests(unittest.TestCase):
    def test_launcher_starts_both_processes_and_stops_peer_on_exit(self) -> None:
        api = _Process()
        engine = _Process(return_code=3)
        with (
            patch.object(cli, "_configure_environment"),
            patch.object(
                cli, "_entrypoint", side_effect=[Path("nsebot-api"), Path("nsebot-engine")]
            ),
            patch.object(cli.subprocess, "Popen", side_effect=[api, engine]) as popen,
            patch.object(cli.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as exit_error,
        ):
            cli.main()

        self.assertEqual(exit_error.exception.code, 3)
        self.assertEqual(popen.call_count, 2)
        self.assertTrue(api.terminated)
