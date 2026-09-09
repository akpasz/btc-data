"""Regression tests for the btc-data pipeline.

Every expected value here is hand-computed or reasoned from the specification,
never copied from the code's own output. A test that asserts what the code
currently does would pass forever and catch nothing.

    python -m pytest tests/ -q          from the repo root

Pure functions only: no network, no fixtures over a few KB, no snapshot files.
The point is that these run in seconds on every push.
"""
import os, sys, math, json, datetime as dt
import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'fetch'))


# ---------------------------------------------------------------- merge_series
class TestMergeSeries:
    """The union that protects every stored series. If this breaks, history is
    silently truncated or a bad refetch overwrites good data."""

    def _f(self):
        import fetch_all
        return fetch_all.merge_series

    def test_new_value_wins_on_a_shared_date(self):
        merge = self._f()
        out = merge({'p': [['2026-01-01', 1.0]]}, {'p': [['2026-01-01', 2.0]]})
        assert out['p'] == [['2026-01-01', 2.0]]

    def test_history_is_preserved_when_the_new_fetch_is_short(self):
        # the real failure mode: an API returns 30 days, must not erase 3000
        merge = self._f()
        old = {'p': [[f'2026-01-{d:02d}', float(d)] for d in range(1, 21)]}
        new = {'p': [['2026-01-20', 99.0]]}
        out = merge(old, new)
        assert len(out['p']) == 20
        assert out['p'][-1] == ['2026-01-20', 99.0]
        assert out['p'][0] == ['2026-01-01', 1.0]

    def test_output_is_date_sorted_regardless_of_input_order(self):
        merge = self._f()
        out = merge({}, {'p': [['2026-03-01', 3], ['2026-01-01', 1], ['2026-02-01', 2]]})
        assert [d for d, _ in out['p']] == ['2026-01-01', '2026-02-01', '2026-03-01']

    def test_nulls_are_dropped_not_stored(self):
        merge = self._f()
        out = merge({'p': [['2026-01-01', 1.0]]}, {'p': [['2026-01-02', None]]})
        assert out['p'] == [['2026-01-01', 1.0]]

    def test_a_null_does_not_erase_an_existing_value(self):
        merge = self._f()
        out = merge({'p': [['2026-01-01', 5.0]]}, {'p': [['2026-01-01', None]]})
        assert out['p'] == [['2026-01-01', 5.0]]

    def test_keys_present_in_only_one_side_survive(self):
        merge = self._f()
        out = merge({'a': [['2026-01-01', 1]]}, {'b': [['2026-01-01', 2]]})
        assert set(out) == {'a', 'b'}


# ---------------------------------------------------------------- freshness
class TestFreshness:
    """'A fetch can succeed and still be stale.' This is the function that
    keeps that distinction honest, so the red notice stays meaningful."""

    def _f(self, now='2026-09-10T00:00:00+00:00'):
        import fetch_all
        fetch_all.NOW = now
        return fetch_all.freshness

    def test_same_day_is_current(self):
        assert self._f()('blockchain', '2026-09-10')['freshness'] == 'current'

    def test_default_threshold_is_two_days(self):
        f = self._f()
        assert f('blockchain', '2026-09-08')['freshness'] == 'current'   # age 2, thr 2
        assert f('blockchain', '2026-09-07')['freshness'] == 'stale'     # age 3

    def test_per_source_thresholds_are_respected(self):
        f = self._f()
        # macro is allowed 10 days; the same date would be stale for blockchain
        assert f('macro', '2026-09-02')['freshness'] == 'current'
        assert f('blockchain', '2026-09-02')['freshness'] == 'stale'

    def test_quarterly_filings_get_a_long_threshold(self):
        assert self._f()('etf_quarterly', '2026-06-30')['freshness'] == 'current'

    def test_missing_date_is_unknown_not_stale(self):
        r = self._f()(  'blockchain', None)
        assert r['freshness'] == 'unknown' and r['age_days'] is None

    def test_unparseable_date_is_unknown_not_a_crash(self):
        assert self._f()('blockchain', 'not-a-date')['freshness'] == 'unknown'

    def test_age_is_reported_in_days(self):
        assert self._f()('blockchain', '2026-09-04')['age_days'] == 6


# ---------------------------------------------------------------- to_daily
class TestToDaily:
    def _f(self):
        import kpis
        return kpis.to_daily

    def test_a_gap_is_linearly_interpolated(self):
        ds, vs = self._f()([['2026-01-01', 0.0], ['2026-01-05', 4.0]])
        assert len(ds) == 5
        assert list(vs) == [0.0, 1.0, 2.0, 3.0, 4.0]      # exactly linear

    def test_a_gap_longer_than_sixty_days_is_left_open(self):
        ds, vs = self._f()([['2026-01-01', 0.0], ['2026-06-01', 4.0]])
        assert len(ds) == 2, 'a 151-day gap must not be invented'

    def test_a_sixty_day_gap_is_still_filled(self):
        ds, _ = self._f()([['2026-01-01', 0.0], ['2026-03-02', 60.0]])
        assert len(ds) == 61                              # boundary: g == 60

    def test_nulls_are_skipped(self):
        ds, vs = self._f()([['2026-01-01', 1.0], ['2026-01-02', None], ['2026-01-03', 3.0]])
        assert list(vs) == [1.0, 2.0, 3.0]                # interpolated, not None


