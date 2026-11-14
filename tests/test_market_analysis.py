from __future__ import annotations

import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from nsebot.bias import BiasObs, compute_bias
from nsebot.candles import CandleBuilder, discard_open_historical
from nsebot.indicators import atr_wilder, ema, rsi_wilder, volume_sma, vwap
from nsebot.risk import build_bracket, costs, entry_limit, stop_limit_price
from nsebot.rules import validate
from nsebot.screener import PriceSnap, screen_stocks
from nsebot.types import Bar, Bias, Reason, Tick


ROOT = Path(__file__).resolve().parents[1]
IST = ZoneInfo("Asia/Kolkata")


def load_rules():
    with (ROOT / "config" / "default_rules.yaml").open(encoding="utf-8") as stream:
        return validate(yaml.safe_load(stream))


def dec(value: str) -> Decimal:
    return Decimal(value)


class CandleTests(unittest.TestCase):
    def test_tick_deltas_form_candle_and_new_bucket_closes_previous(self) -> None:
        builder = CandleBuilder(7, 1)
        first = Tick(
            7, datetime(2026, 11, 16, 9, 15, tzinfo=IST), dec("100"), 100,
            day_open=dec("99"),
        )
        self.assertEqual(first.day_open, dec("99"))
        second = Tick(7, datetime(2026, 11, 16, 9, 15, 30, tzinfo=IST), dec("102"), 120)
        next_minute = Tick(7, datetime(2026, 11, 16, 9, 16, tzinfo=IST), dec("101"), 130)
        self.assertEqual(builder.on_tick(first), [])
        self.assertEqual(builder.on_tick(second), [])
        closed = builder.on_tick(next_minute)
        self.assertEqual(len(closed), 1)
        self.assertEqual((closed[0].open, closed[0].high, closed[0].low, closed[0].close),
                         (dec("100"), dec("102"), dec("100"), dec("102")))
        self.assertEqual(closed[0].volume, 120)
        self.assertEqual(builder.open_bar().volume, 10)

    def test_timer_grace_and_historical_open_bar_filter(self) -> None:
        builder = CandleBuilder(7, 1)
        builder.on_tick(Tick(7, datetime(2026, 11, 16, 9, 15, tzinfo=IST), dec("100"), 1))
        self.assertEqual(builder.on_timer(datetime(2026, 11, 16, 9, 16, 0, 499000, tzinfo=IST)), [])
        self.assertEqual(len(builder.on_timer(datetime(2026, 11, 16, 9, 16, 0, 500000, tzinfo=IST))), 1)

        completed = Bar(7, "1m", datetime(2026, 11, 16, 9, 15, tzinfo=IST),
                        datetime(2026, 11, 16, 9, 16, tzinfo=IST),
                        dec("1"), dec("1"), dec("1"), dec("1"), 1, "historical")
        incomplete = Bar(7, "1m", datetime(2026, 11, 16, 9, 16, tzinfo=IST),
                         datetime(2026, 11, 16, 9, 17, tzinfo=IST),
                         dec("1"), dec("1"), dec("1"), dec("1"), 1, "historical")
        self.assertEqual(discard_open_historical(
            [completed, incomplete], datetime(2026, 11, 16, 9, 16, 30, tzinfo=IST)
        ), [completed])


class IndicatorTests(unittest.TestCase):
    def test_indicator_values_and_zero_volume_vwap(self) -> None:
        values = [dec(str(value)) for value in (1, 2, 3, 4)]
        self.assertEqual(ema(values, 3), dec("3"))
        self.assertEqual(rsi_wilder(values, 2), dec("100"))
        self.assertEqual(volume_sma([10, 20, 30], 2), dec("25"))
        bar = Bar(1, "1m", datetime(2026, 11, 16, 9, 15, tzinfo=IST),
                  datetime(2026, 11, 16, 9, 16, tzinfo=IST),
                  dec("10"), dec("12"), dec("9"), dec("11"), 0, "tick")
        self.assertIsNone(vwap([bar]))
        self.assertIsNone(atr_wilder([bar], 1))


