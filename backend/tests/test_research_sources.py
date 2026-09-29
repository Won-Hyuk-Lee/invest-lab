import io
import zipfile

import httpx
import pytest

from invest_lab.adapters.research_sources import (
    DartSource,
    FredSource,
    KrxSource,
    ResearchSourceError,
    conservative_available_at,
    import_records,
    parse_xbrl_facts,
    safe_xml,
    unzip_documents,
)


def test_source_errors_never_echo_query_credentials():
    key = "never-print-this-test-key"
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(403, json={"error": str(request.url)})
        )
    )
    with pytest.raises(ResearchSourceError) as exc:
        FredSource(key, client=client).observations(
            "VIXCLS", "2023-01-01", "2023-01-02", ["2023-01-03"]
        )
    assert key not in str(exc.value)


def test_source_day_policy_handles_dst_and_korean_sessions():
    assert (
        conservative_available_at("2023-03-10", "America/Chicago", ["2023-03-13", "2023-03-14"])
        == "2023-03-13T00:00:00+00:00"
    )
    assert (
        conservative_available_at("2023-03-12", "America/Chicago", ["2023-03-13", "2023-03-14"])
        == "2023-03-14T00:00:00+00:00"
    )


def test_fred_preserves_vintage_and_business_missing_value():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "count": 2,
                "observations": [
                    {
                        "date": "2023-01-02",
                        "value": "20",
                        "realtime_start": "2023-01-03",
                        "realtime_end": "2023-01-04",
                    },
                    {
                        "date": "2023-01-02",
                        "value": ".",
                        "realtime_start": "2023-01-05",
                        "realtime_end": "9999-12-31",
                    },
                ],
            },
        )

    source = FredSource("test-key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = source.observations(
        "VIXCLS",
        "2023-01-01",
        "2023-01-06",
        ["2023-01-04", "2023-01-05", "2023-01-06", "2023-01-09"],
    )
    assert result["records"][0]["vintage_date"] == "2023-01-03"
    assert result["records"][1]["value"] is None
    assert seen[0].url.params["realtime_start"] == "1776-07-04"
    assert "test-key" not in str(result)


def test_dart_includes_original_and_corrected_filings_and_conservative_dates():
    requests = []

    def handler(request):
        requests.append(request)
        page = request.url.params["page_no"]
        return httpx.Response(
            200,
            json={
                "status": "000",
                "total_page": 2,
                "list": [
                    {
                        "rcept_no": "2023010200000" + page,
                        "rcept_dt": "20230102",
                        "report_nm": "보고서",
                    }
                ],
            },
        )

    source = DartSource("key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = source.filings("00000001", "2023-01-01", "2023-01-03", ["2023-01-02", "2023-01-03"])
    assert len(result["records"]) == 2
    assert requests[0].url.params["last_reprt_at"] == "N"
    assert result["records"][0]["available_at"] == "2023-01-03T00:00:00+00:00"


def test_dart_latest_table_never_claims_point_in_time_original():
    source = DartSource(
        "key",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"status": "000", "list": []})
            )
        ),
    )
    assert source.latest_financials("x", 2023, "11011")["revised_only"] is True


def test_dart_large_company_directory_retains_receipt_xml_limit():
    content = (
        b"<result>"
        + b" " * 20_000_000
        + b"<list><corp_code>00126380</corp_code><stock_code>005930</stock_code>"
        b"</list></result>"
    )
    with pytest.raises(ResearchSourceError, match="unsafe_or_oversized"):
        safe_xml(content)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("CORPCODE.xml", content)
    source = DartSource(
        "test-key",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=archive.getvalue())
            )
        ),
    )
    assert source.companies() == [{"corp_code": "00126380", "stock_code": "005930"}]
    with pytest.raises(ResearchSourceError, match="unsafe_or_oversized"):
        safe_xml(b"<!DOCTYPE x><x/>", max_bytes=50_000_000)


def test_krx_code_is_string_and_missing_release_clock_is_not_pit_verified():
    def handler(request):
        assert request.headers["AUTH_KEY"] == "key"
        return httpx.Response(
            200,
            json={
                "OutBlock_1": [
                    {
                        "ISU_CD": "0048K0",
                        "TDD_OPNPRC": "100",
                        "TDD_HGPRC": "102",
                        "TDD_LWPRC": "99",
                        "TDD_CLSPRC": "101",
                        "ACC_TRDVOL": "20",
                        "ACC_TRDVAL": "2020",
                    }
                ]
            },
        )

    source = KrxSource("key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = source.daily("etf_daily", "2023-01-02", ingested_at="2023-01-03T00:00:00+00:00")
    assert result["records"][0]["symbol"] == "0048K0"
    assert not result["historical_availability_verified"]


def test_import_preserves_leading_zero_and_boolean_contract():
    rows = import_records(
        "symbol,final,volume\n0048K0,true,100\n005930,false,0\n", format="csv", table="bars"
    )
    assert rows[0]["symbol"] == "0048K0"
    assert rows[1]["symbol"] == "005930"
    assert rows[1]["final"] is False


def test_xml_disallows_entity_payload_and_zip_stays_in_memory():
    with pytest.raises(ResearchSourceError):
        safe_xml(b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///local-secret">]><x>&e;</x>')
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("../../escape.xml", "<root />")
    docs = unzip_documents(output.getvalue())
    assert docs["../../escape.xml"] == b"<root />"


def test_xbrl_extracts_receipt_context_and_rejects_segment_values():
    content = b"""<xbrl xmlns="http://www.xbrl.org/2003/instance"
    xmlns:ifrs="urn:ifrs" xmlns:d="urn:dim">
    <context id="total"><entity><identifier scheme="x">A</identifier></entity>
    <period><startDate>2023-01-01</startDate>
    <endDate>2023-03-31</endDate></period></context>
    <context id="segment"><entity><segment>
    <d:explicitMember dimension="BusinessSegmentsAxis">part</d:explicitMember></segment></entity>

    <period><startDate>2023-01-01</startDate>
    <endDate>2023-03-31</endDate></period></context>
    <unit id="KRW"><measure>iso4217:KRW</measure></unit>
    <ifrs:Revenue contextRef="total" unitRef="KRW">100</ifrs:Revenue>
    <ifrs:Revenue contextRef="segment" unitRef="KRW">500</ifrs:Revenue></xbrl>"""
    result = parse_xbrl_facts(
        {"a.xbrl": content},
        symbol="005930",
        receipt="20230501000001",
        scope="CFS",
        available_at="2023-05-02T00:00:00+00:00",
        ingested_at="2023-05-02T00:00:00+00:00",
    )
    assert len(result["records"]) == 1
    assert result["records"][0]["value"] == "100.0"
    assert result["records"][0]["period_type"] == "quarter"
    assert result["omitted"]