# ---------------------------------------------------------------- pct_of
class TestPctOf:
    def _f(self):
        import kpis
        return kpis.pct_of

    def test_minimum_is_zero(self):
        assert self._f()(np.array([1., 2., 3., 4.]), 1.0) == 0.0

    def test_value_above_everything_is_one_hundred(self):
        assert self._f()(np.array([1., 2., 3., 4.]), 9.0) == 100.0

    def test_midpoint(self):
        assert self._f()(np.array([1., 2., 3., 4.]), 3.0) == 50.0


# ---------------------------------------------------------------- _sma
class TestSMA:
    def _f(self):
        import research
        return research._sma

    def test_flat_series_averages_to_itself(self):
        out = self._f()(np.array([5.0] * 10), 3)
        assert np.isnan(out[0]) and np.isnan(out[1])
        assert out[2] == pytest.approx(5.0)

    def test_window_is_not_emitted_before_it_is_full(self):
        out = self._f()(np.arange(10, dtype=float), 5)
        assert np.all(np.isnan(out[:4]))
        assert out[4] == pytest.approx(2.0)               # mean(0..4)

    def test_a_single_nan_inside_a_window_is_tolerated(self):
        a = np.arange(20, dtype=float); a[5] = np.nan
        out = self._f()(a, 10)
        assert np.isfinite(out[10]), '90% finite should still average'

    def test_too_many_nans_suppress_the_value(self):
        a = np.full(20, np.nan); a[:2] = 1.0
        out = self._f()(a, 10)
        assert np.all(np.isnan(out[9:]))


# ---------------------------------------------------------------- state_of
class TestStateOf:
    def _f(self):
        import research
        return research.state_of

    @pytest.mark.parametrize('pct,expected', [
        (0, 'very cheap'), (9.99, 'very cheap'),
        (10, 'cheap'), (29.99, 'cheap'),
        (30, 'average'), (69.99, 'average'),
        (70, 'expensive'), (89.99, 'expensive'),
        (90, 'very expensive'), (100, 'very expensive'),
    ])
    def test_band_boundaries_are_exact(self, pct, expected):
        assert self._f()(pct) == expected

    def test_none_and_nan_return_none(self):
        assert self._f()(None) is None
        assert self._f()(float('nan')) is None


# ---------------------------------------------------------------- schema_version
class TestSchemaVersion:
    """These files are read directly by third parties. A consumer must be able
    to tell whether the shape it was written against still holds."""

    def test_constant_is_declared_and_well_formed(self):
        import fetch_all
        assert hasattr(fetch_all, 'SCHEMA_VERSION')
        major, minor = fetch_all.SCHEMA_VERSION.split('.')
        assert major.isdigit() and minor.isdigit()

    def test_every_saved_source_document_carries_it(self, tmp_path):
        import fetch_all
        fetch_all.OUT = str(tmp_path)
        fetch_all.NOW = '2026-09-10T00:00:00+00:00'
        fetch_all.manifest = {}
        fetch_all.save('demo', 'https://example.com', {'p': [['2026-09-10', 1.0]]})
        doc = json.load(open(tmp_path / 'demo.json'))
        assert doc['schema_version'] == fetch_all.SCHEMA_VERSION

    def test_the_scorecard_carries_it_too(self):
        import scorecard
        src = open(scorecard.__file__).read()
        assert "schema_version" in src, 'derived layers must be versioned as well'


# ---------------------------------------------------------------- slim series
class TestSlim:
    """One file per series. The rounding here is the risk: a fixed number of
    decimal places destroyed 1,341 hash-rate points before this was caught."""

    def _m(self):
        import slim
        return slim

    def test_significant_figures_survive_tiny_values(self):
        r = self._m()._round(4.97102696296296e-08, 9)
        assert r != 0, 'hash rate in 2009 is 5e-08 and must not round to zero'
        assert abs(r - 4.97102696296296e-08) / 4.97102696296296e-08 < 1e-8

    def test_significant_figures_survive_huge_values(self):
        v = 1_631_303_497_729.1895
        assert abs(self._m()._round(v, 9) - v) / v < 1e-8

    def test_zero_stays_zero(self):
        assert self._m()._round(0.0, 9) == 0

    def test_non_numeric_returns_none_not_a_crash(self):
        assert self._m()._round(None, 9) is None
        assert self._m()._round('x', 9) is None

    def test_integers_stay_integers_where_the_unit_is_a_count(self):
        assert isinstance(self._m()._round(6454, 12), int)

    def test_every_mapped_series_has_a_distinct_published_name(self):
        m = self._m().MAP
        names = [n for src in m.values() for n, _ in src.values()]
        assert len(names) == len(set(names)), 'two series would overwrite one file'

    def test_published_names_are_url_safe(self):
        import re
        for src in self._m().MAP.values():
            for name, _ in src.values():
                assert re.fullmatch(r'[a-z0-9_]+', name), name


# ---------------------------------------------------------------- wiring
class TestDerivedLayersAreWired:
    """Phase 5 shipped a fetch_all.py rebuilt from an older copy and silently
    dropped the scorecard call. The pipeline kept running and the manifest kept
    saying 'ok' — from the previous run's file. Nothing would have reported it.
    These assert the calls exist, so a future rebuild cannot quietly lose one."""

    def _src(self):
        import os
        p = os.path.join(os.path.dirname(__file__), '..', 'fetch', 'fetch_all.py')
        return open(p, encoding='utf-8').read()

    def test_scorecard_is_called(self):
        assert 'import scorecard' in self._src()
        assert 'scorecard.main()' in self._src()

    def test_slim_is_called(self):
        assert 'import slim' in self._src()
        assert 'slim.main()' in self._src()

    def test_kpis_is_called(self):
        assert 'kpis.main()' in self._src()

    def test_each_derived_layer_is_isolated(self):
        """One failing layer must not abort the run or block the manifest."""
        src = self._src()
        for name in ('slim', 'scorecard', 'kpis'):
            i = src.find(f'import {name};')
            assert i > 0, name
            assert 'try:' in src[max(0, i - 260):i], f'{name} is not inside a try'
            assert f"manifest_doc['{name}'] = 'error:" in src, f'{name} records no failure reason'