class BiasTests(unittest.TestCase):
    def test_spec_bullish_fixture_and_veto(self) -> None:
        rules = load_rules()
        obs = BiasObs(
            nifty_price=dec("24580"), nifty_prev_close=dec("24500"), nifty_vwap=dec("24520"),
            or_low=dec("24490"), or_high=dec("24560"), or_open=dec("24480"), or_close=dec("24550"),
            close_5m=dec("24570"), ema20_5m=dec("24540"), ema9_5m=dec("24570"),
            ema21_5m=dec("24530"), breadth_above_vwap=33, breadth_total=50,
            bank_price=dec("52000"), bank_vwap=dec("51900"), bank_or_mid=dec("51850"),
            vix_price=dec("13.0"), vix_prev_close=dec("12.8"), atr15=dec("40"),
            inside_or=False,
        )
        result = compute_bias(obs, rules)
        self.assertEqual(result.bias, Bias.BULLISH)
        self.assertEqual(result.score, 11)
        self.assertEqual([line.points for line in result.lines], [2, 2, 2, 2, 1, 2])
        self.assertIsNone(result.veto)

        vetoed = compute_bias(
            BiasObs(**{**obs.__dict__, "vix_price": dec("14.0"), "inside_or": True}),
            rules,
        )
        self.assertEqual(vetoed.veto, Reason.VIX_UNSTABLE)
        bank_disagreement = compute_bias(
            BiasObs(**{**obs.__dict__, "bank_price": dec("51800")}), rules
        )
        self.assertEqual(bank_disagreement.bias, Bias.SIDEWAYS)
        self.assertLess(bank_disagreement.lines[-1].points, 0)
        opposing_open_range = compute_bias(
            BiasObs(**{**obs.__dict__, "or_open": dec("24555")}), rules
        )
        self.assertEqual(opposing_open_range.lines[2].points, 0)


class ScreenerTests(unittest.TestCase):
    def test_winning_sectors_top_pool_and_anti_chase(self) -> None:
        rules = load_rules()
        nifty = PriceSnap(1, "NIFTY 50", dec("100.4"), dec("100"), dec("100"), 0)
        sectors = [
            PriceSnap(2, "NIFTY IT", dec("203.2"), dec("200"), dec("200"), 0),
            PriceSnap(3, "NIFTY AUTO", dec("101.1"), dec("100"), dec("100"), 0),
            PriceSnap(4, "NIFTY FMCG", dec("99.7"), dec("100"), dec("100"), 0),
        ]
        stocks = [
            PriceSnap(10, "CHASE", dec("105.2"), dec("100"), dec("100"), 1000),
            PriceSnap(11, "GOOD", dec("101.8"), dec("100"), dec("100"), 1000),
            PriceSnap(12, "OTHER", dec("102"), dec("100"), dec("100"), 1000),
        ]
        result = screen_stocks(
            Bias.BULLISH, nifty, sectors, stocks,
            {"CHASE": ("NIFTY IT",), "GOOD": ("NIFTY IT",), "OTHER": ("NIFTY FMCG",)},
            rules,
        )
        self.assertEqual([s.tradingsymbol for s in result.winning_sectors],
                         ["NIFTY IT", "NIFTY AUTO"])
        self.assertEqual(result.top_pool[0].tradingsymbol, "CHASE")
        self.assertFalse(result.top_pool[0].anti_chase_ok)
        self.assertEqual([item.tradingsymbol for item in result.shortlist], ["GOOD"])


class RiskTests(unittest.TestCase):
    def test_bracket_costs_and_entry_at_ask(self) -> None:
        rules = load_rules()
        bracket = build_bracket(dec("100.15"), dec("0.05"), 65, rules)
        self.assertEqual(bracket.stop_trigger, dec("70.10"))
        self.assertEqual(bracket.target, dec("160.25"))
        self.assertEqual(bracket.risk_inr, dec("1953.25"))
        self.assertEqual(entry_limit(dec("99.95"), dec("100.00"), dec("0.05")), dec("100.00"))
        self.assertEqual(stop_limit_price(dec("70.10"), dec("0.05")), dec("67.95"))
        self.assertEqual(costs(dec("100"), dec("160"), 65, rules), dec("70.10"))


if __name__ == "__main__":
    unittest.main()
