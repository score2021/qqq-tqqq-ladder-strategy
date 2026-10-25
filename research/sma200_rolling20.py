"""用户明确规则：普通QQQ、均线清仓、恢复全TQQQ、20日收盘峰值退出。

纯信号模块，无券商访问；目标仅在下一交易日实际成交后应用到持仓状态。
"""
from dataclasses import dataclass
from enum import Enum
import math
import pandas as pd


class Position(str, Enum):
    QQQ='QQQ'
    CASH='CASH'
    TQQQ='TQQQ'


@dataclass(frozen=True)
class Decision:
    position: Position
    target: tuple[float,float] | None
    reason: str


def indicator_frame(closes: pd.Series) -> pd.DataFrame:
    """仅按已完成收盘计算；窗口包括信号日，历史不足时保持NaN。"""
    values=closes.astype(float)
    if not values.index.is_unique or not values.index.is_monotonic_increasing:
        raise ValueError('QQQ收盘索引必须唯一且递增')
    if not all(math.isfinite(v) and v>0 for v in values):
        raise ValueError('QQQ收盘必须为正的有限数')
    return pd.DataFrame({'sma200':values.rolling(200,min_periods=200).mean(),
                         'peak20':values.rolling(20,min_periods=20).max()})


def decide(position: Position | str, close: float, sma200: float, peak20: float) -> Decision:
    """同一完整收盘只生成一个目标；100%均指预留现金后的可投资资产。"""
    position=Position(position)
    if not all(math.isfinite(v) and v>0 for v in (close,sma200,peak20)):
        raise ValueError('收盘、SMA200和20日最高收盘必须为正的有限数')
    if peak20<close-1e-9:
        raise ValueError('包含信号日的20日最高收盘不能低于当前收盘')
    if close<=sma200*.99:
        return Decision(Position.CASH,(0.,0.),'MA_CASH_EXIT') if position!=Position.CASH else Decision(position,None,'HOLD')
    if position==Position.CASH and close>sma200*1.01:
        return Decision(Position.TQQQ,(0.,1.),'MA_REENTER_TQQQ')
    if position==Position.TQQQ and close<=peak20*.92:
        return Decision(Position.QQQ,(1.,0.),'ROLLING20_EXIT')
    return Decision(position,None,'HOLD')
