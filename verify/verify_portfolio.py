"""verify/verify_portfolio.py - recompute every published portfolio figure from scratch.

Independent of fetch/portfolio.py: it imports nothing from the pipeline, reads
only the published portfolio.json, and rebuilds each figure from the monthly
series in that file with its own code. Anyone can run it:

    python verify/verify_portfolio.py                 # downloads the live file
    python verify/verify_portfolio.py data/portfolio.json

It prints every comparison it makes and exits 1 if any differs by more than
rounding. What it cannot check: whether the monthly series themselves are
right. Those come from Shiller, the World Bank, FRED and Coinbase, and are
published in the same file so they can be checked against those sources.
"""
import json, math, sys, urllib.request

URL = 'https://raw.githubusercontent.com/akpasz/btc-data/main/data/portfolio.json'


def fetch(arg):
    if arg:
        with open(arg, encoding='utf-8') as fh:
            return json.load(fh)
    with urllib.request.urlopen(URL, timeout=60) as r:
        return json.loads(r.read().decode())


class Book:
    """One portfolio, rebalanced monthly to fixed weights."""

    def __init__(self, P, legs, months):
        self.months = months
        lv = {k: dict(map(tuple, P['monthly'][k])) for k in legs}
        cash = dict(map(tuple, P['monthly']['cash']))
        self.growth = [1.0]
        self.rf = []
        for a, b in zip(months, months[1:]):
            gross = 0.0
            for k, w in legs.items():
                gross += w * lv[k][b] / lv[k][a]
            self.growth.append(self.growth[-1] * gross)
            self.rf.append(math.exp(math.log1p(cash[a] / 100.0) / 12.0) - 1.0)

    def figures(self):
        g = self.growth
        n = len(g) - 1
        monthly = [g[i + 1] / g[i] - 1.0 for i in range(n)]
        cagr = math.exp(math.log(g[-1] / g[0]) * 12.0 / n) - 1.0
        avg = math.fsum(monthly) / n
        sd = math.sqrt(math.fsum((x - avg) ** 2 for x in monthly) / (n - 1)) * math.sqrt(12.0)
        ex = [x - r for x, r in zip(monthly, self.rf)]
        exa = math.fsum(ex) / n * 12.0
        dsd = math.sqrt(math.fsum(min(e, 0.0) ** 2 for e in ex) / n) * math.sqrt(12.0)
        worst, top, run, longest = 0.0, g[0], 0, 0
        for x in g:
            if x >= top:
                top, run = x, 0
            else:
                run += 1
                longest = max(longest, run)
            worst = min(worst, x / top - 1.0)
        return {'cagr_pct': cagr * 100, 'vol_pct': sd * 100, 'sharpe': exa / sd,
                'sortino': exa / dsd, 'max_drawdown_pct': worst * 100,
                'longest_underwater_months': longest}


def legs_for(P, base, pct):
    b = pct / 100.0
    legs = {k: v * (1.0 - b) for k, v in P['bases'][base]['weights'].items()}
    if b:
        legs['btc'] = b
    return {k: v for k, v in legs.items() if v > 0}


TOL = {'cagr_pct': 0.006, 'vol_pct': 0.006, 'sharpe': 0.0006, 'sortino': 0.0006,
       'max_drawdown_pct': 0.051, 'longest_underwater_months': 0}


def main():
    P = fetch(sys.argv[1] if len(sys.argv) > 1 else None)
    months = [m for m, _ in P['monthly']['btc']]
    bad = checked = 0

    def compare(where, mine, theirs):
        nonlocal bad, checked
        for k, tol in TOL.items():
            if k not in theirs or theirs[k] is None:
                continue
            checked += 1
            if abs(mine[k] - theirs[k]) > tol:
                bad += 1
                print(f'  DIFFERS {where} {k}: recomputed {mine[k]:.4f}, published {theirs[k]}')

    for base, ladder in P['ladders'].items():
        for row in ladder:
            compare(f'{base} {row["btc_pct"]}%', Book(P, legs_for(P, base, row['btc_pct']), months).figures(), row)
    for row in P['start_dates']:
        win = [m for m in months if m >= f'{row["start_year"]}-01']
        for pct, figs in row['by_weight'].items():
            compare(f'entered {row["start_year"]} at {pct}%',
                    Book(P, legs_for(P, P['default_base'], int(pct)), win).figures(), figs)
    for L, rw in P.get('rolling_windows', {}).items():
        L = int(L)
        for start, gain, dd in rw['rows']:
            i = months.index(start)
            win = months[i:i + L + 1]
            a = Book(P, legs_for(P, P['default_base'], rw['weight_pct']), win).figures()
            z = Book(P, legs_for(P, P['default_base'], 0), win).figures()
            checked += 2
            if abs((a['cagr_pct'] - z['cagr_pct']) - gain) > 0.011 or abs((a['max_drawdown_pct'] - z['max_drawdown_pct']) - dd) > 0.11:
                bad += 1
                print(f'  DIFFERS rolling {L} from {start}')
    print(f'\n{checked} published figures recomputed independently; '
          + (f'{bad} DIFFER' if bad else 'all match to rounding'))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
