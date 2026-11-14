from __future__ import annotations

import copy
import sqlite3
import unittest
from pathlib import Path

import yaml

from nsebot import logstore
from nsebot.rules import RulesValidationError, load_active, load_scheduled, save_new_version, validate
from nsebot.types import Bar, Bias, Reason, Side, State, Tick


ROOT = Path(__file__).resolve().parents[1]


class SharedTypesTests(unittest.TestCase):
    def test_domain_enums_and_market_data_types(self) -> None:
        self.assertEqual(Side.LONG_CALL.value, "LONG_CALL")
        self.assertEqual(Bias.SIDEWAYS.value, "SIDEWAYS")
        self.assertEqual(State.HALTED.value, "HALTED")
        self.assertEqual(Reason.RISK_CAP.value, "risk_cap")
        self.assertTrue(Bar.__dataclass_params__.frozen)
        self.assertTrue(Tick.__dataclass_params__.frozen)


class RulesTests(unittest.TestCase):
    def setUp(self) -> None:
        with (ROOT / "config" / "default_rules.yaml").open(encoding="utf-8") as stream:
            self.document = yaml.safe_load(stream)

    def test_default_rules_load_and_convert_float_values_to_decimal(self) -> None:
        rules = validate(self.document)
        self.assertEqual(rules.risk.trading_mode, "paper")
        self.assertEqual(rules.screener.shortlist_size, 5)
        self.assertEqual(str(rules.universe.max_bid_ask_pct_of_mid), "1.5")
        self.assertEqual(rules.sets.active, ("orb_vwap",))

    def test_rules_reject_missing_unknown_and_wrongly_typed_values(self) -> None:
        missing = copy.deepcopy(self.document)
        del missing["risk"]["max_lots"]
        with self.assertRaises(RulesValidationError):
            validate(missing)

        unknown = copy.deepcopy(self.document)
        unknown["risk"]["allow_short"] = False
        with self.assertRaises(RulesValidationError):
            validate(unknown)

        wrong_type = copy.deepcopy(self.document)
        wrong_type["calendar"]["skip_weekends"] = 1
        with self.assertRaises(RulesValidationError):
            validate(wrong_type)

    def test_rules_reject_unsupported_enum(self) -> None:
        invalid = copy.deepcopy(self.document)
        invalid["risk"]["trading_mode"] = "automatic"
        with self.assertRaises(RulesValidationError):
            validate(invalid)

    def test_version_store_seeds_and_schedules_without_mutating_previous_version(self) -> None:
        conn = sqlite3.connect(":memory:")
        rules = load_active(conn)
        self.assertEqual(rules.version, 1)
        updated = copy.deepcopy(self.document)
        updated["risk"]["max_lots"] = 2
        new_version = save_new_version(conn, updated, "test change")
        self.assertEqual(new_version, 2)
        self.assertEqual(load_scheduled(conn).risk.max_lots, 2)
        self.assertEqual(load_active(conn).risk.max_lots, 2)
        statuses = conn.execute(
            "SELECT status FROM rules_versions ORDER BY id"
        ).fetchall()
        self.assertEqual(statuses, [("retired",), ("scheduled",)])
        conn.close()


class LogstoreTests(unittest.TestCase):
    def test_schema_initializes_idempotently_and_trade_summary_separates_modes(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        logstore.initialize(conn)
        logstore.initialize(conn)
        rows = [
            ("2026-11-12", "paper", "100", "80", 10, "20", "2", "18"),
            ("2026-11-13", "live", "100", "80", 10, "20", "2", "18"),
            ("2026-11-14", "paper", "100", "80", 10, "-20", "2", "-22"),
        ]
        for day, mode, entry, stop, qty, gross, costs, net in rows:
            conn.execute(
                """
                INSERT INTO trades (
                  day, mode, underlying, symbol, right, strike, expiry, qty,
                  entry_price, stop_trigger, target, gross_pnl, costs, net_pnl,
                  rules_version, set_id
                ) VALUES (?, ?, 'INFY', 'INFYCE', 'CE', '100', '2026-12-29',
                          ?, ?, ?, '160', ?, ?, ?, 1, 'orb_vwap')
                """,
                (day, mode, qty, entry, stop, gross, costs, net),
            )
        conn.commit()
        result = logstore.trades_summary(conn, mode="paper")
        self.assertEqual(result["summary"]["total_trades"], 2)
        self.assertEqual(result["summary"]["wins"], 1)
        self.assertEqual(result["summary"]["losses"], 1)
        self.assertEqual(result["summary"]["net_pnl"], "-4")
        self.assertEqual(result["summary"]["avg_r"], "-0.01")
        self.assertEqual(len(result["trades"]), 2)
        self.assertEqual(logstore.trades_summary(conn, mode="live")["summary"]["total_trades"], 1)
        conn.close()

    def test_trade_summary_rejects_bad_filter_and_limit(self) -> None:
        conn = sqlite3.connect(":memory:")
        logstore.initialize(conn)
        with self.assertRaises(ValueError):
            logstore.trades_summary(conn, mode="unknown")
        with self.assertRaises(ValueError):
            logstore.trades_summary(conn, limit=0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
