import math
from statistics import mean

from invest_lab.domain.models import Analysis, Bar, Strategy


def rsi_wilder(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gain = mean(max(change, 0) for change in changes[:period])
    loss = mean(max(-change, 0) for change in changes[:period])
    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, 0)) / period
        loss = (loss * (period - 1) + max(-change, 0)) / period
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return 100 - 100 / (1 + gain / loss)


def validate_bars(bars: list[Bar]) -> None:
    for i, bar in enumerate(bars):
        prices = (bar.open, bar.high, bar.low, bar.close)
        if not all(math.isfinite(value) and value > 0 for value in prices):
            raise ValueError("가격은 양수이며 유한해야 합니다.")
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            raise ValueError("OHLC 가격 범위를 확인하세요.")
        if type(bar.volume) is not int or bar.volume < 0:
            raise ValueError("거래량은 0 이상의 정수여야 합니다.")
        if i and bars[i - 1].session >= bar.session:
            raise ValueError("거래일은 중복 없이 오름차순이어야 하며 거래량은 음수일 수 없습니다.")


def analyze(symbol: str, bars: list[Bar], strategy: Strategy) -> Analysis:
    validate_bars(bars)
    closes = [bar.close for bar in bars]
    close = closes[-1] if closes else None
    averages = {
        str(days): mean(closes[-days:]) if len(closes) >= days else None
        for days in (7, 30, 90, 120)
    }
    returns = {
        str(days): (closes[-1] / closes[-days - 1] - 1) * 100 if len(closes) > days else None
        for days in (1, 7, 30, 90)
    }
    distances = {
        days: (close / value - 1) * 100 if close is not None and value else None
        for days, value in averages.items()
    }
    rsi = rsi_wilder(closes)
    down_streak = 0
    for current, previous in zip(reversed(closes[1:]), reversed(closes[:-1])):
        if current >= previous:
            break
        down_streak += 1
    baseline_volume = mean(b.volume for b in bars[-21:-1]) if len(bars) >= 21 else 0
    volume_ratio = bars[-1].volume / baseline_volume if baseline_volume > 0 else None
    tags: list[str] = []
    if rsi is not None:
        if rsi <= strategy.oversold_rsi:
            tags.append("oversold")
        if rsi >= strategy.overbought_rsi:
            tags.append("overbought")
    if down_streak >= 3:
        tags.append("declining")
    enough = len(bars) >= 121
    previous_rsi = rsi_wilder(closes[:-1])
    rebound = bool(
        previous_rsi is not None
        and previous_rsi <= strategy.oversold_rsi + 5
        and len(closes) > 1
        and closes[-1] > closes[-2]
        and volume_ratio is not None
        and volume_ratio >= 1.2
    )
    if rebound:
        tags.append("rebound")
    components: list[dict] = []
    score = None
    if enough:
        ma30, ma90, ma120 = (averages[str(n)] for n in (30, 90, 120))
        uptrend = close > ma30 > ma90 > ma120
        downtrend = close < ma30 < ma90 < ma120
        if uptrend:
            tags.append("uptrend")
        elif downtrend:
            tags.append("downtrend")
        else:
            tags.append("mixed")
        trend_points = sum((close > ma90, close > ma120, ma30 > ma90, ma90 > ma120)) * 25
        values = {
            "oversold": max(
                0.0,
                min(
                    100.0,
                    (strategy.overbought_rsi - rsi)
                    / (strategy.overbought_rsi - strategy.oversold_rsi)
                    * 100,
                ),
            ),
            "pullback": max(0.0, min(100.0, -distances["30"] / 10 * 100)),
            "trend": float(trend_points),
            "rebound": 100.0 if rebound else 0.0,
        }
        labels = {
            "oversold": "RSI 과매도",
            "pullback": "30일선 대비 눌림",
            "trend": "중장기 추세",
            "rebound": "반등 조건",
        }
        components = [
            {
                "key": key,
                "label": labels[key],
                "value": round(value, 2),
                "weight": strategy.weights[key],
                "contribution": round(value * strategy.weights[key] / 100, 2),
            }
            for key, value in values.items()
        ]
        score = round(sum(values[k] * strategy.weights[k] / 100 for k in values), 2)
        if score >= strategy.review_threshold:
            tags.insert(0, "review")
    else:
        tags.append("insufficient")
    return Analysis(
        symbol,
        bars[-1].session if bars else None,
        len(bars),
        close,
        returns,
        averages,
        distances,
        rsi,
        down_streak,
        volume_ratio,
        tags,
        score,
        components,
        strategy.version,
    )
