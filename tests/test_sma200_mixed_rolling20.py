"""独立验证入场资格、退出优先级和混合仓控制边界。"""
import unittest
from research.sma200_mixed_rolling20 import Position as P, decide, indicator_frame
import pandas as pd


class MixedRollingTests(unittest.TestCase):
    def test_ordinary_qqq_enters_mixed_without_cash_cycle(self):
        d=decide(P.QQQ,102,100,102)
        self.assertEqual((d.position,d.target,d.reason),(P.MIXED,(.4,.6),'MIXED_ENTRY'))

    def test_entry_threshold_strict(self):
        for state in [P.QQQ,P.CASH]:
            self.assertIsNone(decide(state,101,100,101).target)
            self.assertIsNone(decide(state,100,100,100).target)

    def test_cash_cycle_entry_is_full_tqqq(self):
        a=decide(P.QQQ,99,100,120)
        b=decide(a.position,102,100,102)
        self.assertEqual((b.position,b.target,b.reason),(P.BOOST,(0.,1.),'MA_REENTER_TQQQ'))

    def test_ma_cash_exit_priority_and_equality(self):
        for state in [P.QQQ,P.MIXED,P.BOOST]:
            d=decide(state,99,100,120,.8)
            self.assertEqual((d.position,d.target,d.reason),(P.CASH,(0.,0.),'MA_CASH_EXIT'))
        self.assertIsNone(decide(P.CASH,99,100,120).target)

    def test_rolling_exit_includes_equality(self):
        for state in [P.MIXED,P.BOOST]:
            d=decide(state,92,50,100,.8)
            self.assertEqual((d.position,d.target,d.reason),(P.QQQ,(1.,0.),'ROLLING20_EXIT'))
            self.assertIsNone(decide(state,92.01,50,100,.6).target)

    def test_stop_does_not_reenter_same_signal(self):
        d=decide(P.MIXED,92,50,100,.8)
        self.assertEqual(d.target,(1.,0.))
        next_d=decide(d.position,93,50,100)
        self.assertEqual(next_d.target,(.4,.6))
        self.assertEqual(next_d.position,P.MIXED)

    def test_mixed_drift_strictly_over_seventy_percent(self):
        for weight in [.699,.7]:
            self.assertIsNone(decide(P.MIXED,110,100,115,weight).target)
        d=decide(P.MIXED,110,100,115,.70001)
        self.assertEqual((d.position,d.target,d.reason),(P.MIXED,(.4,.6),'DRIFT_CAP'))

    def test_boost_exempt_from_seventy_percent_cap(self):
        d=decide(P.BOOST,110,100,115,.98)
        self.assertEqual(d.position,P.BOOST)
        self.assertIsNone(d.target)

    def test_rolling_peak_may_expire_and_does_not_reset_on_entry(self):
        f=indicator_frame(pd.Series([100.]*200+[150.]+[140.]*20))
        self.assertEqual(f.peak20.iloc[-2],150)
        self.assertEqual(f.peak20.iloc[-1],140)
        self.assertIsNone(decide(P.MIXED,130,100,140,.6).target)
        # 入场前旧峰值仍在窗口中时，即使当前高于均线，也须移动退出。
        self.assertEqual(decide(P.BOOST,110,100,120,.98).position,P.QQQ)

    def test_cash_hold_is_not_ordinary_entry(self):
        self.assertEqual(decide(P.CASH,100,100,100).position,P.CASH)

    def test_mixed_does_not_rebalance_every_bullish_signal(self):
        d=decide(P.MIXED,110,100,110,.65)
        self.assertEqual(d.position,P.MIXED)
        self.assertIsNone(d.target)

    def test_bad_values_rejected(self):
        for value in [0,-1,float('nan'),float('inf')]:
            with self.assertRaises(ValueError):decide(P.QQQ,value,100,100)
            with self.assertRaises(ValueError):decide(P.QQQ,100,value,100)
            with self.assertRaises(ValueError):decide(P.QQQ,100,100,value)
        for weight in [-.1,1.1,float('nan'),float('inf')]:
            with self.assertRaises(ValueError):decide(P.MIXED,100,100,100,weight)
        with self.assertRaises(ValueError):decide('UNKNOWN',100,100,100)
        with self.assertRaises(ValueError):decide(P.QQQ,110,100,100)


if __name__=='__main__':unittest.main()
