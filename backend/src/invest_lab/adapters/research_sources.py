"""Read-only official data adapters. Keys are supplied by the composition root only.

No broker requests, environment loading, hidden authentication, or silent synthetic fallback.
"""

import csv
import io
import json
import re
import zipfile
from datetime import date, datetime, time, timedelta, timezone
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import httpx

from invest_lab.domain.research import canonical, content_hash, finite


class ResearchSourceError(RuntimeError):
    pass


def conservative_available_at(day: str, source_timezone: str, sessions: list[str]) -> str:
    after = datetime.combine(
        date.fromisoformat(day) + timedelta(days=1), time(), ZoneInfo(source_timezone)
    ).astimezone(timezone.utc)
    for session in sessions:
        opening = datetime.combine(date.fromisoformat(session), time(), timezone.utc)
        if opening >= after:
            return opening.isoformat()
    raise ValueError("공개일 이후 거래 세션이 필요합니다.")


class OfficialSource:
    provider = "official"
    base_url = ""

    def __init__(self, api_key: str, *, client: httpx.Client | None = None):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("공급자 API 키가 필요합니다.")
        self._key = api_key
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self._owns_client = client is None

    def close(self):
        if self._owns_client:
            self.client.close()

    def _get(self, path, *, params=None, headers=None, binary=False):
        try:
            response = self.client.get(self.base_url + path, params=params, headers=headers)
            if response.status_code != 200:
                raise ResearchSourceError(f"{self.provider}: HTTP {response.status_code}")
            if len(response.content) > 50_000_000:
                raise ResearchSourceError(f"{self.provider}: response_too_large")
            return response.content if binary else response.json()
        except ResearchSourceError:
            raise
        except (httpx.HTTPError, ValueError):
            # Do not expose request URLs: DART/FRED put credentials in query strings.
            raise ResearchSourceError(f"{self.provider}: transport_or_response_error") from None


class FredSource(OfficialSource):
    provider = "fred"
    base_url = "https://api.stlouisfed.org/fred/"
    SERIES = {"VIXCLS", "VXNCLS", "DEXKOUS", "DGS2", "DGS10", "BAMLH0A0HYM2"}

    def observations(
        self, series: str, start: str, end: str, sessions: list[str], *, ingested_at=None
    ) -> dict:
        if series not in self.SERIES:
            raise ValueError("허용한 매크로 시리즈가 아닙니다.")
        date.fromisoformat(start)
        date.fromisoformat(end)
        ingested = ingested_at or datetime.now(timezone.utc).isoformat()
        records, raw_pages, offset = [], [], 0
        seen = set()
        for _ in range(1000):
            body = self._get(
                "series/observations",
                params={
                    "api_key": self._key,
                    "file_type": "json",
                    "series_id": series,
                    "observation_start": start,
                    "observation_end": end,
                    "realtime_start": "1776-07-04",
                    "realtime_end": "9999-12-31",
                    "output_type": 1,
                    "limit": 10000,
                    "offset": offset,
                },
            )
            if "error_code" in body or not isinstance(body.get("observations"), list):
                raise ResearchSourceError("fred: invalid_observations_response")
            rows = body["observations"]
            page_hash = content_hash(rows)
            if rows and page_hash in seen:
                raise ResearchSourceError("fred: repeated_page")
            seen.add(page_hash)
            raw_pages.append({k: v for k, v in body.items() if k != "api_key"})
            for row in rows:
                vintage = row["realtime_start"]
                if vintage == "1776-07-04":
                    # Unknown first vintage cannot be presented as ancient known information.
                    records.append(
                        {
                            "series": series,
                            "observation_date": row["date"],
                            "vintage_date": vintage,
                            "value": None,
                            "available_at": ingested,
                            "ingested_at": ingested,
                            "missing_reason": "unknown_first_vintage",
                            "source": self.provider,
                        }
                    )
                    continue
                try:
                    available = conservative_available_at(vintage, "America/Chicago", sessions)
                except ValueError:
                    # Later revisions are retained with a conservative source-day boundary.
                    available = (
                        datetime.combine(
                            date.fromisoformat(vintage) + timedelta(days=1),
                            time(),
                            ZoneInfo("America/Chicago"),
                        )
                        .astimezone(timezone.utc)
                        .isoformat()
                    )
                records.append(
                    {
                        "series": series,
                        "observation_date": row["date"],
                        "vintage_date": vintage,
                        "realtime_end": row.get("realtime_end"),
                        "value": None if row["value"] == "." else finite(row["value"]),
                        "available_at": available,
                        "ingested_at": ingested,
                        "source": self.provider,
                        "availability_policy": "source_day_end_then_krx_session",
                        "time_precision": "date",
                    }
                )
            offset += len(rows)
            if offset >= int(body.get("count", offset)) or not rows:
                break
        else:
            raise ResearchSourceError("fred: pagination_limit_exceeded")
        return {
            "provider": self.provider,
            "table": "macro",
            "records": records,
            "raw": raw_pages,
            "hash": content_hash(raw_pages),
            "coverage": {"series": series, "requested_start": start, "requested_end": end},
            "real_validation_complete": False,
        }

    def vintages(self, series):
        if series not in self.SERIES:
            raise ValueError("지원하지 않는 시리즈입니다.")
        rows, offset = [], 0
        for _ in range(100):
            body = self._get(
                "series/vintagedates",
                params={
                    "api_key": self._key,
                    "file_type": "json",
                    "series_id": series,
                    "limit": 10000,
                    "offset": offset,
                },
            )
            page = body.get("vintage_dates")
            if not isinstance(page, list):
                raise ResearchSourceError("fred: invalid_vintage_response")
            rows.extend(page)
            offset += len(page)
            if offset >= int(body.get("count", offset)) or not page:
                return sorted(set(rows))
        raise ResearchSourceError("fred: pagination_limit_exceeded")


