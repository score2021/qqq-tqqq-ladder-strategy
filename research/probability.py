"""Chronological drawdown-case experiment. Completed cases only, no trading API."""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default='data/market.json')
    parser.add_argument('--start', default='2020-01-02')
    parser.add_argument('--output', default='runs/probability-2020')
    args = parser.parse_args()
    os.environ['STRATEGY_DATA'] = str(Path(args.data).resolve())
    from engine import run, weight, frames, idx, signal, pd, np
    levels = [.05, .10, .15, .20, .25, .30]
    high = frames['QQQ'].high.reindex(idx).rolling(20).max().shift(1)
    episodes, current = [], None
    for j in range(np.flatnonzero(idx >= pd.Timestamp('2013-01-01'))[0], len(idx)):
        price = float(signal.iloc[j])
        if current is None:
            dd = 1-price/high.iloc[j]
            if dd >= .05:
                current = {'start': j, 'H': float(high.iloc[j]), 'maxdd': dd}
        else:
            current['maxdd'] = max(current['maxdd'], 1-price/current['H'])
            if price >= current['H']:
                current['end'] = j
                episodes.append(current)
                current = None

    def estimate(dd, j):
        k = max((k for k, d in enumerate(levels) if dd >= d-1e-12), default=-1)
        if k < 0 or k == 5:
            return .5
        eligible = [e for e in episodes if e['end'] < j and e['maxdd'] >= levels[k]]
        assert all(e['end'] < j for e in eligible)
        n = len(eligible)
        successes = sum(e['maxdd'] >= levels[k+1] for e in eligible)
        return (successes+2.5)/(n+5) if n >= 5 else .5

    def model(direction):
        def calculate(dd, j):
            p = estimate(dd, j)
            return min(.7, weight(dd)*(.5+p if direction == 'direct' else 1.5-p))
        return calculate

    rows = []
    for lock in ['peak', 'next_signal']:
        for entry in ['fixed', 'direct', 'inverse']:
            row, _, _ = run('half8', start=args.start, lookback=20,
                exclude_signal_day=True, reference_field='high', reentry_mode=lock,
                weight_model=None if entry == 'fixed' else model(entry))
            row.update(entry=entry, lock=lock)
            rows.append(row)
    for mode in ['QQQ', 'TQQQ']:
        row, _, _ = run(mode, start=args.start, lookback=20, exclude_signal_day=True, reference_field='high')
        row.update(entry=mode, lock='none')
        rows.append(row)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(output/'summary.csv', index=False)
    (output/'summary.json').write_text(json.dumps(rows, indent=2))
    cases = {'completed': episodes, 'unfinished': current,
        'case_definition': 'first >=5% close drawdown versus prior20 intraday high; freeze high until closing recovery',
        'unfinished_excluded_from_probability': True,
        'estimation': 'end strictly before signal date; min5 eligible cases; Beta(2.5,2.5) smoothing'}
    (output/'cases.json').write_text(json.dumps(cases, indent=2))
    print('Completed cases:', len(episodes), 'Unfinished:', int(current is not None))
    print(pd.DataFrame(rows)[['entry','lock','CAGR','MDD','Sharpe_rf0']].round(4).to_string(index=False))


if __name__ == '__main__':
    main()
