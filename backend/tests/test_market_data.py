import io
import zipfile
from datetime import datetime
from decimal import Decimal

import httpx
import pytest

from invest_lab.adapters.kis import BrokerDataError
from invest_lab.adapters.market_data import (
    CachedMarket,
    MarketDataError,
    MarketIngestion,
    MarketMasterDownloader,
    MarketStore,
    parse_kis_master,
    parse_krx_master_csv,
)

MASTER_TIME = "2026-09-20T00:00:00+00:00"
CLOSE_TIME = "2026-09-25T06:30:00+00:00"
COLLECT_TIME = "2026-09-25T07:00:00+00:00"


class Clock:
    def __init__(self, at=COLLECT_TIME):
        self.at = datetime.fromisoformat(at)

    def now(self):
        return self.at


def bar(session="2026-09-25", close="105", **kwargs):
    return {
        "session": session,
        "open": "100",
        "high": "110",
        "low": "90",
        "close": close,
        "volume": 1000,
        "turnover": "102000",
        **kwargs,
    }


@pytest.fixture
def store(tmp_path):
    store = MarketStore(tmp_path / "fixture-market.db")
    store.import_instruments(
        [
            {
                "symbol": "005930",
                "name": "fixture-stock",
                "kind": "kospi",
                "sector": "test",
                "listed_on": "2026-01-01",
            },
            {"symbol": "0048K0", "name": "fixture-etf", "kind": "etf_kr", "sector": "test"},
        ],
        source="fixture-master",
        as_of=MASTER_TIME,
    )
    store.import_calendar(
        [
            {
                "session": "2026-09-25",
                "is_open": True,
                "open_at": "2026-09-25T09:00:00+09:00",
                "close_at": "2026-09-25T15:30:00+09:00",
            },
            {"session": "2026-09-26", "is_open": False},
            {"session": "2026-09-27", "is_open": False},
            {
                "session": "2026-09-28",
                "is_open": True,
                "open_at": "2026-09-28T09:00:00+09:00",
                "close_at": "2026-09-28T15:30:00+09:00",
            },
        ],
        source="reviewed-calendar-fixture",
        as_of=MASTER_TIME,
    )
    return store


def test_missing_calendar_does_not_infer_weekdays_or_weekends(store):
    assert store.is_open("2026-09-25T10:00:00+09:00")
    assert not store.is_open("2026-09-25T15:30:00+09:00")
    assert not store.is_open("2026-09-26T10:00:00+09:00")
    with pytest.raises(MarketDataError, match="coverage is missing"):
        store.is_open("2026-09-29T10:00:00+09:00")
    assert store.next_session("2026-09-25")["session"] == "2026-09-28"


def test_master_is_point_in_time_and_preserves_product_codes(store):
    assert store.instruments(as_of="2026-09-19T23:59:59+00:00") == []
    assert [row["symbol"] for row in store.instruments()] == ["0048K0", "005930"]
    store.import_instruments(
        [{"symbol": "005930", "name": "renamed", "kind": "kospi"}],
        source="fixture-master",
        as_of="2026-09-26T00:00:00+00:00",
    )
    assert store.instruments(as_of=COLLECT_TIME)[1]["name"] == "fixture-stock"
    assert store.instruments(as_of="2026-09-27T00:00:00+00:00")[1]["name"] == "renamed"


def test_raw_adjusted_and_revisions_are_distinct_and_point_in_time(store):
    for adjusted, close in ((False, "105"), (True, "101")):
        store.import_bars(
            "005930",
            [bar(close=close)],
            adjusted=adjusted,
            source="fixture",
            available_at=COLLECT_TIME,
            recorded_at=COLLECT_TIME,
        )
    store.import_bars(
        "005930",
        [bar(close="102")],
        adjusted=True,
        source="fixture",
        available_at="2026-09-26T07:00:00+00:00",
        recorded_at="2026-09-26T08:00:00+00:00",
    )
    assert store.history("005930", adjusted=False, as_of=COLLECT_TIME)[0]["close"] == "105"
    assert store.history("005930", adjusted=True, as_of=COLLECT_TIME)[0]["close"] == "101"
    revised = store.history("005930", adjusted=True, as_of="2026-09-26T07:30:00+00:00")
    assert revised[0]["close"] == "102"
    replay = store.history(
        "005930", adjusted=True, as_of="2026-09-26T07:30:00+00:00", replay_ingestion=True
    )
    assert replay[0]["close"] == "101"
    assert revised[0]["checksum"] != replay[0]["checksum"]


