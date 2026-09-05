"""Tests for fetch/scorecard.py.

The scorecard publishes verdicts about widely believed claims. If the scoring
is wrong the site is confidently wrong in public, which is worse than silent.

Every expectation below is hand-constructed: a synthetic price series whose
answer is known before the code runs.
"""
import os, sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'fetch'))
import scorecard as sc


class TestMovingAverage:
    def test_window_not_emitted_until_full(self):
        out = sc.sma([1., 2., 3., 4., 5.], 3)
        assert out[0] is None and out[1] is None
        assert out[2] == pytest.approx(2.0)      # mean(1,2,3)
        assert out[4] == pytest.approx(4.0)      # mean(3,4,5)

    def test_flat_series(self):
        assert sc.sma([7.0] * 5, 2)[4] == pytest.approx(7.0)


class TestRSI:
    def test_monotonic_rise_pins_at_one_hundred(self):
        out = sc.rsi([float(i) for i in range(1, 40)], 14)
        assert out[-1] > 99.0, 'no down moves means RSI must be ~100'

    def test_monotonic_fall_pins_at_zero(self):
        out = sc.rsi([float(i) for i in range(40, 1, -1)], 14)
        assert out[-1] < 1.0

    def test_warmup_period_is_empty(self):
        out = sc.rsi([float(i) for i in range(30)], 14)
        assert all(v is None for v in out[:14])
        assert out[14] is not None


class TestScoring:
    """score() is the heart of it: episodes, outcomes, and the baseline."""

    def _dates(self, n):
        import datetime as dt
        d0 = dt.date(2020, 1, 1)
        return [(d0 + dt.timedelta(i)).isoformat() for i in range(n)]

    def test_a_signal_that_always_precedes_a_crash_scores_one_hundred(self):
        n = 1200
        px = [100.0] * n
        # a 50% crash on day 400, well inside the 365-day window from day 100
        for i in range(400, n): px[i] = 50.0
        fires = [i == 100 for i in range(n)]
        elig = [True] * n
        r = sc.score(self._dates(n), px, fires, elig, 'top')
        assert r['episodes'] == 1
        assert r['hit_rate'] == 100.0

    def test_clustered_triggers_count_as_one_episode(self):
        n = 1000
        px = [100.0] * n
        fires = [200 <= i <= 260 for i in range(n)]   # 61 consecutive days
        r = sc.score(self._dates(n), px, fires, [True] * n, 'top')
        assert r['episodes'] == 1, 'a run of triggers is one occasion, not 61'

    def test_triggers_beyond_the_cluster_window_are_separate(self):
        n = 1400
        px = [100.0] * n
        fires = [i in (100, 100 + sc.CLUSTER + 5) for i in range(n)]
        r = sc.score(self._dates(n), px, fires, [True] * n, 'top')
        assert r['episodes'] == 2

    def test_right_censored_episodes_are_excluded_and_counted(self):
        n = 500
        px = [100.0] * n
        fires = [i == 480 for i in range(n)]     # no 365 days left after it
        r = sc.score(self._dates(n), px, fires, [True] * n, 'top')
        assert r['episodes'] == 0
        assert r['censored'] == 1, 'must be counted, not silently dropped'

    def test_baseline_excludes_the_aftermath_of_a_trigger(self):
        """The transition-matched rule. Days just after a signal are unusual
        precisely because a signal fired; leaving them in flatters the test."""
        n = 1200
        px = [100.0] * n
        fires = [i == 300 for i in range(n)]
        elig = [True] * n
        r = sc.score(self._dates(n), px, fires, elig, 'top')
        eligible_with_window = n - sc.HORIZON
        # one trigger plus CLUSTER following days are removed from the baseline
        assert r['baseline_days'] < eligible_with_window
        assert r['baseline_days'] >= eligible_with_window - (sc.CLUSTER + 2)

    def test_ineligible_days_never_enter_either_side(self):
        n = 1200
        px = [100.0] * n
        elig = [i >= 600 for i in range(n)]
        r = sc.score(self._dates(n), px, [False] * n, elig, 'top')
        assert r['eligible_days'] == sum(1 for i in range(n) if elig[i])
        assert r['baseline_days'] <= r['eligible_days']

    def test_top_outcome_needs_a_forty_percent_fall(self):
        n = 1200
        px = [100.0] * n
        for i in range(200, n): px[i] = 65.0          # only a 35% fall
        r = sc.score(self._dates(n), px, [i == 100 for i in range(n)], [True] * n, 'top')
        assert r['hit_rate'] == 0.0, '35% must not count as a 40% fall'

    def test_bottom_outcome_needs_a_doubling(self):
        n = 1200
        px = [100.0] * n
        for i in range(200, n): px[i] = 199.0         # just short of double
        r = sc.score(self._dates(n), px, [i == 100 for i in range(n)], [True] * n, 'bottom')
        assert r['hit_rate'] == 0.0
        px2 = [100.0] * n
        for i in range(200, n): px2[i] = 201.0
        r2 = sc.score(self._dates(n), px2, [i == 100 for i in range(n)], [True] * n, 'bottom')
        assert r2['hit_rate'] == 100.0

    def test_outcome_only_looks_forward(self):
        """A crash BEFORE the signal must not be credited to it."""
        n = 1200
        px = [100.0] * n
        for i in range(50, 200): px[i] = 20.0        # crash and full recovery, before the signal
        r = sc.score(self._dates(n), px, [i == 400 for i in range(n)], [True] * n, 'top')
        assert r['hit_rate'] == 0.0, 'past falls must not count'