# ---------------------------------------------------------------- date alignment
class TestAlign:
    """Blockchain.com and Coin Metrics labelled the same day's price one day
    apart, so the site published a price from one convention beside a ratio
    computed from the other. Divide the two published numbers and you got 1.53
    against a published MVRV of 1.499."""

    def _m(self):
        import align
        return align

    def test_shift_moves_every_date_and_loses_nothing(self):
        m = self._m()
        s = {'p': [['2026-01-01', 1.0], ['2026-01-02', 2.0]]}
        out = m._shift_series(s, 1)
        assert [d for d, _ in out['p']] == ['2026-01-02', '2026-01-03']
        assert [v for _, v in out['p']] == [1.0, 2.0], 'values must not change'

    def test_values_travel_with_their_dates(self):
        out = self._m()._shift_series({'p': [['2026-03-31', 9.0]]}, 1)
        assert out['p'] == [['2026-04-01', 9.0]], 'month boundary'

    def test_leap_day_survives(self):
        out = self._m()._shift_series({'p': [['2028-02-28', 1.0]]}, 1)
        assert out['p'][0][0] == '2028-02-29'

    def test_unparseable_dates_are_dropped_not_crashed(self):
        out = self._m()._shift_series({'p': [['nonsense', 1.0], ['2026-01-01', 2.0]]}, 1)
        assert len(out['p']) == 1

    def test_canonical_source_is_never_shifted(self):
        m = self._m()
        assert m.CANONICAL not in m.SHIFT, \
            'the canonical convention must be the one nothing moves against'

    def test_shifts_are_whole_days_and_small(self):
        for src, days in self._m().SHIFT.items():
            assert isinstance(days, int) and abs(days) <= 2, \
                f'{src}: a large shift means the diagnosis is wrong, not the data'


class TestAlignRunsFirst:
    """align must run before anything derives from the sources, or the derived
    layers are computed on the unaligned data and the fix does nothing."""

    def _src(self):
        import os
        p = os.path.join(os.path.dirname(__file__), '..', 'fetch', 'fetch_all.py')
        return open(p, encoding='utf-8').read()

    def test_align_is_called(self):
        assert 'import align' in self._src() and 'align.main()' in self._src()

    def test_align_precedes_every_derived_layer(self):
        src = self._src()
        a = src.find('import align')
        for later in ('import kpis', 'import slim', 'import scorecard'):
            assert a < src.find(later), f'align must run before {later}'


# ---------------------------------------------------------------- portfolio
class TestPortfolio:
    """Portfolio arithmetic fails silently: a wrong denominator or an off-by-one
    window produces numbers that look entirely plausible. Every expectation here
    is hand-computed."""

    def _m(self):
        import portfolio
        return portfolio

    def _flat(self, n=13):
        return [f'2020-{i:02d}' for i in range(1, 13)] + ['2021-01'][:max(0, n - 12)]

    def test_doubling_in_twelve_months_is_100_percent_cagr(self):
        P = self._m()
        months = self._flat()
        lv = {m: 2 ** (i / 12) for i, m in enumerate(months)}
        st = P.stats(P.path({'a': P.returns(lv, months)}, {'a': 1.0}, months), months)
        assert abs(st['cagr_pct'] - 100.0) < 0.01

    def test_flat_series_has_no_volatility_and_no_drawdown(self):
        P = self._m()
        months = self._flat()
        lv = {m: 1.0 for m in months}
        st = P.stats(P.path({'a': P.returns(lv, months)}, {'a': 1.0}, months), months)
        assert st['vol_pct'] == 0.0 and st['max_drawdown_pct'] == 0.0

    def test_max_drawdown_is_peak_to_trough(self):
        P = self._m()
        months = self._flat()
        lv = dict(zip(months, [100, 90, 80, 70, 60, 50, 60, 70, 80, 90, 100, 110, 120]))
        st = P.stats(P.path({'a': P.returns(lv, months)}, {'a': 1.0}, months), months)
        assert abs(st['max_drawdown_pct'] + 50.0) < 0.01, '100 to 50 is -50%'
        assert st['longest_underwater_months'] == 9

    def test_rebalancing_and_holding_differ(self):
        """0.5x(+10%) + 0.5x(-10%) rebalanced is exactly flat; held, the winner
        compounds. 0.5(1.1^2) + 0.5(0.9^2) = 1.01."""
        P = self._m()
        mm = ['2020-01', '2020-02', '2020-03']
        a = {'2020-01': 100, '2020-02': 110, '2020-03': 121}
        b = {'2020-01': 100, '2020-02': 90, '2020-03': 81}
        ra, rb = P.returns(a, mm), P.returns(b, mm)
        assert abs(P.path({'a': ra, 'b': rb}, {'a': .5, 'b': .5}, mm)[-1] - 1.0) < 1e-12
        assert abs(P.path({'a': ra, 'b': rb}, {'a': .5, 'b': .5}, mm, 'hold')[-1] - 1.01) < 1e-9

    def test_weights_sum_to_one_at_every_rung(self):
        P = self._m()
        for pct in P.WEIGHTS:
            b = pct / 100.0
            w = {k: v * (1 - b) for k, v in P.BASE.items()}
            w['btc'] = b
            assert abs(sum(w.values()) - 1.0) < 1e-12, pct

    def test_a_window_cannot_see_past_its_own_end(self):
        """The failure that would be invisible. Changing a month outside the
        window must not change the window's result."""
        import copy
        P = self._m()
        mm = [f'2020-{i:02d}' for i in range(1, 13)] + [f'2021-{i:02d}' for i in range(1, 13)]
        lv = {m: 100 * (1.01 ** i) for i, m in enumerate(mm)}
        assets = {'equities': dict(lv), 'gold': dict(lv), 'btc': dict(lv)}
        window = mm[:13]
        before, _ = P.build(assets, window, 10)
        later = copy.deepcopy(assets)
        for k in later:
            later[k][mm[20]] *= 3.0
        after, _ = P.build(later, window, 10)
        assert before == after, 'look-ahead: a future month changed a past window'

    def test_refuses_to_publish_on_a_tiny_sample(self):
        src = open(self._m().__file__, encoding='utf-8').read()
        assert 'len(months) < 60' in src, 'must refuse to publish on too few months'

    def test_refuses_a_partial_portfolio(self):
        """A missing leg used to be dropped silently, leaving the weights summing
        to 0.64 while the result was reported as a whole portfolio."""
        import pytest as _pt
        P = self._m()
        assets = {'equities': {'2020-01': 1.0, '2020-02': 1.0},
                  'btc': {'2020-01': 1.0, '2020-02': 1.0}}      # no gold
        with _pt.raises(ValueError):
            P.build(assets, ['2020-01', '2020-02'], 10)

    def test_monthly_aggregation_is_a_mean_not_a_last_value(self):
        """The equity and gold series are monthly AVERAGES; taking bitcoin's last
        daily price mixed two conventions and put the observations half a month
        apart. Verified against the data: sp500_monthly correlates 0.933 with the
        Nasdaq monthly average and 0.594 with its month-end close."""
        P = self._m()
        pts = [['2020-01-01', 10.0], ['2020-01-15', 20.0], ['2020-01-31', 30.0]]
        assert P.to_monthly(pts)['2020-01'] == 20.0, 'must be the mean, not 30.0'