def safe_xml(content: bytes, *, max_bytes: int = 20_000_000):
    if (
        len(content) > max_bytes
        or b"\x00" in content
        or b"<!DOCTYPE" in content.upper()
        or b"<!ENTITY" in content.upper()
    ):
        raise ResearchSourceError("xml: unsafe_or_oversized_document")
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        raise ResearchSourceError("xml: parse_error") from None


def unzip_documents(content: bytes) -> dict[str, bytes]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
        if (
            len(archive.infolist()) > 500
            or sum(i.file_size for i in archive.infolist()) > 50_000_000
        ):
            raise ResearchSourceError("dart: archive_too_large")
        result = {}
        for item in archive.infolist():
            # Never extract remote paths to the filesystem.
            if item.is_dir():
                continue
            if item.filename.lower().endswith((".xml", ".xbrl")):
                result[item.filename] = archive.read(item)
        return result
    except (zipfile.BadZipFile, RuntimeError):
        raise ResearchSourceError("dart: invalid_archive") from None


class DartSource(OfficialSource):
    provider = "dart"
    base_url = "https://opendart.fss.or.kr/api/"

    def _json(self, name, params):
        result = self._get(name + ".json", params={"crtfc_key": self._key, **params})
        status = result.get("status")
        if status == "013":
            return {**result, "list": []}
        if status != "000":
            # Only the numeric business status is safe to surface.
            raise ResearchSourceError("dart: business_status_" + str(status)[:3])
        return result

    def companies(self):
        documents = unzip_documents(
            self._get("corpCode.xml", params={"crtfc_key": self._key}, binary=True)
        )
        rows = []
        for content in documents.values():
            # The official complete company directory exceeds 20 MB. Keep receipt
            # documents at the smaller default and retain the archive's 50 MB cap.
            for item in safe_xml(content, max_bytes=50_000_000).findall("list"):
                row = {child.tag: child.text or "" for child in item}
                if row.get("stock_code", "").strip():
                    rows.append(row)
        return rows

    def filings(self, corp_code, start, end, sessions, *, ingested_at=None):
        ingested = ingested_at or datetime.now(timezone.utc).isoformat()
        records, seen, raw = [], set(), []
        for page in range(1, 10001):
            body = self._json(
                "list",
                {
                    "corp_code": corp_code,
                    "bgn_de": start.replace("-", ""),
                    "end_de": end.replace("-", ""),
                    "last_reprt_at": "N",
                    "page_no": page,
                    "page_count": 100,
                },
            )
            raw.append(body)
            for row in body["list"]:
                receipt = row["rcept_no"]
                if receipt in seen:
                    raise ResearchSourceError("dart: repeated_receipt_or_page")
                seen.add(receipt)
                day = datetime.strptime(row["rcept_dt"], "%Y%m%d").date().isoformat()
                records.append(
                    {
                        **row,
                        "source_published_date": day,
                        "available_at": conservative_available_at(day, "Asia/Seoul", sessions),
                        "ingested_at": ingested,
                        "availability_policy": "next_krx_session_after_disclosure_date",
                        "time_precision": "date",
                    }
                )
            if not body["list"] or page >= int(body.get("total_page", page)):
                return {
                    "provider": self.provider,
                    "records": records,
                    "raw": raw,
                    "hash": content_hash(raw),
                }
        raise ResearchSourceError("dart: pagination_limit_exceeded")

    def original(self, receipt, *, report_code=None):
        if not re.fullmatch(r"\d{14}", receipt):
            raise ValueError("접수번호 형식을 확인하세요.")
        path = "fnlttXbrl.xml" if report_code else "document.xml"
        params = {"crtfc_key": self._key, "rcept_no": receipt}
        if report_code:
            if report_code not in {"11011", "11012", "11013", "11014"}:
                raise ValueError("보고서 코드를 확인하세요.")
            params["reprt_code"] = report_code
        return unzip_documents(self._get(path, params=params, binary=True))

    def latest_financials(self, corp_code, year, report_code, scope="CFS"):
        if scope not in {"CFS", "OFS"}:
            raise ValueError("연결/별도 범위를 확인하세요.")
        body = self._json(
            "fnlttSinglAcntAll",
            {
                "corp_code": corp_code,
                "bsns_year": str(year),
                "reprt_code": report_code,
                "fs_div": scope,
            },
        )
        return {
            "provider": self.provider,
            "records": body["list"],
            "raw": body,
            "pit_ready": False,
            "revised_only": True,
            "reason": "endpoint_not_addressed_by_receipt",
        }

    def financial_facts(
        self,
        receipt,
        symbol,
        filing_date,
        sessions,
        report_code,
        *,
        scope="CFS",
        ingested_at=None,
        account_mapping=None,
    ):
        """Receipt-addressed original facts; mapping omissions remain visible to coverage review."""
        import base64

        if scope not in {"CFS", "OFS"}:
            raise ValueError("연결/별도 범위를 확인하세요.")
        ingested = ingested_at or datetime.now(timezone.utc).isoformat()
        available = conservative_available_at(filing_date, "Asia/Seoul", sessions)
        documents = self.original(receipt, report_code=report_code)
        result = parse_xbrl_facts(
            documents,
            symbol=symbol,
            receipt=receipt,
            scope=scope,
            available_at=available,
            ingested_at=ingested,
            account_mapping=account_mapping,
        )
        result["raw_documents"] = {
            name: base64.b64encode(content).decode("ascii") for name, content in documents.items()
        }
        result["raw_encoding"] = "base64"
        result["raw_hash"] = content_hash(result["raw_documents"])
        result["availability_policy"] = "next_krx_session_after_disclosure_date"
        result["provider"] = self.provider
        result["requires_coverage_review"] = True
        return result


