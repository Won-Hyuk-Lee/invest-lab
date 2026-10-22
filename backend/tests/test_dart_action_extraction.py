import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "prepare_dart_actions", Path(__file__).resolve().parents[2] / "scripts/prepare_dart_actions.py"
)
parser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parser)


def document(name, rows):
    return {
        "filing": {
            "report_nm": name,
            "stock_code": "011300",
            "rcept_no": "20241212000630",
            "rcept_dt": "20241212",
        },
        "collected_at": "2026-09-29T00:00:00Z",
        "documents": {
            "report.xml": "<table>"
            + "".join(
                "<tr>" + "".join("<td>" + value + "</td>" for value in row) + "</tr>"
                for row in rows
            )
            + "</table>"
        },
    }


def test_tandem_reduction_and_split_remain_distinct_events():
    common = [["이사회결의일", "2024-12-12"], ["신주상장예정일", "2025-02-27"]]
    reduction = parser.parse_split(
        document(
            "감자결정",
            common
            + [
                ["감자비율", "보통주식 (%)", "75"],
                ["감자기준일", "2025-02-03"],
                ["감자방법", "보통주 4주를 동일 액면가의 보통주 1주로 무상병합"],
            ],
        )
    )
    split = parser.parse_split(
        document(
            "주식분할결정",
            common
            + [
                ["1주당 가액(원)", "500", "100"],
                ["신주의 효력발생일", "2025-02-15"],
            ],
        )
    )
    assert reduction["ratio"] * split["ratio"] == 1.25
    assert reduction["effective_date"] == "2025-02-04"
    assert reduction["event_id"] != split["event_id"]
    assert parser.event_identity(reduction, "2025-02-27") != parser.event_identity(
        split, "2025-02-27"
    )


def test_court_reductions_keep_distinct_phases_and_exact_unrounded_ratio():
    events = []
    for phase, denominator in ((1, 27), (2, 20)):
        events.append(
            parser.parse_split(
                document(
                    "감자결정",
                    [
                        ["이사회결의일", "2026-06-26"],
                        ["신주상장예정일", "2026-09-01"],
                        ["감자기준일", "2026-06-30"],
                        ["감자비율", "보통주식 (%)", "96.30"],
                        [
                            "감자방법",
                            f"제{phase}차 자본감소-보통주에 대하여 {denominator}:1 병합"
                            "-단주는 무상 소각",
                        ],
                    ],
                )
            )
        )
    assert events[0]["ratio"] == 1 / 27
    assert events[1]["ratio"] == 1 / 20
    assert events[0]["event_id"] != events[1]["event_id"]
    assert events[0]["fractional_policy"] == "cancel_without_compensation"
