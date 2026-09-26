"""Explicit local market/source -> immutable research dataset integration.

This adapter reads stored data and explicitly supplied provider clients. It does not
load credentials, contact a broker, synthesize exchange calendars, or replace an
old universe with today's master. A successful import is not PIT certification.
"""

import json
from datetime import datetime, timezone

from invest_lab.domain.research import (
    canonical,
    content_hash,
    coverage,
    decision_time,
    timestamp,
    validate_dataset,
)


class ResearchDataPipeline:
    def __init__(self, market_store, research_service):
        self.market_store = market_store
        self.research_service = research_service

    def build_market_dataset(
        self,
        start,
        end,
        *,
        name="Local market research snapshot",
        as_of=None,
        observed_by=None,
        historical_universe=None,
        benchmarks=None,
        actions=None,
        financial_facts=None,
        macro=None,
        etf_metadata=None,
        extra_instruments=None,
        provenance=None,
        source_kind="imported",
    ):
        cutoff = timestamp(as_of or datetime.now(timezone.utc))
        observed = timestamp(observed_by or cutoff)
        # sessions() requires every date (including closed days) to be sourced.
        calendar = self.market_store.sessions(start, end, as_of=cutoff)
        sessions = [r["session"] for r in calendar]
        if not sessions:
            raise ValueError("확인된 개장 세션이 없습니다.")
        instruments = self.market_store.instruments(as_of=cutoff)
        if not instruments:
            raise ValueError("먼저 출처가 있는 종목 마스터를 가져오세요.")
        payload = {
            "name": name,
            "source_kind": source_kind,
            "sessions": sessions,
            "instruments": instruments + list(extra_instruments or []),
            "bars": [],
            "universe": list(historical_universe or []),
            "actions": list(actions or []),
            "financial_facts": list(financial_facts or []),
            "macro": list(macro or []),
            "benchmarks": list(benchmarks or []),
            "etf_metadata": list(etf_metadata or []),
        }
        issues, skipped = [], []
        incoming_provenance = dict(provenance or {})
        proofs = {
            "historical_universe_verified": incoming_provenance.get("historical_universe_verified")
            is True
            and historical_universe is not None,
            "calendar_verified": incoming_provenance.get("calendar_verified") is True,
            "corporate_actions_verified": incoming_provenance.get("corporate_actions_verified")
            is True
            and actions is not None,
        }
        calendar_digest = content_hash(calendar)
        if any(timestamp(r["available_at"]) > timestamp(r["open_at"]) for r in calendar):
            proofs["calendar_verified"] = False
            issues.append("calendar_not_available_before_open")
        if historical_universe is None:
            # This is a truthful reconstruction of stored contemporary snapshots,
            # not proof that delisted instruments are represented in old periods.
            issues.append("historical_universe_not_supplied")
            for session in sessions:
                at = decision_time(session)
                known = self.market_store.instruments(as_of=min(at, cutoff), active_on=session)
                if known:
                    payload["universe"].append(
                        {
                            "session": session,
                            "symbols": [r["symbol"] for r in known],
                            "available_at": max((r["available_at"] for r in known), key=timestamp),
                            "ingested_at": observed.isoformat(),
                            "source": "stored_master_reconstruction",
                            "availability_policy": "stored_versions_only_no_backfill",
                        }
                    )
        if actions is None:
            issues.append("corporate_action_coverage_not_supplied")
        if not benchmarks:
            issues.append("benchmark_history_not_supplied")
        with self.market_store.connection() as connection:
            rows = connection.execute(
                "SELECT payload,recorded_at,revision,checksum FROM market_bars "
                "WHERE session>=? AND session<=? ORDER BY session,id",
                (sessions[0], sessions[-1]),
            ).fetchall()
        known_symbols = {r["symbol"] for r in payload["instruments"]}
        seen = set()
        for stored in rows:
            row = json.loads(stored["payload"])
            if (
                timestamp(row["available_at"]) > cutoff
                or timestamp(stored["recorded_at"]) > observed
            ):
                continue
            if row["session"] not in sessions:
                skipped.append({"symbol": row["symbol"], "reason": "not_in_verified_calendar"})
                continue
            if not row.get("finalized"):
                skipped.append({"symbol": row["symbol"], "reason": "not_finalized"})
                continue
            if row["symbol"] not in known_symbols:
                raise ValueError("저장 일봉의 종목이 snapshot 마스터에 없습니다.")
            normalized = {
                **row,
                "price_basis": "adjusted" if row["adjusted"] else "raw",
                "session_type": "regular",
                "final": True,
                "ingested_at": stored["recorded_at"],
                "source_revision": stored["revision"],
                "source_checksum": stored["checksum"],
            }
            # Missing turnover remains absent; technical coverage reports it.
            if normalized.get("turnover") is None:
                normalized.pop("turnover", None)
            digest = content_hash(normalized)
            if digest not in seen:
                seen.add(digest)
                payload["bars"].append(normalized)
        sources = sorted({r.get("source", "unknown") for r in instruments + payload["bars"]})
        if source_kind == "synthetic" or any(
            any(marker in source.lower() for marker in ("synthetic", "demo", "fixture"))
            for source in sources
        ):
            payload["source_kind"] = "synthetic"
        payload["provenance"] = {
            **incoming_provenance,
            "source": incoming_provenance.get("source", "local_market_store_snapshot"),
            **proofs,
            "market_sources": sources,
            "calendar_hash": calendar_digest,
            "calendar_rows": calendar,
            "snapshot_at": cutoff.isoformat(),
            "observed_by": observed.isoformat(),
            "pipeline_issues": sorted(set(issues)),
            "master_observation_time": "not_stored_by_market_master; snapshot_time_only",
        }
        validated = validate_dataset(payload)
        transfer = {
            "calendar_sessions": len(sessions),
            "bar_revisions": len(payload["bars"]),
            "skipped_rows": skipped[:100],
            "skipped_count": len(skipped),
            "available_and_ingested_times_preserved": True,
            "current_master_not_backfilled": True,
            "source_kind": payload["source_kind"],
        }
        return {"payload": validated, "coverage": coverage(validated), "transfer_report": transfer}

    def import_market_dataset(self, start, end, **kwargs):
        built = self.build_market_dataset(start, end, **kwargs)
        result = self.research_service.import_dataset(built["payload"])
        result["transfer_report"] = built["transfer_report"]
        return result

    def import_file_content(
        self, content, *, format="json", base_dataset_id=None, table=None, name=None
    ):
        """CLI-friendly text entry. File access/paths and key loading belong to the caller."""
        if format == "json" and base_dataset_id is None:
            if len(content.encode()) > 100_000_000:
                raise ValueError("전체 JSON 가져오기는 100MB 이하입니다.")
            return self.research_service.import_dataset(json.loads(content))
        return self.research_service.import_dataset(
            {
                "format": format,
                "base_dataset_id": base_dataset_id,
                "table": table,
                "name": name or "Approved source import",
                "csv_text" if format == "csv" else "json_text": content,
            }
        )

    def collect_into_dataset(
        self,
        base_dataset_id,
        provider,
        request,
        *,
        symbol_map=None,
        instrument_overrides=None,
        name=None,
    ):
        collected = self.research_service.collect_source(provider, request)
        return self.merge_source_result(
            base_dataset_id,
            collected["result"],
            symbol_map=symbol_map,
            instrument_overrides=instrument_overrides,
            name=name,
        )

    def merge_source_result(
        self,
        base_dataset_id,
        result,
        *,
        symbol_map=None,
        instrument_overrides=None,
        name=None,
    ):
        """Append actual source rows and originals; do not promote coverage attestations."""
        table = result.get("table")
        allowed = {"bars", "benchmarks", "macro", "financial_facts", "instruments"}
        if table not in allowed or not isinstance(result.get("records"), list):
            raise ValueError(
                "이 응답은 연구 행이 아닙니다. 공시 목록/최신 재무는 원문 매핑이 필요합니다."
            )
        if result.get("revised_only"):
            raise ValueError(
                "접수번호 원본 재무를 사용하세요. 최신 수정본은 PIT 원장에 병합하지 않습니다."
            )
        data = self.research_service.repository.get_dataset(base_dataset_id)["data"]
        rows = json.loads(canonical(result["records"]))
        symbol_map, overrides = symbol_map or {}, instrument_overrides or {}
        for row in rows:
            original = row.get("symbol") or row.get("ISU_CD")
            if original in symbol_map:
                row["source_symbol"] = original
                row["symbol"] = symbol_map[original]
            if table == "instruments":
                if original not in overrides:
                    raise ValueError(
                        "KRX 마스터는 보통주/ETF 분류를 검토한 instrument_overrides가 필요합니다."
                    )
                row.update(overrides[original])
                if not row.get("symbol"):
                    raise ValueError("명시적인 종목 식별자 매핑이 필요합니다.")
        incoming = {content_hash(r): r for r in rows}
        existing = {content_hash(r) for r in data[table]}
        if table == "instruments":
            identities = {r["symbol"] for r in data[table]}
            if any(r["symbol"] in identities for r in rows):
                raise ValueError(
                    "기존 상품 분류 수정은 새 마스터 snapshot으로 명시적으로 처리하세요."
                )
        appended = [r for key, r in incoming.items() if key not in existing]
        data[table].extend(appended)
        digest = content_hash(result)
        data.setdefault("source_artifacts", {})[digest] = result
        data["name"] = name or data["name"] + " + " + str(result.get("provider", table))
        provenance = data["provenance"]
        provenance["parent_dataset"] = base_dataset_id
        provenance.setdefault("source_manifests", []).append(
            {
                "provider": result.get("provider"),
                "table": table,
                "artifact_hash": digest,
                "records": len(rows),
                "appended_records": len(appended),
                "omitted": result.get("omitted", []),
                "historical_availability_verified": result.get("historical_availability_verified"),
            }
        )
        if result.get("omitted"):
            provenance.setdefault("pipeline_issues", []).append("source_rows_omitted")
        saved = self.research_service.import_dataset(data)
        saved["collection"] = {
            "table": table,
            "appended_records": len(appended),
            "artifact_hash": digest,
            "requires_coverage_review": True,
        }
        return saved
