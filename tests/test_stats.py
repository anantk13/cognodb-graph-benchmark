"""Tests for the statistics the report publishes.

These matter more than they look. The percentile code decides what number goes
in the README, and an earlier version of it shipped a bug: `minimum_samples_for`
and `percentile` disagreed about the minimum sample size for a bounded p95,
because one of them omitted the `+1` on the upper order statistic. The two now
share `_ci_ranks`, and `test_minimum_is_exactly_the_boundary` is the test that
would have caught it.
"""

from __future__ import annotations

import pytest

from gbench.report.stats import minimum_samples_for, percentile, summarise


class TestPercentile:
    def test_nearest_rank_returns_a_real_observation(self) -> None:
        """A percentile is a sample, never an interpolation between two.

        Interpolating implementations differ between libraries; a rank is
        unambiguous and a reader can point at the observation it names.
        """
        samples = [float(i) for i in range(1, 101)]
        result = percentile(samples, 0.95)
        assert result.value == 95.0
        assert result.rank == 95
        assert result.value in samples

    def test_unsorted_input(self) -> None:
        assert percentile([5.0, 1.0, 3.0, 2.0, 4.0], 0.5).value == 3.0

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="zero samples"):
            percentile([], 0.5)

    @pytest.mark.parametrize("quantile", [0.0, 1.0, -0.1, 1.5])
    def test_rejects_quantile_outside_the_open_interval(self, quantile: float) -> None:
        with pytest.raises(ValueError, match="quantile"):
            percentile([1.0], quantile)


class TestConfidenceInterval:
    @pytest.mark.parametrize(("quantile", "expected"), [(0.5, 8), (0.95, 110), (0.99, 563)])
    def test_known_minimums(self, quantile: float, expected: int) -> None:
        """The thresholds the harness and the README both quote.

        110 for p95 is the number that establishes that the commonly suggested
        "at least 100 iterations per read workload" does not close the interval.
        """
        assert minimum_samples_for(quantile) == expected

    @pytest.mark.parametrize("quantile", [0.5, 0.95, 0.99])
    def test_minimum_is_exactly_the_boundary(self, quantile: float) -> None:
        """At n the interval closes; at n-1 it does not.

        This is the regression test for the bug described in the module
        docstring: it fails if the two functions ever drift apart again.
        """
        n = minimum_samples_for(quantile)
        assert percentile([float(i) for i in range(n)], quantile).is_bounded
        assert not percentile([float(i) for i in range(n - 1)], quantile).is_bounded

    def test_small_sample_refuses_to_claim_an_upper_bound(self) -> None:
        result = percentile([float(i) for i in range(40)], 0.95)
        assert result.ci_high is None
        assert not result.is_bounded

    def test_interval_tightens_as_n_grows(self) -> None:
        """Wider in absolute rank, narrower as a fraction -- which is the point."""
        thousand = percentile([float(i) for i in range(1000)], 0.95)
        ten_thousand = percentile([float(i) for i in range(10000)], 0.95)
        assert thousand.ci_width / 1000 > ten_thousand.ci_width / 10000

    def test_interval_brackets_the_value(self) -> None:
        result = percentile([float(i) for i in range(1000)], 0.95)
        assert result.ci_low <= result.value <= result.ci_high


class TestSummary:
    def test_has_no_p99(self) -> None:
        """p99 must be structurally absent, not merely omitted from the report.

        This client is closed-loop, which distorts p99 by 20x or worse. Leaving
        the field off the type means no future code can publish one by
        accident.
        """
        summary = summarise([1.0, 2.0, 3.0])
        assert not hasattr(summary, "p99")
        assert "p99_ms" not in summary.as_dict()

    def test_mean_is_published_alongside_percentiles(self) -> None:
        """Omitting the mean is its own form of cherry-picking.

        One vendor benchmark published a 120x p99 win while the competitor it
        named had won the mean, p50, p90 and p95.
        """
        keys = summarise([1.0, 2.0, 3.0]).as_dict()
        for key in ("mean_ms", "stdev_ms", "p50_ms", "p95_ms"):
            assert key in keys

    def test_mean_hides_what_the_tail_exposes(self) -> None:
        """The reason percentiles are reported at all."""
        summary = summarise([5.0] * 990 + [2000.0] * 10)
        assert summary.mean < 30.0  # one stall barely moves it
        assert summary.maximum == 2000.0  # but it definitely happened

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="zero samples"):
            summarise([])
