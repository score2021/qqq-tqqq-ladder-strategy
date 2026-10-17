"""Offline synthetic checks of the unchanged Tiger script; not a platform mock backtest."""
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest

SCRIPT = Path(__file__).resolve().parents[1]/'strategies'/'tiger'/'main.py'


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.orders = []
        def order(asset, amount, **kwargs):
            self.orders.append((asset, amount, kwargs))
            return str(len(self.orders))
        self.code = runpy.run_path(str(SCRIPT), init_globals={
            'symbol': lambda name: name,
            'set_benchmark': lambda *x: None,
            'set_max_leverage': lambda *x: None,
            'set_slippage': lambda *x: None,
            'log': SimpleNamespace(info=lambda *x: None), 'order': order,
        })
        self.c = SimpleNamespace()
        self.code['initialize'](self.c)

    def signal(self, price, high=100):
        return self.code['signal'](self.c, [high]*251+[price])

    def test_entry_thresholds_and_jump(self):
        f = self.code['entry_weight']
        self.assertEqual(f(.049), 0)
        self.assertEqual(f(.05), .1)
        self.assertEqual(f(.20), .6)
        self.assertEqual(f(.31), .7)

    def test_native_uses_all_supplied_closes(self):
        # A high in the oldest part still affects entry; this is NOT a 20-day rule.
        p = [100] + [94]*251
        self.assertEqual(self.code['signal'](self.c, p), (.1, 'ENTRY'))

    def test_deeper_entry_then_trailing_before_recovery(self):
        self.assertEqual(self.signal(94), (.1, 'ENTRY'))
        self.assertEqual(self.signal(90)[0], .3)
        self.assertEqual(self.signal(85), (0., 'TRAIL_EXIT'))

    def test_half_is_target_weight_not_actual_shares(self):
        self.signal(94)
        self.assertEqual(self.signal(99.5), (.05, 'PRIOR_HIGH_HALF'))
        self.assertEqual(self.signal(110)[0], .05)
        self.assertEqual(self.signal(100), (0., 'TRAIL_EXIT'))

    def test_reentry_deeper_by_five_points(self):
        self.signal(94)
        self.signal(86)
        self.assertEqual(self.signal(82), (0., 'LOCKED'))
        self.assertEqual(self.signal(81), (.4, 'ENTRY'))

    def test_near_high_unlocks(self):
        self.signal(94)
        self.signal(86)
        self.assertEqual(self.signal(99.5), (0., 'WAIT'))
        self.assertIsNone(self.c.lock_price)

    def test_sells_before_spending_proceeds(self):
        self.c.portfolio = SimpleNamespace(cash=0., positions={'QQQ': SimpleNamespace(amount=100)})
        self.code['rebalance'](self.c, 100., 50., .3)
        self.assertEqual(len(self.orders), 1)
        self.assertEqual(self.orders[0][0], 'QQQ')
        self.assertLess(self.orders[0][1], 0)

    def test_buy_limit_budget_and_reserve(self):
        self.c.portfolio = SimpleNamespace(cash=1000., positions={})
        self.code['rebalance'](self.c, 100., 50., .3)
        budget = sum(n*kw['limit_price'] for _, n, kw in self.orders)
        self.assertLessEqual(budget, 980.)
        self.assertTrue(all(kw['style']=='limit' for _, _, kw in self.orders))

    def test_pending_orders_block_new_orders(self):
        self.c.pending = ['unsettled']
        self.code['rebalance'](self.c, 100., 50., .3)
        self.assertEqual(self.orders, [])

    def test_negative_cash_stops(self):
        self.c.portfolio = SimpleNamespace(cash=-1., positions={})
        with self.assertRaises(ValueError):
            self.code['rebalance'](self.c, 100., 50., .3)


if __name__ == '__main__':
    unittest.main()
