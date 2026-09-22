from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from bridges.dashboard_holdings import (
    load_dashboard_holdings,
    order_dashboard_tickers_for_display,
    persist_dashboard_universe,
)


class DashboardHoldingsRuntimeContractTests(unittest.TestCase):
    def _root(self, temp: str) -> Path:
        root = Path(temp) / "market-ai"
        (root / "db").mkdir(parents=True)
        return root

    def _portfolio(self, root: Path, payload: dict) -> Path:
        path = root.parent / "investment-dashboard" / "data" / "portfolio.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_portfolio_is_authoritative_for_dynamic_bootstrap_and_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._root(temp)
            self._portfolio(
                root,
                {
                    "securities": [
                        {"ticker": "005930", "name": "삼성전자", "type": "개별주식", "qty": 10},
                        {"ticker": "123456", "name": "신규종목", "type": "ETF", "qty": 2},
                        {"ticker": "999999", "name": "매도완료", "qty": 0},
                    ],
                    "pension": [
                        {"ticker": "278530", "name": "KODEX 200TR", "qty": 3},
                        {"ticker": "123456", "name": "신규종목", "qty": 1},
                    ],
                },
            )
            loaded = load_dashboard_holdings(root)
            self.assertEqual(loaded["source"], "portfolio")
            self.assertEqual(loaded["tickers"], ("005930", "123456", "278530"))
            self.assertEqual(loaded["names"]["123456"], "신규종목")
            self.assertEqual(loaded["types"]["005930"], "개별주식")
            self.assertEqual(loaded["types"]["123456"], "ETF")
            self.assertEqual(loaded["types"]["278530"], "ETF")
            self.assertEqual(loaded["positions"]["005930"]["securities_qty"], 10.0)
            self.assertEqual(loaded["positions"]["278530"]["pension_qty"], 3.0)
            self.assertEqual(loaded["positions"]["123456"]["securities_qty"], 2.0)
            self.assertEqual(loaded["positions"]["123456"]["pension_qty"], 1.0)
            self.assertNotIn("999999", loaded["tickers"])


    def test_display_order_matches_dashboard_evaluation_rule_and_deduplicates_accounts(self):
        tickers = ["005930", "000660", "069500", "395160", "278530", "448330"]
        positions = {
            "005930": {"securities_qty": 10, "pension_qty": 0},
            "000660": {"securities_qty": 2, "pension_qty": 0},
            "069500": {"securities_qty": 100, "pension_qty": 0},
            "395160": {"securities_qty": 193, "pension_qty": 193},
            "278530": {"securities_qty": 0, "pension_qty": 662},
            "448330": {"securities_qty": 0, "pension_qty": 687},
        }
        prices = {
            "005930": 253500,
            "000660": 1759000,
            "069500": 106365,
            "395160": 38915,
            "278530": 39000,
            "448330": 17500,
        }
        names = {ticker: ticker for ticker in tickers}

        ordered = order_dashboard_tickers_for_display(tickers, positions, prices, names)

        self.assertEqual(
            ordered,
            ["069500", "395160", "000660", "005930", "278530", "448330"],
        )
        self.assertEqual(ordered.count("395160"), 1)

    def test_valid_empty_portfolio_does_not_resurrect_stale_runtime_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._root(temp)
            persist_dashboard_universe(["005930"], {"005930": "삼성전자"}, root=root)
            self._portfolio(root, {"securities": [], "pension": []})
            loaded = load_dashboard_holdings(root)
            self.assertEqual(loaded["source"], "portfolio")
            self.assertEqual(loaded["tickers"], ())
            self.assertEqual(loaded["names"], {})

    def test_runtime_state_is_fallback_and_only_rewrites_on_change(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._root(temp)
            self.assertTrue(
                persist_dashboard_universe(
                    ["395160", "005380"],
                    {"395160": "KODEX AI반도체", "005380": "현대차"},
                    {"395160": "ETF", "005380": "개별주식"},
                    root=root,
                )
            )
            self.assertFalse(
                persist_dashboard_universe(
                    ["005380", "395160"],
                    {"395160": "KODEX AI반도체", "005380": "현대차"},
                    {"395160": "ETF", "005380": "개별주식"},
                    root=root,
                )
            )
            loaded = load_dashboard_holdings(root)
            self.assertEqual(loaded["source"], "runtime-state")
            self.assertEqual(loaded["tickers"], ("005380", "395160"))
            self.assertEqual(loaded["names"]["005380"], "현대차")
            self.assertEqual(loaded["types"]["005380"], "개별주식")
            self.assertEqual(loaded["types"]["395160"], "ETF")


if __name__ == "__main__":
    unittest.main()
