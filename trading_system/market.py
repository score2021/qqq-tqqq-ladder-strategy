"""十进制、带时区时间及真实交易日历校验。"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd


class InvalidInput(ValueError):
    """输入不能支持确定性计算。"""


def number(value):
    try:
        if isinstance(value, bool):
            raise InvalidOperation
        result = Decimal(str(value))
        if not result.is_finite():
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise InvalidInput("INVALID_DECIMAL") from exc


def positive(value):
    result = number(value)
    if result <= 0:
        raise InvalidInput("NON_POSITIVE_VALUE")
    return result


def stamp(value):
    try:
        result = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError) as exc:
        raise InvalidInput("TIMEZONE_REQUIRED") from exc


def floor_step(value, step):
    return (number(value) / positive(step)).to_integral_value(
        rounding=ROUND_FLOOR
    ) * number(step)


@lru_cache(maxsize=1)
def _calendar():
    return xcals.get_calendar("XNYS", start="2000-01-01", end="2035-12-31")


class Calendar:
    def session(self, day):
        try:
            cal = _calendar()
            label = pd.Timestamp(day)
            return cal.session_open(label).to_pydatetime(), cal.session_close(
                label
            ).to_pydatetime()
        except (ValueError, KeyError) as exc:
            raise InvalidInput("NOT_A_TRADING_SESSION") from exc

    def history(self, end, count):
        end = date.fromisoformat(end)
        sessions = _calendar().sessions_in_range(
            pd.Timestamp(end - timedelta(days=count * 3)), pd.Timestamp(end)
        )
        return [str(x.date()) for x in sessions[-count:]]

    def next_session(self, day):
        return str(_calendar().next_session(pd.Timestamp(day)).date())

    def latest_complete(self, now):
        now = stamp(now)
        local_day = now.astimezone(
            __import__("zoneinfo").ZoneInfo("America/New_York")
        ).date()
        label = _calendar().date_to_session(
            pd.Timestamp(local_day), direction="previous"
        )
        if _calendar().session_close(label).to_pydatetime() > now:
            label = _calendar().previous_session(label)
        return str(label.date())

    def executable(self, signal_day, now):
        opens, closes = self.session(self.next_session(signal_day))
        return (
            opens + timedelta(minutes=5) <= stamp(now) < closes - timedelta(minutes=5)
        )

    def validate_bars(self, bars, now):
        if len(bars) < 200:
            raise InvalidInput("INSUFFICIENT_HISTORY")
        dates = [x["date"] for x in bars]
        if dates != sorted(set(dates)) or dates[-1] != self.latest_complete(now):
            raise InvalidInput("INCOMPLETE_OR_STALE_SIGNAL")
        if dates[-200:] != self.history(dates[-1], 200):
            raise InvalidInput("MISSING_TRADING_SESSION")
        if any(x.get("complete") is not True for x in bars):
            raise InvalidInput("INCOMPLETE_DAILY_BAR")
        return dates[-1]