class TestConfidenceInterval:
    """A resampling bootstrap collapses to "100% to 100%" when every episode is
    a hit, reporting certainty from five observations. The Wilson score interval
    is well behaved at the boundaries, which is why it is used instead."""

    def _wilson(self, hits, n, z=1.6449):
        import math
        p = hits / n
        d = 1 + z * z / n
        c = (p + z * z / (2 * n)) / d
        h = (z / d) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
        return max(0.0, c - h) * 100, min(1.0, c + h) * 100

    def test_all_hits_does_not_collapse_to_a_point(self):
        lo, hi = self._wilson(5, 5)
        assert hi == 100.0
        assert 55 < lo < 75, f'five for five should admit a rate near 65%, got {lo}'

    def test_no_hits_does_not_collapse_either(self):
        lo, hi = self._wilson(0, 5)
        assert lo == 0.0 and 25 < hi < 45

    def test_interval_narrows_as_episodes_grow(self):
        w5 = self._wilson(3, 5); w50 = self._wilson(30, 50)
        assert (w50[1] - w50[0]) < (w5[1] - w5[0]) / 2

    def test_interval_contains_the_observed_rate(self):
        for hits, n in ((1, 4), (5, 5), (0, 5), (12, 20), (7, 15)):
            lo, hi = self._wilson(hits, n)
            assert lo <= 100 * hits / n <= hi, (hits, n)

    def test_scorecard_publishes_the_interval(self):
        import os
        src = open(os.path.join(os.path.dirname(__file__), '..', 'fetch', 'scorecard.py'),
                   encoding='utf-8').read()
        assert 'hit_rate_ci90' in src
        assert 'Wilson' in src, 'the choice of interval must be explained in the module'


class TestNoFutureDates:
    """Coin Metrics drifted five days into the future and was served publicly,
    with freshness reporting 'current' the whole time because a negative age
    passes an "older than N days" test. Three defences now, each tested."""

    def _m(self):
        import fetch_all
        return fetch_all

    def test_future_dating_is_its_own_freshness_state(self):
        f = self._m().freshness('coinmetrics', '2999-01-01')
        assert f['freshness'] == 'future_dated', 'a negative age must not read as current'
        assert f['age_days'] < 0

    def test_normal_ages_still_classify(self):
        import datetime as dt
        m = self._m()
        today = dt.datetime.fromisoformat(m.NOW).date()
        assert m.freshness('coinmetrics', today.isoformat())['freshness'] == 'current'
        old = (today - dt.timedelta(days=30)).isoformat()
        assert m.freshness('coinmetrics', old)['freshness'] == 'stale'

    def test_save_truncates_future_dated_points(self):
        src = open(self._m().__file__, encoding='utf-8').read()
        assert 'future-dated points' in src, 'save() must drop points dated after today'

    def test_the_shift_happens_at_ingest_not_on_stored_files(self):
        """align.py mutating the stored file was the bug: save() rebuilds each
        document and drops the idempotency flag, so the shift reapplied forever."""
        src = open(self._m().__file__, encoding='utf-8').read()
        assert 'align.shift_series' in src or '_align.shift_series' in src, \
            'coinmetrics must be shifted on the raw rows at ingest'
        import align
        a = open(align.__file__, encoding='utf-8').read()
        i = a.find('def main(')
        assert '_shift_series(doc' not in a[i:], 'align.main must no longer shift stored files'

    def test_align_main_rejects_a_future_dated_file(self, tmp_path):
        import json, pytest as _pt, align
        (tmp_path / 'blockchain.json').write_text(json.dumps(
            {'series': {'price': [['2999-01-01', 1.0]]}}))
        align.OUT = str(tmp_path)
        with _pt.raises(ValueError):
            align.main()


