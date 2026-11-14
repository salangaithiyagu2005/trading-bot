from __future__ import annotations

import os
import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import yaml

from nsebot import logstore
from nsebot.candles import CandleBuilder
from nsebot.engine import Engine
from nsebot.instruments import Instrument
from nsebot.rules import load_scheduled, save_new_version
from nsebot.types import Bar, Reason, Side, State, Tick

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 16, 9, 20, tzinfo=IST)


class FakeGateway:
    def __init__(self, api_key, access_token):
        self.api_key = api_key
        self.access_token = access_token
        self.closed = False
        self.profile_calls = 0
        self.margin_calls = 0
        self.subscribed = []

    def close(self):
        self.closed = True

    def profile(self):
        self.profile_calls += 1
        return {"user_id": "user"}

    def margins_equity(self):
        self.margin_calls += 1
        return {"available": {"live_balance": 12345}}

    def quote(self, keys):
        return {
            key: {
                "last_price": 100,
                "oi": 100000,
                "depth": {
                    "buy": [{"price": 99, "quantity": 1000}],
                    "sell": [{"price": 100, "quantity": 1000}],
                },
            }
            for key in keys
        }

    def subscribe(self, tokens, mode):
        self.subscribed.extend(tokens)

    def place(self, **_params):
        raise AssertionError("paper mode must never place a broker order")