@pytest.mark.parametrize(
    "changes",
    [
        {"close": "NaN"},
        {"open": "Infinity"},
        {"volume": 1.5},
        {"volume": True},
        {"low": "106"},
        {"high": "99"},
        {"close": "0"},
        {"session": "2026-09-28"},
        {"symbol": "0048K0"},
        {"venue": "NXT"},
    ],
)
def test_invalid_history_import_is_atomic(store, changes):
    with pytest.raises((MarketDataError, BrokerDataError)):
        store.import_bars(
            "005930",
            [bar(**changes)],
            source="fixture",
            available_at=COLLECT_TIME,
            recorded_at=COLLECT_TIME,
        )
    assert store.history("005930", adjusted=False) == []


def test_partial_or_unknown_session_cannot_be_finalized(store):
    with pytest.raises(MarketDataError, match="confirmed closed"):
        store.import_bars("005930", [bar()], source="fixture", available_at="2026-09-25T05:00:00Z")
    with pytest.raises(MarketDataError, match="Calendar coverage"):
        store.import_bars(
            "005930", [bar("2026-09-24")], source="fixture", available_at=COLLECT_TIME
        )
    store.import_bars(
        "005930",
        [bar("2026-09-24")],
        source="unreviewed-import",
        available_at=COLLECT_TIME,
        require_calendar=False,
    )
    assert store.history("005930", adjusted=False) == []
    assert len(store.history("005930", adjusted=False, finalized_only=False)) == 1


def test_quote_cache_preserves_observation_time_and_rejects_older_overwrite(store):
    clock = Clock("2026-09-25T09:00:00+00:00")
    store.put_quote(
        {"symbol": "0048K0", "price": "105", "source": "kis_paper", "observed_at": COLLECT_TIME}
    )
    store.put_quote(
        {"symbol": "0048K0", "price": "99", "source": "kis_paper", "observed_at": CLOSE_TIME}
    )
    market = CachedMarket(store, clock)
    quote = market.quote("0048K0")
    assert quote.price == Decimal("105")
    assert quote.observed_at == datetime.fromisoformat(COLLECT_TIME)
    assert quote.observed_at != clock.now()
    with pytest.raises(MarketDataError):
        market.quote("005930")


def test_calendar_corrections_are_versioned(store):
    store.import_calendar(
        [{"session": "2026-09-28", "is_open": False}],
        source="reviewed-calendar-fixture",
        as_of="2026-09-27T00:00:00Z",
    )
    assert store.calendar("2026-09-28", as_of=MASTER_TIME)["is_open"]
    assert not store.calendar("2026-09-28", as_of="2026-09-28T00:00:00Z")["is_open"]


def test_missing_bar_is_not_zero_volume_or_weekend(store):
    store.import_bars(
        "005930",
        [bar(volume=0)],
        source="fixture",
        adjusted=True,
        available_at=COLLECT_TIME,
        recorded_at=COLLECT_TIME,
    )
    coverage = store.coverage("005930", "2026-09-25", "2026-09-29")
    assert coverage["present"] == 1
    assert coverage["missing_sessions"] == ["2026-09-28"]
    assert coverage["unknown_calendar"] == ["2026-09-29"]
    assert not coverage["complete"]


def test_ui_cached_reads_do_not_require_client_or_make_network_requests(store):
    store.import_bars(
        "005930",
        [bar()],
        source="fixture",
        adjusted=True,
        available_at=COLLECT_TIME,
        recorded_at=COLLECT_TIME,
    )
    market = CachedMarket(store, Clock())
    assert len(market.instruments()) == 2
    assert market.history("005930")[0].close == 105
    assert market.history("0048K0") == []


def test_ingestion_does_not_advance_checkpoint_over_missing_open_session(store):
    class Client:
        source = "fixture"

        def __init__(self):
            self.requests = []

        def bars(self, symbol, start, end, adjusted=False):
            self.requests.append((start, end))
            return []

    client = Client()
    ingestion = MarketIngestion(client, store, Clock())
    outcome = ingestion.refresh_history("005930", "2026-09-25", "2026-09-27")
    assert outcome["status"] == "incomplete"
    state = store.checkpoint("bars:005930:1:2026-09-25:2026-09-27")
    assert state["next_date"] == "2026-09-25"


