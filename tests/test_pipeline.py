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
