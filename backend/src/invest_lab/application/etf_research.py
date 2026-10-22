"""Independent ETF use cases. There is deliberately no broker or operational store port."""

from dataclasses import replace

from invest_lab.domain.etf_research import EtfReplayConfig, replay_etf, validate_etf_dataset
from invest_lab.domain.etf_strategies import POLICIES
from invest_lab.ports.etf_research import EtfDataSource, EtfResearchStore


class EtfResearchService:
    def __init__(
        self,
        store: EtfResearchStore,
        source: EtfDataSource | None = None,
        engine_revision="unrecorded",
    ):
        self.store, self.source = store, source
        self.engine_revision = engine_revision

    def collect(self, symbols, start, end):
        if self.source is None:
            raise ValueError("This ETF research instance is offline")
        datasets = self.source.collect(symbols, start, end)
        return {
            symbol: self.store.save_dataset(validate_etf_dataset(dataset))
            for symbol, dataset in datasets.items()
        }

    def run(self, dataset_id, policy_id, config: EtfReplayConfig, parameters=None):
        if policy_id not in POLICIES:
            raise ValueError("Unknown independent ETF policy")
        policy = POLICIES[policy_id]
        if parameters:
            if set(parameters) & {"id", "family", "label"}:
                raise ValueError("Policy identity cannot be overridden")
            policy = replace(policy, **parameters)
        dataset = self.store.load_dataset(dataset_id)
        result = replay_etf(dataset, policy, config)
        result["dataset_id"] = dataset_id
        result["engine_revision"] = self.engine_revision
        result["provenance"] = dataset.get("provenance", {})
        identifier = self.store.save_run(result)
        return {
            "run_id": identifier,
            "symbol": result["symbol"],
            "policy": policy.id,
            "config": result["config"],
            "metrics": result["metrics"],
            "trades": len(result["trades"]),
            "cost_krw": result["cost_krw"],
            "audit": result["audit"],
            "performance_validated": False,
        }
