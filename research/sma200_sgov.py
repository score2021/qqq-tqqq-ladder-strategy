"""最新SGOV版研究信号入口；与辅助交易系统共用纯核心。

此入口不提供历史成交、费用和分红会计，不据此声称复现首页图表。
"""

from trading_system.strategy import VERSION, decide, indicators

__all__ = ["VERSION", "decide", "indicators"]
