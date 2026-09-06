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
