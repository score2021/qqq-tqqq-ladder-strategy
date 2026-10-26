"""新加坡单次任务、闲置现金预算与收盘证据的合成回归。"""
from copy import deepcopy
from datetime import datetime, timezone
import unittest

from cloud_runner.runtime import analyze, due_jobs
from cloud_runner.notification import render
from test_cloud_runner import bundle, NOW
from test_portfolio_scope import configured, account


class DailyCashTests(unittest.TestCase):
    def make_report(self, **overrides):
        b = account()
        c = configured()
        c.update(phase='MIXED', cash_allocation_policy='EXCESS_CASH_TO_QQQ_IN_NORMAL_STATE')
        c.update(overrides)
        return analyze(b, NOW, c), b, c

    def test_single_singapore_window_summer_winter_holiday_and_end(self):
        for day in ('2026-10-26', '2026-11-02', '2026-11-27'):
            at = datetime.fromisoformat(day+'T13:20:00+00:00')
            self.assertEqual(due_jobs(at), [day+':preopen'])
            self.assertEqual(due_jobs(at.replace(minute=19)), [])
            self.assertEqual(due_jobs(at.replace(minute=30)), [])
            self.assertEqual(due_jobs(at.replace(hour=20)), [])
        for day in ('2026-11-26', '2026-10-25'):
            self.assertEqual(due_jobs(datetime.fromisoformat(day+'T13:20:00+00:00')), [])

    def test_cash_budget_uses_settled_cash_and_reserve_without_tqqq_change(self):
        r, _, _ = self.make_report()
        cash = r['cash_review']
        self.assertEqual(cash['status'], 'REVIEW')
        self.assertEqual(cash['asset'], 'QQQ')
        self.assertEqual(cash['reserve_usd'], '200.0027250')
        self.assertEqual(cash['budget_before_fees_usd'], '3079.9977750')
        self.assertTrue(cash['preserve_tqqq'])
        self.assertEqual(r['executable_orders'], [])
        self.assertIsNone(r['decision'])
        self.assertEqual(r['price_decision']['reason'], 'HOLD')
        self.assertFalse(r['close_review']['verified'])
        self.assertIn('CASH_FLOW_LEDGER_UNAVAILABLE', r['close_review']['reasons'])
        self.assertEqual(r['allocation']['settled_cash_usd'], '3280.0005')

    def test_cash_missing_settlement_low_cash_and_conflict(self):
        _, b, c = self.make_report()
        for row in b['balances']['balances']:
            if row['currency'] == 'USD': row.pop('settled_cash')
        r = analyze(b, NOW, c)
        self.assertEqual(r['cash_review']['reason'], 'SETTLED_CASH_UNVERIFIED')
        self.assertTrue(r['allocation']['confirmed'])
        b['balances']['balances'][1]['settled_cash'] = '0'
        self.assertEqual(analyze(b, NOW, c)['cash_review']['status'], 'WAIT')
        b['balances']['balances'][1]['settled_cash'] = '250'
        r = analyze(b, NOW, c)
        self.assertEqual(r['cash_review']['budget_before_fees_usd'], '49.9972750')
        b['orders']['orders'] = [{'order_status':'Submitted'}]
        self.assertEqual(analyze(b, NOW, c)['cash_review']['reason'], 'ORDER_OR_INSTRUCTION_CONFLICT')
        b['orders']['orders'] = []
        b['instructions']['instructions'] = [{'id':'synthetic'}]
        self.assertEqual(analyze(b, NOW, c)['cash_review']['reason'], 'ORDER_OR_INSTRUCTION_CONFLICT')

    def test_price_exit_and_boost_override_cash_advice_and_policy_required(self):
        _, b, c = self.make_report()
        for price, reason in (('90', 'MA_DEFENSE'), ('93', 'ROLLING20_EXIT')):
            test = deepcopy(b)
            if price == '93':
                test['history']['QQQ']['close'] = ['80']*181+['105']*19+['93']
            else: test['history']['QQQ']['close'][-1] = price
            r = analyze(test, NOW, c)
            self.assertEqual(r['decision']['reason'], reason)
            self.assertEqual(r['cash_review']['status'], 'DEFER')
        self.assertEqual(analyze(b, NOW, dict(c,phase='BOOST'))['cash_review']['status'], 'DEFER')
        c.pop('cash_allocation_policy')
        self.assertEqual(analyze(b, NOW, c)['cash_review']['status'], 'DISABLED')
        c['cash_allocation_policy'] = 'EXCESS_CASH_TO_QQQ_IN_NORMAL_STATE'
        c['cash_scope_confirmed'] = False
        self.assertEqual(analyze(b, NOW, c)['cash_review']['reason'], 'ALLOCATION_UNVERIFIED')

    def test_post_close_trades_do_not_turn_snapshot_into_verified_close(self):
        _, b, c = self.make_report()
        b['trades']['trades'] = [{'symbol':'TQQQ','trade_time':'2026-10-23T20:01:00Z'},
                                 {'symbol':'QQQ','trade_time':'2026-10-23T19:59:00Z'}]
        r = analyze(b, NOW, c)
        self.assertEqual(r['close_review']['post_close_trade_count'], 1)
        self.assertIn('POST_CLOSE_TRADES_PRESENT', r['close_review']['reasons'])
        self.assertIsNone(r['decision'])
        b['trades']['trades'].append({'symbol':'QQQ'})
        self.assertIn('TRADE_TIME_UNVERIFIED', analyze(b, NOW, c)['close_review']['reasons'])

    def test_notification_budget_and_drift_warning_fit_without_quantities(self):
        r, _, _ = self.make_report()
        msg = render(r)
        self.assertIn('闲置现金补QQQ', msg)
        self.assertIn('3,080.00', msg)
        self.assertIn('200.00', msg)
        self.assertIn('保留现有TQQQ', msg)
        self.assertIn('70%漂移待核对', msg)
        self.assertIn('未扣佣金', msg)
        self.assertIn('未提交订单', msg)
        self.assertNotIn('无新增买入', msg)
        self.assertLessEqual(len(msg.encode()), 1900)