class TestCensoringIsReachable:
    """The methodology says right-censored episodes are "counted separately".
    They were not counted at all: every eligible definition already required
    i + HORIZON < n, so an episode could never be censored and the counter was
    dead. Seven rules had fired inside the last year with no hint on the page."""

    def _src(self):
        import os
        return open(os.path.join(os.path.dirname(__file__), '..', 'fetch', 'scorecard.py'),
                    encoding='utf-8').read()

    def test_eligibility_no_longer_encodes_the_horizon(self):
        src = self._src()
        i = src.find('def main(')
        assert 'i + HORIZON < n' not in src[i:] and 'i+HORIZON<n' not in src[i:], \
            'eligibility must mean inputs present; score() applies the horizon'

    def test_score_still_applies_the_horizon(self):
        src = self._src()
        assert 'if i+HORIZON>=n: return None' in src, 'outcome() must still censor'

    def test_pending_episodes_are_dated(self):
        assert 'pending_since' in self._src()

    def test_rsi_series_is_not_shadowed_by_stock_to_flow(self):
        """`r` held the RSI series and was rebound inside the S2F loop. It
        worked only because RSI happened to be scored first."""
        src = self._src()
        assert 'sf_ratio' in src
        i = src.find('def main(')
        assert '\\n            r = b / flow' not in src[i:]


class TestConventionIsChecked:
    """align verified only that nothing was dated in the future. That would not
    notice the shift being WRONG: move the series back a day and no date is in
    the future, the calendar check passes, and the data is simply a day out."""

    def _m(self):
        import align
        return align

    def test_best_offset_finds_zero_when_aligned(self):
        import datetime as dt
        base = dt.date(2026, 1, 1)
        a = {(base + dt.timedelta(i)).isoformat(): 100.0 + i for i in range(120)}
        off, err = self._m().best_offset(a, dict(a))
        assert off == 0 and err < 1e-9

    def test_best_offset_returns_none_on_too_few_points(self):
        """Fewer than 30 overlapping days cannot settle a one-day question."""
        a = {f'2026-01-{d:02d}': 100.0 + d for d in range(1, 21)}
        assert self._m().best_offset(a, dict(a)) is None

    def test_best_offset_finds_the_shift_when_misaligned(self):
        import datetime as dt
        base = dt.date(2026, 1, 1)
        b = {(base + dt.timedelta(i)).isoformat(): 100.0 + i * 5 for i in range(120)}
        a = {(dt.date.fromisoformat(k) + dt.timedelta(1)).isoformat(): v for k, v in b.items()}
        off, _ = self._m().best_offset(a, b)
        assert off == -1, 'a series shifted forward must want shifting back'

    def test_main_rejects_a_wrong_convention(self, tmp_path):
        """The case the calendar check misses entirely."""
        import json, datetime as dt, pytest as _pt, align
        base = dt.date(2026, 1, 1)
        canon = {(base + dt.timedelta(i)).isoformat(): 100.0 + i * 3 for i in range(120)}
        wrong = {(base + dt.timedelta(i - 1)).isoformat(): 100.0 + i * 3 for i in range(120)}
        (tmp_path / 'blockchain.json').write_text(json.dumps(
            {'series': {'price': [[k, v] for k, v in sorted(canon.items())]}}))
        (tmp_path / 'coinmetrics.json').write_text(json.dumps(
            {'series': {'PriceUSD': [[k, v] for k, v in sorted(wrong.items())]}}))
        align.OUT = str(tmp_path)
        with _pt.raises(ValueError, match='convention'):
            align.main()


class TestEtfDatesAreNotInvented:
    """etf.py dated an undated scrape "today", so a Saturday run wrote a
    weekend row carrying Friday's figure and etf_flows reported age 0."""

    def _m(self):
        import etf
        return etf

    def test_fallback_date_is_a_business_day(self):
        import datetime as dt
        d = dt.date.fromisoformat(self._m().TODAY)
        assert d.weekday() < 5, 'the fallback must never be a Saturday or Sunday'

    def test_fallback_is_not_in_the_future(self):
        import datetime as dt
        assert dt.date.fromisoformat(self._m().TODAY) <= dt.date.today()

    def test_asserted_dates_are_recorded(self):
        m = self._m()
        assert hasattr(m, 'DATE_ASSERTED')
        src = open(m.__file__, encoding='utf-8').read()
        assert "'date_asserted'" in src, 'the published file must say which dates were inferred'

    def test_weekend_rows_are_dropped(self):
        """Two survived the last-business-day fix because merges preserve every
        date ever written. No issuer discloses at the weekend."""
        import datetime as dt
        m = self._m()
        s = {'BITB': [['2026-09-04', 1.0], ['2026-09-05', 1.0], ['2026-09-06', 1.0],
                      ['2026-09-07', 2.0]]}
        out = m._drop_weekend_rows(s)['BITB']
        assert [p[0] for p in out] == ['2026-09-04', '2026-09-07']
        for p in out:
            assert dt.date.fromisoformat(p[0]).weekday() < 5

    def test_align_publishes_its_fit(self):
        import align, os
        src = open(align.__file__, encoding='utf-8').read()
        assert 'align_report.json' in src, \
            'the convention fit must be published, not only logged'
        assert 'mean_abs_pct_at_zero' in src


