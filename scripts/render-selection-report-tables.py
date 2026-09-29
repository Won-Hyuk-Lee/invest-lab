"""Regenerate numeric report tables; the dated narrative still requires human review."""

import json
from pathlib import Path

from invest_lab.adapters.etf_store import FileEtfStore
from invest_lab.domain.auto_models import MODELS
from invest_lab.domain.selection_models import SELECTION_MODELS
from invest_lab.selection_cli import engine_revision


def main():
    root = Path(".local/selection-research")
    rev = engine_revision()[:12]
    suite = json.loads((root / f"suite-{rev}.json").read_text(encoding="utf-8"))
    audit = json.loads((root / f"audit-{rev}.json").read_text(encoding="utf-8"))
    store = FileEtfStore(root)
    labels = {k: v.label for k, v in {**MODELS, **SELECTION_MODELS}.items()}
    labels.update(
        hold_qual="QUAL 장기 보유", sector_pullback="업종 추세 70 + 단기 RSI 30"
    )
    lines = []

    def pct(value):
        return f"{100 * value:.2f}%"

    def pair(row):
        m = row["metrics"]
        return f"{pct(m['cagr_krw'])} / {pct(m['max_drawdown_krw'])}"

    for cohort, label in (
        ("sector", "미국 업종"),
        ("global", "세계 자산"),
        ("style", "투자 스타일"),
    ):
        selected = [
            r
            for r in suite["rows"]
            if r["cohort"] == cohort and r["scenario"] == "base"
        ]
        lines += [
            f"## {label} — {selected[0]['start']}~{selected[0]['end']}",
            "",
            "| 모델 | 원화 연복리 | 원화 최대 낙폭 | USD 최대 낙폭 | 평균 ETF 비중 | 매매 건수 |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for r in selected:
            m = r["metrics"]
            lines.append(
                f"| {labels[r['model']]} | {pct(m['cagr_krw'])} | {pct(m['max_drawdown_krw'])} | {pct(m['max_drawdown_usd'])} | {pct(m['mean_exposure'])} | {m['trade_count']} |"
            )
        lines.append("")
    lines += [
        "## 비용·체결·현금 통화",
        "",
        "각 칸은 `원화 연복리 / 원화 최대 낙폭`. 비용 4배는 모든 편도 수수료·슬리피지·환전 가정을 4배 적용한다.",
        "",
        "| 모델/기간 | 기본 | 비용 4배 | 체결 1일 지연 | KRW 현금 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for cohort, model in (
        ("sector", "sector_rank"),
        ("sector", "sector_trend"),
        ("sector", "sector_pullback"),
        ("global", "global_trend"),
        ("global", "global_risk"),
        ("global", "global_shy"),
        ("global", "asset_rotation"),
        ("style", "style_rank"),
        ("style", "quality_momentum"),
        ("style", "hold_spy"),
    ):
        selected = {
            r["scenario"]: r
            for r in suite["rows"]
            if r["cohort"] == cohort and r["model"] == model
        }
        columns = [
            pair(selected[s]) for s in ("base", "cost_4x", "delay_1", "krw_cash")
        ]
        lines.append(
            f"| {labels[model]} ({selected['base']['start'][:4]}~) | "
            + " | ".join(columns)
            + " |"
        )
    lines += [
        "",
        "SHY 방어는 기본 위험 배분보다 수익 증가가 작고 비용이 늘었다. 위험 예산으로 남긴 현금을 다시 단기국채로 채우는 것이 항상 유리하지는 않았다.",
        "",
        "## 위험 배분 요소 분리",
        "",
        "아래는 각 후보군의 기본 위험 배분을 기준으로 일부 설정만 변경한 진단이다. 기본값을 사후 최고값으로 교체하지 않았다. `역변동성만`은 목표 변동성 축소를 끄고 월말에만 갱신한다. 따라서 주별 갱신 제거 효과도 포함하며 순수한 단일 변수 인과효과로 해석하지 않는다.",
        "",
        "| 후보군 | 기본 12%·주별 | 목표 축소 없이 역변동성 | 동일 비중+변동성 | 12%·월별 | 10%·주별 | 15%·주별 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for cohort in ("sector", "global", "style"):
        rows = {
            r["scenario"]: r
            for r in suite["rows"]
            if r["cohort"] == cohort and r["model"] == f"{cohort}_risk"
        }
        columns = [
            pair(rows[s])
            for s in (
                "base",
                "inverse_vol_only",
                "equal_weight_vol",
                "monthly_risk",
                "vol_10pct",
                "vol_15pct",
            )
        ]
        lines.append(f"| {cohort} | " + " | ".join(columns) + " |")
    lines += [
        "",
        "## 구간별 재시작 비교",
        "",
        "각 구간마다 초기자금을 새로 투입한 비교다. 하나의 장기 계좌를 잘라낸 값과 다르다. `연복리 / 낙폭`이며, 최근 구간도 미사용 테스트가 아니다.",
        "",
    ]
    for cohort in ("sector", "global", "style"):
        models = [
            "hold_spy",
            "hold_qqq",
            "long_trend",
            f"{cohort}_rank",
            f"{cohort}_trend",
            f"{cohort}_risk",
        ]
        if cohort == "style":
            models.append("quality_momentum")
        periods = list(
            dict.fromkeys(
                r["scenario"]
                for r in suite["rows"]
                if r["cohort"] == cohort and r["scenario"].startswith("period_")
            )
        )
        lines += [
            f"### {cohort}",
            "",
            "| 모델 | "
            + " | ".join(p.removeprefix("period_").replace("_", "~") for p in periods)
            + " |",
            "| --- | " + " | ".join("---:" for _ in periods) + " |",
        ]
        for model in models:
            rows = {
                r["scenario"]: r
                for r in suite["rows"]
                if r["cohort"] == cohort and r["model"] == model
            }
            lines.append(
                f"| {labels[model]} | "
                + " | ".join(pair(rows[p]) for p in periods)
                + " |"
            )
        lines.append("")
    lines += [
        "## 차이가 우연일 가능성에 대한 진단",
        "",
        "월별 원화 로그수익 차이를 12개월 블록으로 1,000회 재표본했다(고정 난수 시드). 미완료 가능성이 있는 마지막 달은 제외했다. 값은 CAGR의 단순 차이가 아니라 **연율 상대 성장률**이며, 95% 범위는 모델 탐색 횟수를 보정하지 않은 탐색용이다. 아래 범위가 모두 0을 포함하므로 안정적인 초과수익을 확인했다고 말할 근거는 부족하다.",
        "",
        "| 비교 | 연율 상대 성장률 | 재표본 95% 범위 |",
        "| --- | ---: | ---: |",
    ]
    for c in audit["comparisons"]:
        if (c["model"], c["benchmark"]) in (
            ("sector_rank", "hold_spy"),
            ("global_trend", "global_equal"),
            ("style_rank", "hold_spy"),
            ("quality_momentum", "hold_spy"),
        ):
            lo, hi = c["block12_interval_95"]
            lines.append(
                f"| {labels[c['model']]} / {labels[c['benchmark']]} | {pct(c['annual_relative_growth'])} | {pct(lo)}~{pct(hi)} |"
            )
    factor_labels = {
        "market": "미국 시장",
        "profit_high": "영업수익성 상위 30%",
        "profit_low": "영업수익성 하위 30%",
        "value_high": "장부가/시가 상위 30%",
        "value_low": "장부가/시가 하위 30%",
        "profit_value": "고수익성·가치 50:50",
        "profit_trend": "고수익성+추세",
        "profit_value_trend": "고수익성·가치+추세",
        "market_trend": "시장+추세",
    }
    lines += [
        "",
        "## 재무 특성: 별도 월별 진단",
        "",
        "출처가 구성한 시가총액 가중 포트폴리오 수익을 사용한다. **실제 매매 가능한 개별 주식 전략 성과가 아니며, 내부 회전 비용·세금은 미확인이다.** 기본 외부 교체 비용 7.5bp, 추가 연간 비용 0% 조건이다. 월별 낙폭은 일별 낙폭보다 작게 나타날 수 있다.",
        "",
        "| 모델 | 1965-01~2026-08 USD 연복리 / 월말 낙폭 | 2001-02~2026-08 KRW 연복리 / 월말 낙폭 |",
        "| --- | ---: | ---: |",
    ]
    for model, label in factor_labels.items():
        vals = []
        for start, currency in (("1965-01", "usd"), ("2001-02", "krw")):
            r = next(
                r
                for r in suite["factor_rows"]
                if r["model"] == model and r["start"] == start and r["annual_drag"] == 0
            )
            m = r["metrics"][currency]
            vals.append(f"{pct(m['cagr'])} / {pct(m['drawdown_monthly'])}")
        lines.append(f"| {label} | " + " | ".join(vals) + " |")
    lines += [
        "",
        "고수익성 포트폴리오의 1965년 이후 USD 연복리는 추가 연간 비용 1%를 가정하면 10.99%, 2%면 9.87%다. 이 추가 비용은 실측 수수료가 아니며 내부 매매/구현 오차에 대한 민감도다. 전체 JSON에는 시장 기준선에도 같은 비용을 적용한 결과를 포함했다. 공개 연구 특성의 효과와 실제 구현 가능한 순수익은 구분한다.",
        "",
    ]
    # Keep per-year and measured volatility/recovery information in the portable result JSON.
    for row in suite["rows"]:
        if row["scenario"] == "base":
            row["years"] = store.load_run(row["run_id"])["metrics"]["years"]
    suite["verification"] = {k: v for k, v in audit.items() if k != "raw"}
    Path("docs/selection-research-results-2026-09-29.json").write_text(
        json.dumps(suite, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    path = Path("docs/selection-research-results-2026-09-29.md")
    text = path.read_text(encoding="utf-8")
    begin, end = "<!-- GENERATED_TABLES -->", "<!-- END_GENERATED_TABLES -->"
    if end in text:
        text = (
            text[: text.index(begin)]
            + begin
            + "\n\n"
            + "\n".join(lines)
            + end
            + text[text.index(end) + len(end) :]
        )
    else:
        text = text.replace(begin, begin + "\n\n" + "\n".join(lines) + end)
    path.write_text(text, encoding="utf-8")
    print(
        f"Rendered {len(suite['rows'])} ETF + {len(suite['factor_rows'])} factor comparisons"
    )


if __name__ == "__main__":
    main()