XBRL_ACCOUNTS = {
    "Revenue": "revenue",
    "RevenueFromContractsWithCustomers": "revenue",
    "ProfitLossFromOperatingActivities": "operating_income",
    "OperatingIncomeLoss": "operating_income",
    "ProfitLoss": "net_income",
    "CashFlowsFromUsedInOperatingActivities": "operating_cash_flow",
    "Equity": "equity",
    "Liabilities": "liabilities",
    "Assets": "assets",
}


def parse_xbrl_facts(
    documents: dict[str, bytes],
    *,
    symbol,
    receipt,
    scope,
    available_at,
    ingested_at,
    account_mapping=None,
) -> dict:
    """Extract only unambiguous contexts; unsupported dimensions are reported, not guessed."""
    mapping = {**XBRL_ACCOUNTS, **(account_mapping or {})}
    facts, omitted = [], []
    for filename, content in documents.items():
        root = safe_xml(content)
        contexts, units = {}, {}
        for node in root:
            local = node.tag.rsplit("}", 1)[-1]
            if local == "context":
                period = next((n for n in node if n.tag.endswith("}period")), None)
                if period is None:
                    continue
                dates = {n.tag.rsplit("}", 1)[-1]: n.text for n in period}
                dimensions = [
                    n
                    for n in node.iter()
                    if n.tag.endswith("}explicitMember") or n.tag.endswith("}typedMember")
                ]
                # Consolidation dimensions may be present. Other breakdowns aren't entity totals.
                unsafe = [
                    d
                    for d in dimensions
                    if "ConsolidatedAndSeparate" not in d.attrib.get("dimension", "")
                ]
                texts = " ".join((d.text or "") for d in dimensions)
                detected_scope = (
                    "OFS"
                    if "Separate" in texts or "NonConsolidated" in texts
                    else "CFS"
                    if "Consolidated" in texts
                    else scope
                )
                contexts[node.attrib["id"]] = {
                    "dates": dates,
                    "unsupported": bool(unsafe),
                    "scope": detected_scope,
                }
            elif local == "unit":
                units[node.attrib["id"]] = [
                    n.text for n in node.iter() if n.tag.endswith("}measure")
                ]
        for node in root:
            name = node.tag.rsplit("}", 1)[-1]
            if name not in mapping or "contextRef" not in node.attrib:
                continue
            context = contexts.get(node.attrib["contextRef"])
            if not context or context["unsupported"] or context["scope"] != scope:
                omitted.append(
                    {
                        "file": filename,
                        "concept": name,
                        "reason": "unsupported_or_incompatible_context",
                    }
                )
                continue
            measure = units.get(node.attrib.get("unitRef"), [])
            if len(measure) != 1 or not measure[0].endswith("KRW"):
                omitted.append({"file": filename, "concept": name, "reason": "unsupported_unit"})
                continue
            if node.text is None:
                continue
            dates = context["dates"]
            instant = dates.get("instant")
            start, end = dates.get("startDate"), dates.get("endDate")
            if not instant and (not start or not end):
                continue
            period_type = (
                "instant"
                if instant
                else "quarter"
                if 70 <= (date.fromisoformat(end) - date.fromisoformat(start)).days <= 100
                else "ytd"
            )
            facts.append(
                {
                    "symbol": symbol,
                    "receipt_id": receipt,
                    "account": mapping[name],
                    "original_concept": node.tag,
                    "scope": scope,
                    "attribution": "total",
                    "currency": "KRW",
                    "period_type": period_type,
                    "period_start": start or instant,
                    "period_end": end or instant,
                    "value": str(finite(node.text)),
                    "available_at": available_at,
                    "ingested_at": ingested_at,
                    "source": "dart_receipt_xbrl",
                    "source_file": filename,
                    "context_ref": node.attrib["contextRef"],
                }
            )
    # Multiple concepts mapping to the same account/period require explicit mapping resolution.
    groups = {}
    for fact in facts:
        key = (fact["account"], fact["period_start"], fact["period_end"], fact["scope"])
        groups.setdefault(key, []).append(fact)
    accepted = []
    for items in groups.values():
        if len({f["value"] for f in items}) > 1:
            omitted.append(
                {
                    "account": items[0]["account"],
                    "period_end": items[0]["period_end"],
                    "reason": "ambiguous_mapped_account",
                }
            )
        else:
            accepted.append(items[0])
    return {
        "table": "financial_facts",
        "records": accepted,
        "omitted": omitted,
        "receipt_id": receipt,
        "mapping_version": content_hash(mapping),
        "complete": not omitted and bool(accepted),
    }


