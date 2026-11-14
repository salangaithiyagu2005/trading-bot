from __future__ import annotations

import os
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import yaml

from nsebot.chain import NoExpiryError, liquidity_ok, select_atm, select_expiry
from nsebot.execution import Execution
from nsebot.instruments import Instrument
from nsebot.kite_gateway import Gateway, OrderError
from nsebot.paper import PaperBroker
from nsebot.risk import build_bracket
from nsebot.rules import validate
from nsebot.types import Reason


ROOT = Path(__file__).resolve().parents[1]


def load_rules():
    with (ROOT / "config" / "default_rules.yaml").open(encoding="utf-8") as stream:
        return validate(yaml.safe_load(stream))


def option(expiry: date, strike: str, right: str = "CE", name: str = "INFY") -> Instrument:
    return Instrument(1, f"{name}{strike}{right}", "NFO", "NFO-OPT", name, expiry,
                     Decimal(strike), Decimal("0.05"), 65, right)


class ChainTests(unittest.TestCase):
    def test_expiry_dte_atm_tie_and_liquidity_gates(self) -> None:
        rules = load_rules()
        options = [option(date(2026, 11, 23), "100"), option(date(2026, 12, 29), "110")]
        self.assertEqual(select_expiry(options, date(2026, 11, 18), rules), date(2026, 12, 29))
        with self.assertRaises(NoExpiryError):
            select_expiry(options[:1], date(2026, 11, 18), rules)
        tie = [option(date(2026, 12, 29), "100"), option(date(2026, 12, 29), "110")]
        self.assertEqual(select_atm(tie, date(2026, 12, 29), "CE", Decimal("105")).strike,
                         Decimal("100"))
        self.assertEqual(
            liquidity_ok(Decimal("100"), Decimal("100.5"), 75000, rules, lot_size=1000),
            Reason.LIQUIDITY_REJECT,
        )
        self.assertEqual(
            liquidity_ok(Decimal("100"), Decimal("100.5"), 100000, rules, qty=500, ask_qty=200),
            Reason.LIQUIDITY_REJECT,
        )


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.order_rows = []

    def place(self, **kwargs):
        self.calls.append(("place", kwargs))
        return str(len(self.calls))

    def modify(self, *args, **kwargs):
        self.calls.append(("modify", args, kwargs))
        return "modified"

    def cancel(self, *args, **kwargs):
        self.calls.append(("cancel", args, kwargs))

    def orders(self):
        return self.order_rows


class OrderSafetyTests(unittest.TestCase):
    def test_gateway_refuses_modify_without_live_environment(self) -> None:
        fake = FakeGateway()
        gateway = Gateway("key", "token", kite_client=fake)
        with patch.dict(os.environ, {"LIVE_TRADING": "false"}):
            with self.assertRaises(OrderError):
                gateway.modify("123", price="1.00")
        self.assertEqual(fake.calls, [])

    def test_execution_uses_sl_and_waits_for_terminal_cancel(self) -> None:
        fake = FakeGateway()
        execution = Execution(fake)
        instrument = option(date(2026, 12, 29), "100")
        bracket = build_bracket(Decimal("100"), Decimal("0.05"), 65, load_rules())
        stop, target = execution.protective_orders(instrument, bracket, "D2026111601")
        self.assertEqual(fake.calls[0][1]["order_type"], "SL")
        self.assertEqual(fake.calls[0][1]["price"], "67.90")
        self.assertEqual(fake.calls[1][1]["order_type"], "LIMIT")
        fake.order_rows = [{"order_id": stop, "status": "CANCELLED", "filled_qty": 0}]
        result = execution.cancel_and_wait(stop)
        self.assertEqual(result["status"], "CANCELLED")
        self.assertNotEqual(stop, target)

    def test_paper_stop_does_not_fill_below_limit_floor(self) -> None:
        broker = PaperBroker()
        bracket = build_bracket(Decimal("100"), Decimal("0.05"), 65, load_rules())
        now = datetime(2026, 11, 16, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        broker.enter("INFYCE", 65, Decimal("100"), bracket, Decimal("0.05"), now)
        self.assertIsNone(broker.on_tick(Decimal("70"), Decimal("67.80"), now))
        fill = broker.on_tick(Decimal("70"), Decimal("68.00"), now)
        self.assertEqual(fill.price, Decimal("68.00"))


if __name__ == "__main__":
    unittest.main()