class TestLpplsScorecardEntry:
    """The LPPL rule bypasses score() and reads the pipeline's own permutation
    baseline. Its claim page said "interval not computed" while the pipeline
    had already computed a random-signal range - a stronger instrument than
    Wilson - and had measured that price doubled after the signal MORE often
    than baseline. Both now travel through the scorecard."""

    def _src(self):
        import os
        return open(os.path.join(os.path.dirname(__file__), '..', 'fetch', 'scorecard.py'),
                    encoding='utf-8').read()

    def test_lppls_publishes_the_random_range_as_its_interval(self):
        src = self._src()
        i = src.find("key='lppls'")
        assert 'hit_rate_ci90' in src[i:i+2500], 'the random p5/p95 range must be published'
        assert 'interval_kind' in src[i:i+2500], 'and labelled as a permutation range, not Wilson'

    def test_lppls_publishes_doubling_after_signal(self):
        src = self._src()
        i = src.find("key='lppls'")
        assert 'doubling_after_signal' in src[i:i+2500]
        assert 'doubling_baseline' in src[i:i+2500]

    def test_lppls_reports_whether_the_strict_spec_was_scored(self):
        """Once hardcoded False; now derived from whether the pipeline
        actually produced a strict-spec baseline."""
        src = self._src()
        i = src.find("key='lppls'")
        assert 'strict_spec_scored=' in src[i:i+3000]
        assert 'strict_spec_scored=False' not in src[i:i+3000], 'must be derived, not asserted'


class TestLpplsThreeFurtherTests:
    """The strict spec, negative bubbles and the critical-time claim were all
    computed daily and published, and none was scored. Now all three are."""

    def _m(self):
        import lppls
        return lppls

    def test_random_baseline_accepts_a_direction(self):
        import inspect
        assert 'direction' in inspect.signature(self._m().random_baseline).parameters

    def test_critical_time_scores_per_run_not_per_day(self):
        """Per day, 522 correlated trials gave p = 0.000. Per run, 16 trials
        gave 1 hit. The per-day version was a false positive."""
        src = open(self._m().__file__, encoding='utf-8').read()
        i = src.find('def critical_time_test')
        body = src[i:]
        assert 'one trial per run' in body.lower() or 'per run' in body.lower()
        assert 'trials.append((r[0]' in body, 'must take one trial per run, at its first day'

    def test_critical_time_reports_several_tolerances(self):
        src = open(self._m().__file__, encoding='utf-8').read()
        assert 'for tol in (15, 30, 60)' in src

    def test_critical_time_on_synthetic_data(self):
        """A signal whose t_c always lands on the year's high must score 100%."""
        import datetime as dt, numpy as np
        m = self._m()
        n = 3000
        dates = [dt.date(2013, 1, 1) + dt.timedelta(i) for i in range(n)]
        prices = [100.0] * n
        pos, tc = [], []
        # ten runs; each run's first day has t_c = 50, and price spikes at exactly +50
        for k in range(10):
            s = 200 + k * 250
            prices[s + 50] = 1000.0
            for j in range(s, s + 5):
                pos.append([dates[j].isoformat(), 1.0])
                tc.append([dates[j].isoformat(), 50.0])
        out = m.critical_time_test(dates, prices, pos, tc, draws=200)
        assert out['trials'] == 10
        assert out['by_tolerance']['15']['hit_rate_signal'] == 1.0


class TestCapitalFlows:
    """The flow monitor's arithmetic must reconcile exactly and its supply
    schedule must reproduce reality. Every published figure on that page is
    read from this layer, so an error here is an error on the page."""

    def _m(self):
        import flows
        return flows

    def test_supply_schedule_reproduces_a_known_span(self):
        """From 20.0M on a known date the schedule must reach the pipeline's
        current supply to within 0.2%: 3.125 BTC x 144 blocks a day."""
        import datetime as dt
        m = self._m()
        s = m.supply_on(dt.date(2026, 9, 7), 19_900_000.0, dt.date(2026, 5, 1))
        # 129 days x 450 BTC/day = 58,050
        assert abs(s - (19_900_000 + 129 * 3.125 * 144)) < 1e-6

    def test_supply_schedule_halves_in_2028(self):
        import datetime as dt
        m = self._m()
        before = m.supply_on(dt.date(2028, 4, 14), 20_000_000.0, dt.date(2028, 4, 13))
        after = m.supply_on(dt.date(2028, 4, 16), 20_000_000.0, dt.date(2028, 4, 15))
        assert abs((before - 20_000_000) - 3.125 * 144) < 1e-6
        assert abs((after - 20_000_000) - 1.5625 * 144) < 1e-6

    def test_realised_cap_reconciles(self, tmp_path):
        """market cap / MVRV x MVRV must give market cap back, and market cap /
        supply must give price. These are the identities the page publishes."""
        import json
        m = self._m()
        days = [f'2024-01-{d:02d}' for d in range(1, 11)]
        cm = {'series': {'CapMrktCurUSD': [[d, 1.0e12 + i * 1e9] for i, d in enumerate(days)],
                         'CapMVRVCur': [[d, 1.5 + i * 0.01] for i, d in enumerate(days)],
                         'SplyCur': [[d, 19.6e6] for d in days],
                         'PriceUSD': [[d, (1.0e12 + i * 1e9) / 19.6e6] for i, d in enumerate(days)]}}
        dd, RC, MV, MC, SP, PR = m.realised_cap_series(cm)
        for d in dd:
            assert abs(RC[d] * MV[d] - MC[d]) < 1e-3
            assert abs(MC[d] / SP[d] - PR[d]) < 1e-6

    def test_surface_cell_is_the_stated_arithmetic(self):
        """price = (RC_now + inflow) x MVRV / supply, nothing else."""
        rc, inf, mvrv, sup = 1.07e12, 1.3e12, 1.7, 20.46e6
        assert abs((rc + inf) * mvrv / sup - 196_920.8) < 1.0

    def test_walk_forward_uses_only_completed_windows(self):
        """No training window may end after its origin."""
        src = open(self._m().__file__, encoding='utf-8').read()
        assert 'D[k] > od' in src, 'windows ending after the origin must be excluded'

    def test_holder_categories_come_from_primary_sources_only(self):
        src = open(self._m().__file__, encoding='utf-8').read()
        assert 'widely reported' in src.lower() or 'primary source' in src.lower()
        import treasuries
        t = open(treasuries.__file__, encoding='utf-8').read()
        assert 'addressbalance' in t, 'sovereign balance must be read on chain when one is listed'
        assert 'companyfacts' in t, 'treasury holdings must come from SEC XBRL'


