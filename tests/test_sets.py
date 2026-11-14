from __future__ import annotations

import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from nsebot.rules import validate
from nsebot.sets import SetContext, SetEval, evaluate
from nsebot.types import Bar, Bias, Side


ROOT = Path(__file__).resolve().parents[1]
IST = ZoneInfo("Asia/Kolkata")


def load_rules():
    with (ROOT / "config" / "default_rules.yaml").open(encoding="utf-8") as stream:
        return validate(yaml.safe_load(stream))


class OrbVwapTests(unittest.TestCase):
    def test_one_minute_breakout_arms_without_order(self) -> None:
        bar = Bar(
            7, "1m",
            datetime(2026, 11, 16, 10, 0, tzinfo=IST),
            datetime(2026, 11, 16, 10, 1, tzinfo=IST),
            Decimal("108"), Decimal("111"), Decimal("107"), Decimal("110"),
            130, "historical",
        )
        context = SetContext(
            bias=Bias.BULLISH, veto=None, now=bar.end, rules=load_rules(),
            or_high=Decimal("109"), or_low=Decimal("95"), vwap=Decimal("105"),
            ema9_5m=Decimal("110"), ema21_5m=Decimal("108"),
            closed_1m=bar, closed_5m=None, prev_1m_volumes=(100,) * 20,
            rsi_1m=Decimal("60"), rsi_5m=None, armed=False,
            armed_bar_5m_index=None, closed_5m_index=2, side=None,
        )
        self.assertEqual(evaluate("orb_vwap", context), SetEval.ARM)

    def test_five_minute_confirmation_and_vwap_drop(self) -> None:
        bar = Bar(
            7, "5m",
            datetime(2026, 11, 16, 10, 0, tzinfo=IST),
            datetime(2026, 11, 16, 10, 5, tzinfo=IST),
            Decimal("109"), Decimal("112"), Decimal("108"), Decimal("111"),
            500, "historical",
        )
        context = SetContext(
            bias=Bias.BULLISH, veto=None, now=bar.end, rules=load_rules(),
            or_high=Decimal("109"), or_low=Decimal("95"), vwap=Decimal("105"),
            ema9_5m=Decimal("110"), ema21_5m=Decimal("108"),
            closed_1m=None, closed_5m=bar, prev_1m_volumes=(100,) * 20,
            rsi_1m=Decimal("60"), rsi_5m=Decimal("60"), armed=True,
            armed_bar_5m_index=1, closed_5m_index=2, side=Side.LONG_CALL,
        )
        self.assertEqual(evaluate("orb_vwap", context), SetEval.CONFIRM)
        lost_vwap = Bar(
            7, "5m", bar.start, bar.end, Decimal("106"), Decimal("108"),
            Decimal("103"), Decimal("104"), 500, "historical",
        )
        self.assertEqual(
            evaluate("orb_vwap", SetContext(**{**context.__dict__, "closed_5m": lost_vwap})),
            SetEval.DROP,
        )

    def test_vwap_pullback_requires_directional_confirmation_body(self) -> None:
        bar = Bar(
            7, "5m",
            datetime(2026, 11, 16, 10, 0, tzinfo=IST),
            datetime(2026, 11, 16, 10, 5, tzinfo=IST),
            Decimal("110"), Decimal("111"), Decimal("106"), Decimal("109"),
            500, "historical",
        )
        context = SetContext(
            bias=Bias.BULLISH, veto=None, now=bar.end, rules=load_rules(),
            or_high=Decimal("109"), or_low=Decimal("95"), vwap=Decimal("107"),
            ema9_5m=Decimal("108"), ema21_5m=Decimal("106"),
            closed_1m=None, closed_5m=bar, prev_1m_volumes=(100,) * 20,
            rsi_1m=Decimal("60"), rsi_5m=Decimal("60"), armed=True,
            armed_bar_5m_index=1, closed_5m_index=2, side=Side.LONG_CALL,
            prior_5m_outside_or=True,
        )
        self.assertEqual(evaluate("vwap_pullback", context), SetEval.DROP)
        bullish = Bar(
            7, "5m", bar.start, bar.end, Decimal("108"), Decimal("111"),
            Decimal("106"), Decimal("109"), 500, "historical",
        )
        self.assertEqual(
            evaluate("vwap_pullback", SetContext(**{**context.__dict__, "closed_5m": bullish})),
            SetEval.CONFIRM,
        )


if __name__ == "__main__":
    unittest.main()
