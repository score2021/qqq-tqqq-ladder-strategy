"""A新版：普通60/40、均线现金恢复全TQQQ、20日滚动峰值退出。"""
from dataclasses import dataclass
from enum import Enum
import math
from research.sma200_rolling20 import indicator_frame
class Position(str, Enum):
    QQQ='QQQ'
    CASH='CASH'
    BOOST='BOOST'
    MIXED='MIXED'
@dataclass(frozen=True)
class Decision:
    position: Position
    target: tuple[float,float] | None
    reason: str

def decide(position, close, sma200, peak20, tqqq_weight=0.):
    """完整收盘产生最多一个动作；调用方在下一交易日成交后应用状态。"""
    position=Position(position)
    if not all(math.isfinite(v) and v>0 for v in (close,sma200,peak20)):
        raise ValueError('价格与指标必须为正的有限数')
    if not math.isfinite(tqqq_weight) or not 0<=tqqq_weight<=1:
        raise ValueError('TQQQ真实收盘权重须在0至1之间')
    if peak20<close-1e-9:
        raise ValueError('含信号日的20日最高收盘不能低于当前收盘')
    if close<=sma200*.99:
        return Decision(Position.CASH,(0.,0.),'MA_CASH_EXIT') if position!=Position.CASH else Decision(position,None,'HOLD')
    if position in (Position.MIXED,Position.BOOST) and close<=peak20*.92:
        return Decision(Position.QQQ,(1.,0.),'ROLLING20_EXIT')
    if position in (Position.QQQ,Position.CASH) and close>sma200*1.01:
        if position==Position.CASH:
            return Decision(Position.BOOST,(0.,1.),'MA_REENTER_TQQQ')
        return Decision(Position.MIXED,(.4,.6),'MIXED_ENTRY')
    if position==Position.MIXED and tqqq_weight>.7:
        return Decision(Position.MIXED,(.4,.6),'DRIFT_CAP')
    return Decision(position,None,'HOLD')