class TestBaseRates:
    """The base-rate page is the denominator under every scorecard figure, so
    its outcome definitions must be IDENTICAL to the scorecard's."""

    def test_outcome_definitions_match_the_scorecard(self):
        import os
        b = open(os.path.join(os.path.dirname(__file__), '..', 'fetch', 'baserate.py'), encoding='utf-8').read()
        s = open(os.path.join(os.path.dirname(__file__), '..', 'fetch', 'scorecard.py'), encoding='utf-8').read()
        assert 'P[i] * 0.60' in b and 'px[i]*0.60' in s, 'a 40% fall is min <= 0.60 x start in both'
        assert 'P[i] * 2.0' in b and 'px[i]*2.0' in s, 'a doubling is max >= 2.0 x start in both'
        assert 'P[i + 1:i + 366]' in b and 'i+HORIZON+1' in s, 'both look at the 365 days AFTER the start day'

    def test_halving_dates_are_the_protocol_dates(self):
        import baserate, datetime as dt
        assert dt.date(2012, 11, 28) in baserate.HALVINGS
        assert dt.date(2016, 7, 9) in baserate.HALVINGS
        assert dt.date(2020, 5, 11) in baserate.HALVINGS
        assert dt.date(2024, 4, 20) in baserate.HALVINGS

    def test_months_since_halving(self):
        import baserate, datetime as dt
        m = baserate.months_since_halving(dt.date(2024, 10, 20))
        assert 5.9 < m < 6.1
        assert baserate.months_since_halving(dt.date(2012, 1, 1)) is None

    def test_synthetic_doubling_rate(self, tmp_path):
        """A series that doubles every 200 days must show a 100% doubling rate
        and a 0% fall rate within a year."""
        import json, datetime as dt, baserate
        n = 2000
        days = [(dt.date(2013, 1, 1) + dt.timedelta(i)).isoformat() for i in range(n)]
        px = [100.0 * (2 ** (i / 200)) for i in range(n)]
        (tmp_path / 'blockchain.json').write_text(json.dumps({'series': {'price': [[d, p] for d, p in zip(days, px)]}}))
        baserate.OUT = str(tmp_path)
        baserate.main()
        out = json.load(open(tmp_path / 'baserate.json'))
        assert out['outcomes']['double_within_365d']['rate_pct'] == 100.0
        assert out['outcomes']['fall_40pct_within_365d']['rate_pct'] == 0.0


class TestCrossAsset:
    """The cross-asset layer uses the scorecard's engine with a per-asset
    percentile outcome. Its arithmetic and its consistency check are tested."""

    def test_forward_return_uses_calendar_days(self):
        """Equities trade ~252 days a year. An index-based horizon would give
        them a 17-month window; the layer must use calendar days."""
        import crossasset, datetime as dt
        dates = [dt.date(2020, 1, 1) + dt.timedelta(days=2 * i) for i in range(400)]   # every other day
        px = [100.0 * (1.001 ** i) for i in range(400)]
        r = crossasset.forward_return(px, 0, dates)
        # 365 calendar days ahead is index ~183, not index 365
        assert abs(r - (px[183] / px[0] - 1)) < 1e-9

    def test_wilson_is_the_scorecard_wilson(self):
        import crossasset, scorecard
        # 5 of 5 must give the Pi Cycle interval the scorecard publishes: ~65-100
        ci = crossasset.wilson(5, 5)
        assert 60 < ci[0] < 70 and ci[1] == 100.0

    def test_outcome_definitions_are_percentile_cuts(self):
        import crossasset
        assert crossasset.PCT_HI == 0.80 and crossasset.PCT_LO == 0.20

    def test_pooled_beats_downgraded_when_an_asset_reverses(self):
        """A pooled 'beats' with one judged asset running the other way by
        more than five points must read 'Mixed', not 'Beats'."""
        src = open(__import__('crossasset').__file__, encoding='utf-8').read()
        assert "'Mixed: pooled result reverses in '" in src
        assert 'x * pooled_sign < -5' in src

    def test_synthetic_rule_that_always_works(self, tmp_path):
        """Construct one asset where every RSI<30 episode is followed by a
        top-quintile year; the layer must report a hit rate near 100% for it."""
        import json, datetime as dt, math, crossasset
        n = 2500
        dates = [(dt.date(2016, 1, 1) + dt.timedelta(days=i)).isoformat() for i in range(n)]
        # 40 days falling hard (drives RSI < 30), 100 days rising strongly, then
        # 590 flat. The rise follows ONLY the fall, so days at the fall are the
        # ones with a top-quintile forward year. A purely periodic series would
        # give every day the same forward return and no top quintile at all.
        px = []; v = 100.0
        for i in range(n):
            k = i % 730
            v *= 0.97 if k < 40 else (1.02 if k < 140 else 1.0)
            px.append(v)
        rel = {'series': {'btc_usd': [[d, p] for d, p in zip(dates, px)], 'eth_usd': [], 'sol_usd': [], 'nasdaq_daily': [], 'sp500_daily': []}}
        (tmp_path / 'relative.json').write_text(json.dumps(rel))
        crossasset.OUT = str(tmp_path)
        crossasset.main()
        out = json.load(open(tmp_path / 'crossasset.json'))
        btc = out['rules']['rsi_cold']['by_asset']['btc']
        # 2,500 days at a 730-day period: three complete cycles scoreable, the
        # fourth censored. Every scored episode must be a hit and the baseline
        # must sit far below - the constructed rule really works here.
        assert btc['episodes'] == 3 and btc['censored'] == 1
        assert btc['hit_rate'] == 100.0
        assert btc['baseline_rate'] < 25.0


