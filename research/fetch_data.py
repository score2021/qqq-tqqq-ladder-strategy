"""Download public chart data; no brokerage login, orders, or credentials."""
import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import urllib.parse
import urllib.request

import pandas as pd


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', default='2012-01-01', type=date.fromisoformat)
    parser.add_argument('--end', default='2026-10-16', type=date.fromisoformat)
    parser.add_argument('--output', default='data/market.json')
    args = parser.parse_args()
    result = {}
    for symbol in ['QQQ', 'TQQQ']:
        params = {
            'period1': int(datetime.combine(args.start, datetime.min.time(), timezone.utc).timestamp()),
            'period2': int(datetime.combine(args.end + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp()),
            'interval': '1d', 'events': 'div,splits',
        }
        url = 'https://query2.finance.yahoo.com/v8/finance/chart/' + symbol + '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=60) as response:
            payload = json.load(response)['chart']['result'][0]
        dates = pd.to_datetime(payload['timestamp'], unit='s', utc=True).tz_convert('America/New_York').tz_localize(None).normalize()
        if not dates.is_unique or not dates.is_monotonic_increasing:
            raise ValueError('Non-unique or unordered calendar: ' + symbol)
        bars = payload['indicators']['quote'][0]
        output = {key: bars[key] for key in ['open', 'high', 'low', 'close']}
        if any(value is None or value <= 0 for values in output.values() for value in values):
            raise ValueError('Missing/non-positive OHLC: ' + symbol)
        output['time'] = [str(day.date()) for day in dates]
        # Chart OHLC and dividend amounts share the split-adjusted share basis.
        # Do not adjust these dividend events a second time for stock splits.
        output['corp_actions'] = [
            {'type': 'CashDividends',
             'date': pd.Timestamp(event['date'], unit='s', tz='UTC').tz_convert('America/New_York').strftime('%Y%m%d'),
             'value': str(event['amount'])}
            for event in payload.get('events', {}).get('dividends', {}).values()
        ]
        output['source'] = 'Yahoo Finance split-adjusted OHLC and split-adjusted dividend events'
        result[symbol] = output
        print(symbol, len(dates), dates[0].date(), dates[-1].date())
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result))
    print('SHA256', hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
