"""Run independently funded automatic-model hypotheses, then audit capital-partition blends."""

from dataclasses import replace

from invest_lab.domain.auto_models import BLENDS, MODELS
from invest_lab.domain.auto_replay import AutoConfig, blend, prepare, replay
from invest_lab.ports.etf_research import EtfDataSource, EtfResearchStore


class AutoResearchService:
    def __init__(
        self, store: EtfResearchStore, source: EtfDataSource | None = None, revision="unrecorded"
    ):
        self.store, self.source, self.revision = store, source, revision
        self._tapes = {}

    def collect(self, symbols, start, end):
        if self.source is None:
            raise ValueError("This instance is offline")
        return {
            s: self.store.save_dataset(d)
            for s, d in self.source.collect(symbols, start, end).items()
        }

    def tape(self, dataset_ids):
        key = tuple(sorted(dataset_ids.items()))
        if key not in self._tapes:
            loaded = {s: self.store.load_dataset(k) for s, k in key}
            if any(d["symbol"] != s for s, d in loaded.items()):
                raise ValueError("Dataset symbol/ID mismatch")
            self._tapes[key] = prepare(loaded, tuple(sorted(loaded)))
        return self._tapes[key]

    def run(self, dataset_ids, model_id, config: AutoConfig, parameters=None):
        tape = self.tape(dataset_ids)
        if model_id in BLENDS:
            if parameters:
                raise ValueError("Blend constituent overrides require a new explicit experiment")
            children = [self.run(dataset_ids, name, config) for name, _ in BLENDS[model_id]]
            result = blend(
                model_id,
                [
                    (self.store.load_run(c["run_id"]), weight)
                    for c, (_, weight) in zip(children, BLENDS[model_id])
                ],
            )
            result["component_ids"] = [c["run_id"] for c in children]
        else:
            if model_id not in MODELS:
                raise ValueError("Unknown automatic model")
            if parameters and set(parameters) & {"id", "kind", "label", "symbols"}:
                raise ValueError("Model identity cannot be overridden")
            model = replace(MODELS[model_id], **(parameters or {}))
            result = replay(tape, model, config)
        result["dataset_ids"] = dataset_ids
        result["engine_revision"] = self.revision
        result["provenance"] = {s: d.get("provenance", {}) for s, d in tape["datasets"].items()}
        identifier = self.store.save_run(result)
        return {
            "run_id": identifier,
            "model": model_id,
            "config": result["config"],
            "start": result["start"],
            "end": result["end"],
            "metrics": result["metrics"],
            "cost_krw": result["cost_krw"],
            "audit": result["audit"],
            "performance_validated": False,
        }