class TestForwardRecord:
    """The ledger writes what the site said each day and scores it a year on;
    the register scores pre-registered rules through the scorecard engine."""

    def test_ledger_scores_a_row_once_it_is_a_year_old(self, tmp_path):
        import json, datetime as dt, ledger
        days = [(dt.date(2024, 1, 1) + dt.timedelta(i)).isoformat() for i in range(800)]
        px = [100.0 if i < 400 else 50.0 for i in range(800)]        # halves at day 400
        (tmp_path / 'blockchain.json').write_text(json.dumps({'series': {'price': [[d, p] for d, p in zip(days, px)]}}))
        (tmp_path / 'kpis.json').write_text(json.dumps({'as_of': days[-1], 'price_close': 50.0, 'powerlaw': {}, 'realised': {}}))
        (tmp_path / 'scorecard.json').write_text(json.dumps({'rules': [{'key': 'r_top', 'direction': 'top', 'firing_today': False, 'name': 'T'}]}))
        (tmp_path / 'ledger.json').write_text(json.dumps({'schema_version': '1.0', 'began': days[100], 'horizon_days': 365,
            'rows': [{'date': days[100], 'price': 100.0, 'firing': ['r_top'], 'composite_state': 'dear', 'scored': None}]}))
        ledger.OUT = str(tmp_path); ledger.main()
        L = json.load(open(tmp_path / 'ledger.json'))
        old = next(r for r in L['rows'] if r['date'] == days[100])
        assert old['scored'] is not None
        assert old['scored']['fell_40pct'] is True and old['scored']['doubled'] is False
        assert old['scored']['rules_right']['r_top'] is True
        assert L['per_rule']['r_top']['right'] == 1

    def test_ledger_never_rewrites_a_scored_row(self, tmp_path):
        import json, datetime as dt, ledger
        days = [(dt.date(2024, 1, 1) + dt.timedelta(i)).isoformat() for i in range(800)]
        (tmp_path / 'blockchain.json').write_text(json.dumps({'series': {'price': [[d, 100.0] for d in days]}}))
        (tmp_path / 'kpis.json').write_text(json.dumps({'as_of': days[-1], 'price_close': 100.0, 'powerlaw': {}, 'realised': {}}))
        (tmp_path / 'scorecard.json').write_text(json.dumps({'rules': []}))
        frozen = {'date': days[10], 'price': 1.0, 'firing': [], 'scored': {'on': 'x', 'fell_40pct': False, 'doubled': True, 'rules_right': {}}}
        (tmp_path / 'ledger.json').write_text(json.dumps({'schema_version': '1.0', 'began': days[10], 'horizon_days': 365, 'rows': [frozen]}))
        ledger.OUT = str(tmp_path); ledger.main()
        L = json.load(open(tmp_path / 'ledger.json'))
        assert next(r for r in L['rows'] if r['date'] == days[10])['scored']['on'] == 'x'

    def test_registry_uses_the_full_price_series(self):
        """A first version started at 2013 and lost early episodes to the
        moving-average warm-up. The register must read the whole series."""
        import registry
        src = open(registry.__file__, encoding='utf-8').read()
        assert 'FROM' not in src.split('def main')[1].split('pr = [')[0] or "d >= FROM" not in src

    def test_registry_scores_since_registration_separately(self):
        import registry
        src = open(registry.__file__, encoding='utf-8').read()
        assert 'elig_since' in src and "dates[i] >= reg" in src

    def test_registry_reproduces_a_scorecard_rule(self, tmp_path):
        import json, datetime as dt, registry, scorecard
        n = 3000
        days = [(dt.date(2015, 1, 1) + dt.timedelta(i)).isoformat() for i in range(n)]
        import math
        px = [100.0 * math.exp(0.0006 * i) * (1 + 0.3 * math.sin(i / 60.0)) for i in range(n)]
        (tmp_path / 'blockchain.json').write_text(json.dumps({'series': {'price': [[d, p] for d, p in zip(days, px)], 'hash_rate': []}}))
        (tmp_path / 'registry.json').write_text(json.dumps({'rules': [{'id': 'r', 'ind': 'rsi', 'op': 'below', 'thr': 30, 'dir': 'bottom', 'registered': days[0]}]}))
        registry.OUT = str(tmp_path); registry.main()
        out = json.load(open(tmp_path / 'registry_scores.json'))
        r = scorecard.rsi(px, 14)
        e = [x is not None for x in r]; f = [bool(e[i] and r[i] < 30) for i in range(n)]
        direct = scorecard.score(days, px, f, e, 'bottom')
        assert out['rules'][0]['full_history']['episodes'] == direct['episodes']
        assert abs((out['rules'][0]['full_history']['hit_rate'] or 0) - (direct['hit_rate'] or 0)) < 1e-9
