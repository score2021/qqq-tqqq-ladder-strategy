"""Compare the 20-session research variants; NOT a Tiger execution simulator."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default='data/market.json')
    parser.add_argument('--start', default='2020-01-02')
    parser.add_argument('--cost', type=float, default=.001)
    parser.add_argument('--output', default='runs/research-2020')
    args = parser.parse_args()
    os.environ['STRATEGY_DATA'] = str(Path(args.data).resolve())
    from engine import run, pd, idx
    modes = ['half8', 'half5', 'priorall', 'trail8', 'trail5', 'sma50', 'sma175', 'symmetric', 'QQQ', 'TQQQ']
    rows, equities, events = [], {}, {}
    for mode in modes:
        row, equity, orders = run(mode, start=args.start, cost=args.cost, lookback=20, exclude_signal_day=True, reference_field='high')
        rows.append(row)
        equities[mode] = equity
        events[mode] = orders
    destination = Path(args.output)
    destination.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(destination/'summary.csv', index=False)
    pd.DataFrame(equities).to_csv(destination/'equity.csv')
    (destination/'events.json').write_text(json.dumps(events, indent=2))
    (destination/'summary.json').write_text(json.dumps(rows, indent=2))
    metadata = {
        'strategy_version': 'research-20d-v1', 'data_source': 'user-selected JSON; default Yahoo Finance',
        'data_sha256': hashlib.sha256(Path(args.data).read_bytes()).hexdigest(),
        'python': platform.python_version(), 'pandas': pd.__version__,
        'start': rows[0]['start'], 'end': str(idx[-1].date()), 'cost_per_traded_dollar': args.cost,
        'signal_reference': 'max QQQ intraday high, prior20 completed sessions excluding signal day',
        'execution': 'next open; fractional synthetic total-return sleeve units',
        'native_tiger_result': False,
    }
    (destination/'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    print(pd.DataFrame(rows)[['mode', 'CAGR', 'MDD', 'Sharpe_rf0', 'rebalances']].round(4).to_string(index=False))


if __name__ == '__main__':
    main()