def test_krx_csv_requires_explicit_etf_geography_and_keeps_alphanumeric_codes():
    text = (
        "ISU_SRT_CD,ISU_ABBRV,MKT_NM,LIST_DD\n"
        "005930,stock,KOSPI,20260101\n0048K0,ETF,ETF,20260101\n"
    )
    with pytest.raises(MarketDataError, match="classification"):
        parse_krx_master_csv(text)
    rows = parse_krx_master_csv(text, classifications={"0048K0": "etf_us"})
    assert rows[1]["symbol"] == "0048K0" and rows[1]["kind"] == "etf_us"
    assert rows[0]["listed_on"] == "2026-01-01"


def test_kis_cp949_master_prefix_offsets_and_newline_width():
    prefix = "0048K0   " + "KR70048K0000" + "한글 ETF 이름"
    suffix = "EF" + " " * 225
    raw = (prefix + suffix + "\r\n").encode("cp949")
    with pytest.raises(MarketDataError, match="classification"):
        parse_kis_master(raw)
    row = parse_kis_master(raw, classifications={"0048K0": "etf_kr"})[0]
    assert row["symbol"] == "0048K0"
    assert row["name"] == "한글 ETF 이름"
    assert row["isin"] == "KR70048K0000"


def test_calendar_import_validates_all_rows_before_writing(store):
    with pytest.raises(MarketDataError):
        store.import_calendar(
            [
                {"session": "2026-09-29", "is_open": False},
                {
                    "session": "2026-09-30",
                    "is_open": True,
                    "open_at": "2026-09-30T09:00:00",
                    "close_at": "2026-09-30T15:30:00",
                },
            ],
            source="fixture",
            as_of=MASTER_TIME,
        )
    with pytest.raises(MarketDataError):
        store.calendar("2026-09-29")


def archive_bytes(member, body):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, body)
    return result.getvalue()


def test_public_master_download_fixed_url_immutable_artifact_and_incomplete_etf_review(store):
    rows = (
        "005930   KR7005930003테스트 주식"
        + "ST"
        + " " * 225
        + "\n"
        + "0048K0   KR70048K0000테스트 ETF"
        + "EF"
        + " " * 225
        + "\n"
    )
    archive = archive_bytes("kospi_code.mst", rows.encode("cp949"))
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == MarketMasterDownloader.URL
        assert "appkey" not in request.headers and "authorization" not in request.headers
        return httpx.Response(200, content=archive)

    downloader = MarketMasterDownloader(
        store,
        transport=httpx.MockTransport(handler),
        now=lambda: datetime.fromisoformat(COLLECT_TIME),
    )
    result = downloader.download_kis_kospi()
    assert result["status"] == "needs_classification"
    assert result["unclassified_etfs"][0]["symbol"] == "0048K0"
    assert not result["historical_universe_complete"]
    path = store.path.parent / "market_sources" / (result["artifact_id"] + ".zip")
    assert path.read_bytes() == archive
    complete = downloader.download_kis_kospi(classifications={"0048K0": "etf_us"})
    assert complete["status"] == "complete" and complete["supported_rows"] == 2
    assert complete["artifact_id"] == result["artifact_id"]
    assert len(requests) == 2


@pytest.mark.parametrize(
    "member,body",
    [("../outside.mst", b"unsafe"), ("kospi_code.mst", b"A" * 100000)],
    ids=["path-traversal", "compression-bomb"],
)
def test_public_master_zip_path_and_bomb_are_rejected_without_extraction(store, member, body):
    archive = archive_bytes(member, body)
    downloader = MarketMasterDownloader(
        store, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=archive))
    )
    with pytest.raises(MarketDataError):
        downloader.download_kis_kospi()
    assert not (store.path.parent / "outside.mst").exists()


def test_public_master_redirect_is_not_followed(store):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://untrusted.invalid/archive.zip"})

    downloader = MarketMasterDownloader(store, transport=httpx.MockTransport(handler))
    with pytest.raises(MarketDataError, match="download failed"):
        downloader.download_kis_kospi()
    assert len(requests) == 1
