"""The trigger record lists every call the LPPLS model has made.

A count - one hit in sixteen - hides that the model HAS made spectacular calls
(17 March 2021 predicted 10 November; the top came on the 9th) and that they
were indistinguishable at the time from the misses around them. The record
shows both, and must never disagree with the published critical-time test."""
import os, sys, json, datetime as dt
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'fetch'))
import lppls as L

HERE = os.path.join(os.path.dirname(__file__), '..', 'data')


def _real():
    bc = json.load(open(os.path.join(HERE, 'blockchain.json')))
    m = {d: float(v) for d, v in bc['series']['price'] if float(v) > 0}
    ds = sorted(m)
    return [dt.date.fromisoformat(d) for d in ds], [m[d] for d in ds], json.load(open(os.path.join(HERE, 'lppls.json')))


def test_the_record_agrees_with_the_published_test():
    """Same run definition, same peak rule: the hit column must sum to the
    critical-time test's hit count, today and on every future run."""
    dates, prices, doc = _real()
    s = doc['series']
    tr = L.trigger_record(dates, prices, s['lppls_pos'], s['lppls_tc_days'])
    ct = L.critical_time_test(dates, prices, s['lppls_pos'], s['lppls_tc_days'], draws=20)
    if 'by_tolerance' not in ct:
        return
    assert tr['hits'] == ct['by_tolerance']['30']['hits']
    assert tr['scored'] == ct['trials']


def _synth(n=900, sig_days=(), tc=10.0):
    d0 = dt.date(2013, 1, 1)
    dates = [d0 + dt.timedelta(days=i) for i in range(n)]
    prices = list(np.linspace(100, 300, n))
    pos = [[dates[i].isoformat(), 1.0 if i in sig_days else 0.0] for i in range(n)]
    tcs = [[dates[i].isoformat(), tc] for i in sig_days]
    return dates, prices, pos, tcs


def test_an_unfinished_year_is_pending_and_never_scored():
    dates, prices, pos, tcs = _synth(sig_days=(850,))
    tr = L.trigger_record(dates, prices, pos, tcs)
    assert tr['rows'][0]['status'] == 'pending' and tr['scored'] == 0


def test_signal_days_within_a_week_are_one_run():
    dates, prices, pos, tcs = _synth(sig_days=(100, 104, 108, 200))
    assert L.trigger_record(dates, prices, pos, tcs)['runs'] == 2


def test_a_rising_market_scores_the_top_as_later():
    """Price rises all year, so the peak is the last day; a 10-day critical
    date is a miss with the top coming later - the model's usual failure."""
    dates, prices, pos, tcs = _synth(sig_days=(100,))
    r = L.trigger_record(dates, prices, pos, tcs)['rows'][0]
    assert r['status'] == 'top_came_later' and r['miss_days'] > 300


def test_a_run_without_a_date_is_listed_but_not_a_trial():
    dates, prices, pos, _ = _synth(sig_days=(100,))
    tr = L.trigger_record(dates, prices, pos, [])
    assert tr['rows'][0]['status'] == 'no_date' and tr['scored'] == 0