class EngineSafetyTests(unittest.TestCase):
    def _engine(self, data_dir: Path, gateway_factory=FakeGateway) -> Engine:
        seed = data_dir / "seed"
        seed.mkdir(exist_ok=True)
        return Engine(
            data_dir=data_dir,
            repo_data_dir=seed,
            gateway_factory=gateway_factory,
            clock=lambda: NOW,
            sleep=lambda _delay: None,
        )

    def _open_paper_position(self, engine: Engine) -> tuple[FakeGateway, Instrument]:
        gateway = FakeGateway("key", "token")
        engine.gateway = gateway
        engine.state = State.ARMED
        engine.user_armed = True
        stock = Instrument(
            7, "ABC", "NSE", "NSE", "ABC", None, None,
            Decimal("0.05"), 1, "EQ",
        )
        option = Instrument(
            8, "ABC26DEC100CE", "NFO", "NFO-OPT", "ABC",
            datetime(2026, 12, 31).date(), Decimal("100"),
            Decimal("0.05"), 65, "CE",
        )
        engine.instruments = [stock, option]
        engine.latest_prices[stock.token] = Decimal("100")
        state = {"armed": True, "armed_bar_5m_index": 1, "side": Side.LONG_CALL}
        engine._confirm_stock_setup(stock, "NIFTY IT", "orb_vwap", state)
        return gateway, option

    def test_engine_fails_closed_when_exchange_calendar_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"LIVE_TRADING": "false"}):
            engine = self._engine(Path(temporary))
            self.assertEqual(engine.state, State.BLOCKED)
            self.assertEqual(engine._block_reason(), "health_fail")
            engine.close()

    def test_live_rules_block_when_server_live_lock_is_false(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"LIVE_TRADING": "false"}):
            data_dir = Path(temporary)
            conn = logstore.connect(data_dir)
            document = yaml.safe_load(
                (Path(__file__).resolve().parents[1] / "config" / "default_rules.yaml")
                .read_text(encoding="utf-8")
            )
            document["risk"]["trading_mode"] = "live"
            save_new_version(conn, document, "test")
            conn.close()
            (data_dir / "nse_holidays.txt").write_text("2026-01-26\n", encoding="utf-8")
            engine = self._engine(data_dir)
            self.assertEqual(engine.state, State.BLOCKED)
            self.assertEqual(engine._block_reason(), "health_fail")
            engine.close()

    def test_live_rules_block_even_when_env_allows_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"LIVE_TRADING": "true"}):
            data_dir = Path(temporary)
            conn = logstore.connect(data_dir)
            document = yaml.safe_load(
                (Path(__file__).resolve().parents[1] / "config" / "default_rules.yaml")
                .read_text(encoding="utf-8")
            )
            document["risk"]["trading_mode"] = "live"
            save_new_version(conn, document, "test")
            conn.close()
            (data_dir / "nse_holidays.txt").write_text("2026-01-26\n", encoding="utf-8")
            engine = self._engine(data_dir)
            self.assertEqual(engine.state, State.BLOCKED)
            event = engine.conn.execute(
                "SELECT reason_text FROM events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertIn("live order lifecycle is not implemented", event["reason_text"])
            engine.close()

    def test_valid_token_reload_recovers_no_token_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"LIVE_TRADING": "false"}):
            data_dir = Path(temporary)
            (data_dir / "nse_holidays.txt").write_text("2026-01-26\n", encoding="utf-8")
            logstore.save_credentials(data_dir, {"KITE_API_KEY": "test-key"})
            engine = self._engine(data_dir)
            self.assertEqual(engine.state, State.BLOCKED)
            logstore._atomic_write(
                data_dir / "token.json",
                b'{"access_token":"token","user_id":"user","created_at":"2026-11-16T09:00:00+05:30"}',
                0o600,
            )
            logstore.enqueue_command(engine.conn, "RELOAD_TOKEN")
            self.assertTrue(engine._process_one_command())
            self.assertEqual(engine.state, State.IDLE)
            self.assertIsNotNone(engine.gateway)
            engine.close()

    def test_step_writes_heartbeat_and_verify_command_checks_broker_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"LIVE_TRADING": "false"}):
            data_dir = Path(temporary)
            (data_dir / "nse_holidays.txt").write_text("2026-01-26\n", encoding="utf-8")
            logstore.save_credentials(data_dir, {"KITE_API_KEY": "test-key"})
            logstore._atomic_write(
                data_dir / "token.json",
                b'{"access_token":"token","user_id":"user","created_at":"2026-11-16T08:30:00+05:30"}',
                0o600,
            )
            engine = self._engine(data_dir)
            engine._startup_verified = True
            logstore.enqueue_command(engine.conn, "VERIFY_TOKEN")
            asyncio.run(engine.step())
            self.assertEqual(engine.token_verify_status, "ok")
            self.assertEqual(engine.gateway.profile_calls, 1)
            self.assertEqual(engine.gateway.margin_calls, 1)
            status = logstore.load_engine_status(engine.conn)
            self.assertEqual(status["state"], engine.state.value)
            self.assertEqual(status["available_margin_inr"], "12345")
            engine.close()

    def test_early_arm_requires_verified_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"LIVE_TRADING": "false"}):
            data_dir = Path(temporary)
            (data_dir / "nse_holidays.txt").write_text("2026-01-26\n", encoding="utf-8")
            logstore.save_credentials(data_dir, {"KITE_API_KEY": "test-key"})
            logstore._atomic_write(
                data_dir / "token.json",
                b'{"access_token":"token","user_id":"user","created_at":"2026-11-16T08:30:00+05:30"}',
                0o600,
            )
            engine = self._engine(data_dir)
            logstore.enqueue_command(engine.conn, "ARM")
            asyncio.run(engine.step())
            self.assertTrue(engine.user_armed)
            engine.close()

    def test_done_state_rolls_over_to_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"LIVE_TRADING": "false"}):
            data_dir = Path(temporary)
            (data_dir / "nse_holidays.txt").write_text("2026-01-26\n", encoding="utf-8")
            logstore.save_credentials(data_dir, {"KITE_API_KEY": "test-key"})
            logstore._atomic_write(
                data_dir / "token.json",
                b'{"access_token":"token","user_id":"user","created_at":"2026-11-16T08:30:00+05:30"}',
                0o600,
            )
            current = [datetime(2026, 11, 13, 16, 0, tzinfo=IST)]
            engine = Engine(
                data_dir=data_dir,
                repo_data_dir=data_dir / "seed",
                gateway_factory=FakeGateway,
                clock=lambda: current[0],
            )
            engine.conn.execute(
                "UPDATE sessions SET state='DONE' WHERE day='2026-11-13'"
            )
            engine.conn.commit()
            engine.state = State.DONE
            current[0] = datetime(2026, 11, 16, 8, 45, tzinfo=IST)
            asyncio.run(engine.step())
            self.assertEqual(engine.active_day.isoformat(), "2026-11-16")
            self.assertEqual(engine.state, State.IDLE)
            self.assertIsNotNone(engine.conn.execute(
                "SELECT 1 FROM sessions WHERE day='2026-11-16'"
            ).fetchone())
            engine.close()

    def test_opening_range_requires_all_fifteen_unique_bars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"LIVE_TRADING": "false"}):
            engine = self._engine(Path(temporary))
            instrument = Instrument(
                7, "NIFTY", "NFO", "NFO-FUT", "NIFTY", None, None,
                Decimal("0.05"), 50, "FUT",
            )
            engine.history[(7, "1m")] = [
                Bar(
                    7, "1m",
                    datetime(2026, 11, 16, 9, 15, tzinfo=IST) + timedelta(minutes=index),
                    datetime(2026, 11, 16, 9, 16, tzinfo=IST) + timedelta(minutes=index),
                    Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"),
                    10, "historical",
                )
                for index in range(15)
            ]
            self.assertIsNotNone(engine._opening_range(instrument, require_all=True))
            engine.history[(7, "1m")].pop(7)
            self.assertIsNone(engine._opening_range(instrument, require_all=True))
            engine.close()

    def test_tick_day_open_seeds_session_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"LIVE_TRADING": "false"}):
            engine = self._engine(Path(temporary))
            engine.builders[(7, 1)] = CandleBuilder(7, 1)
            engine._process_ticks([Tick(
                7, datetime(2026, 11, 16, 9, 15, tzinfo=IST),
                Decimal("101"), 20, day_open=Decimal("100"),
            )])
            self.assertEqual(engine.open_0915[7], Decimal("100"))
            engine.close()

    def test_paper_confirmation_records_and_closes_trade_without_broker_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"LIVE_TRADING": "false"}):
            engine = self._engine(Path(temporary))
            gateway, option = self._open_paper_position(engine)
            self.assertEqual(engine.state, State.MANAGING)
            self.assertEqual(gateway.subscribed, [option.token])
            trade = engine.conn.execute(
                "SELECT mode, symbol, entry_price, net_pnl FROM trades WHERE day=?",
                (engine.active_day.isoformat(),),
            ).fetchone()
            self.assertEqual(trade["mode"], "paper")
            self.assertEqual(trade["symbol"], option.tradingsymbol)
            self.assertIsNone(trade["net_pnl"])

            engine._manage_paper_tick(Tick(
                option.token, NOW, Decimal("161"), 1, bid=Decimal("160.5"),
            ))
            closed = engine.conn.execute(
                "SELECT exit_price, exit_reason, net_pnl FROM trades WHERE day=?",
                (engine.active_day.isoformat(),),
            ).fetchone()
            self.assertEqual(engine.state, State.DONE)
            self.assertEqual(closed["exit_reason"], "target_hit")
            self.assertIsNotNone(closed["net_pnl"])
            event_count = engine.conn.execute(
                "SELECT COUNT(*) FROM events WHERE day=? AND reason='target_hit'",
                (engine.active_day.isoformat(),),
            ).fetchone()[0]
            self.assertEqual(event_count, 1)
            engine.close()

    def test_paper_stop_watchdog_steps_unfilled_stop_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"LIVE_TRADING": "false"}):
            engine = self._engine(Path(temporary))
            self._open_paper_position(engine)
            position = engine.paper_broker.position
            self.assertIsNotNone(position)
            original_floor = position.stop_limit_floor
            engine._manage_paper_price(
                Decimal("60"), Decimal("59"), NOW,
            )
            self.assertTrue(position.is_open)
            engine._manage_paper_price(
                Decimal("60"), Decimal("59"), NOW + timedelta(seconds=2),
            )
            self.assertTrue(position.is_open)
            self.assertLess(position.stop_limit_floor, original_floor)
            engine.close()

    def test_paper_rollover_flatten_records_one_event_without_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"LIVE_TRADING": "false"}):
            engine = self._engine(Path(temporary))
            self._open_paper_position(engine)
            engine.active_option_last = Decimal("120")
            engine.active_option_bid = Decimal("119")
            engine.active_option_last_seen = NOW
            self.assertTrue(engine._exit_paper_position(Reason.TIME_FLATTEN, transition=False))
            self.assertEqual(engine.state, State.MANAGING)
            event_count = engine.conn.execute(
                "SELECT COUNT(*) FROM events WHERE day=? AND reason='time_flatten'",
                (engine.active_day.isoformat(),),
            ).fetchone()[0]
            self.assertEqual(event_count, 1)
            engine.close()


if __name__ == "__main__":
    unittest.main()
