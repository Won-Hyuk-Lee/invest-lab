from dataclasses import replace
from datetime import date, timedelta

import pytest

from invest_lab.domain.indicators import analyze, rsi_wilder
from invest_lab.domain.models import Bar, Strategy


def history(prices):
    # Deliberately irregular dates: periods count observations, not calendar days.
    return [
        Bar(date(2025, 1, 1) + timedelta(days=i * 3), p, p, p, p, 100) for i, p in enumerate(prices)
    ]


def test_returns_use_n_plus_one_observations_and_moving_averages_use_n():
    result = analyze("005930", history(list(range(100, 230))), Strategy())
    for n in (7, 30, 90):
        assert result.returns[str(n)] == pytest.approx((229 / (229 - n) - 1) * 100)
    assert result.moving_averages["120"] == pytest.approx((110 + 229) / 2)


def test_history_warmup_never_fills_missing_data_with_zero():
    result = analyze("005930", history([100] * 7), Strategy())
    assert result.returns["7"] is None
    assert result.moving_averages["7"] == 100
    assert result.moving_averages["120"] is None
    assert result.rsi is None
    assert result.score is None
    assert "insufficient" in result.tags


def test_score_starts_after_121_observations():
    assert analyze("005930", history([100] * 120), Strategy()).score is None
    result = analyze("005930", history([100] * 121), Strategy(review_threshold=10))
    assert result.score == 15
    assert "review" in result.tags


@pytest.mark.parametrize(
    "prices, expected",
    [
        ([100] * 20, 50),
        (list(range(100, 120)), 100),
        (list(range(120, 100, -1)), 0),
        # First 14 changes: seven +1 and seven -1; next +2 uses Wilder smoothing.
        ([100, 101] * 7 + [100, 102], 56.6666666667),
    ],
)
def test_wilder_rsi(prices, expected):
    assert rsi_wilder(prices) == pytest.approx(expected)


def test_unchanged_close_breaks_decline_streak():
    result = analyze("005930", history([100, 95, 95, 94, 93, 92]), Strategy())
    assert result.down_streak == 3
    assert "declining" in result.tags


def test_volume_ratio_excludes_today_from_baseline():
    bars = history([100] * 21)
    bars[-1] = replace(bars[-1], volume=200)
    assert analyze("005930", bars, Strategy()).volume_ratio == 2


def test_rebound_is_a_specific_condition_not_just_low_rsi():
    bars = history(list(range(300, 171, -1)) + [173])
    bars[-1] = replace(bars[-1], volume=120)
    assert "rebound" in analyze("005930", bars, Strategy()).tags
    bars[-1] = replace(bars[-1], volume=119)
    assert "rebound" not in analyze("005930", bars, Strategy()).tags


@pytest.mark.parametrize(
    "bad",
    [
        lambda bars: list(reversed(bars)),
        lambda bars: bars + [bars[-1]],
        lambda bars: [replace(bars[0], close=float("nan"))],
        lambda bars: [replace(bars[0], low=101)],
        lambda bars: [replace(bars[0], volume=-1)],
    ],
)
def test_invalid_market_data_is_rejected(bad):
    with pytest.raises(ValueError):
        analyze("005930", bad(history([100, 101])), Strategy())


def test_analysis_only_consumes_given_history():
    bars = history(list(range(100, 260)))
    prefix = bars[:130]
    result = analyze("005930", prefix, Strategy())
    bars[150] = replace(bars[150], close=1, low=1)
    assert analyze("005930", bars[:130], Strategy()) == result


def test_strategy_rejects_invalid_weight_sum():
    with pytest.raises(ValueError, match="합계"):
        Strategy(weights={"oversold": 31, "pullback": 25, "trend": 25, "rebound": 20})