class KrxSource(OfficialSource):
    provider = "krx"
    base_url = "https://data-dbg.krx.co.kr/svc/apis/"
    SERVICES = {
        "kospi_daily": "sto/stk_bydd_trd",
        "kospi_master": "sto/stk_isu_base_info",
        "etf_daily": "etp/etf_bydd_trd",
        "kospi_index": "idx/kospi_dd_trd",
    }

    def daily(self, service, session, *, available_at=None, ingested_at=None):
        if service not in self.SERVICES:
            raise ValueError("허용된 KRX 서비스가 아닙니다.")
        date.fromisoformat(session)
        body = self._get(
            self.SERVICES[service],
            params={"basDd": session.replace("-", "")},
            headers={"AUTH_KEY": self._key},
        )
        rows = body.get("OutBlock_1")
        if not isinstance(rows, list):
            raise ResearchSourceError("krx: missing_output_block_or_not_approved")
        ingested = ingested_at or datetime.now(timezone.utc).isoformat()
        # KRX historical release clock is not inferred from the requested market date.
        available = available_at or ingested
        output, omitted = [], []
        for row in rows:
            if service == "kospi_master":
                output.append({**row, "available_at": available, "ingested_at": ingested})
                continue
            try:
                price_keys = (
                    ("OPNPRC_IDX", "HGPRC_IDX", "LWPRC_IDX", "CLSPRC_IDX")
                    if service == "kospi_index"
                    else ("TDD_OPNPRC", "TDD_HGPRC", "TDD_LWPRC", "TDD_CLSPRC")
                )
                item = {
                    "symbol": str(row.get("ISU_CD") or row.get("IDX_NM")),
                    "session": session,
                    **{
                        target: str(finite(row[source], positive=True))
                        for target, source in zip(("open", "high", "low", "close"), price_keys)
                    },
                    "volume": int(finite(row["ACC_TRDVOL"], integer=True)),
                    "turnover": str(finite(row["ACC_TRDVAL"])),
                    "venue": "KRX",
                    "session_type": "regular",
                    "final": True,
                    "price_basis": "raw",
                    "available_at": available,
                    "ingested_at": ingested,
                    "source": "krx:" + service,
                    "availability_policy": "explicit_evidence"
                    if available_at
                    else "ingestion_only_not_historical_pit",
                }
                if row.get("MKTCAP") not in (None, "", "-"):
                    item["market_cap"] = str(finite(row["MKTCAP"]))
                if row.get("NAV") not in (None, "", "-"):
                    item["nav"] = str(finite(row["NAV"], positive=True))
                output.append(item)
            except (ValueError, KeyError):
                omitted.append(
                    {
                        "symbol": str(row.get("ISU_CD", "")),
                        "session": session,
                        "reason": "missing_or_invalid_ohlcv",
                    }
                )
        return {
            "provider": self.provider,
            "table": "instruments"
            if service == "kospi_master"
            else "benchmarks"
            if service == "kospi_index"
            else "bars",
            "records": output,
            "omitted": omitted,
            "raw": body,
            "hash": content_hash(body),
            "historical_availability_verified": bool(available_at),
            "real_validation_complete": False,
        }


def import_records(content: str, *, format: str, table: str) -> list[dict]:
    if len(content.encode()) > 20_000_000:
        raise ValueError("가져오기 자료는 20MB 이하로 나누세요.")
    if format == "json":
        rows = json.loads(content)
    elif format == "csv":
        rows = list(csv.DictReader(io.StringIO(content)))
        for row in rows:
            for key, value in list(row.items()):
                if value == "":
                    row[key] = None
                elif key in {"final", "tradable", "inverse"}:
                    if value.lower() not in {"true", "false"}:
                        raise ValueError("CSV bool 값은 true/false여야 합니다.")
                    row[key] = value.lower() == "true"
                elif key == "symbols":
                    row[key] = json.loads(value)
    else:
        raise ValueError("지원 형식은 json/csv입니다.")
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError("가져오기 자료는 행 객체 배열이어야 합니다.")
    # Ensure serializability / no NaN before passing to domain validation.
    canonical(rows)
    return rows
