"""Independent selection research using an injected store/source and the audited account engine."""

from dataclasses import replace

from invest_lab.domain.auto_models import MODELS
from invest_lab.domain.auto_replay import blend, replay
from invest_lab.domain.selection_models import (
    SELECTION_MODELS,
    decision_function,
    prepare_selection,
)
from invest_lab.ports.etf_research import EtfDataSource, EtfResearchStore


class SelectionResearchService:
    def __init__(
        self, store: EtfResearchStore, source: EtfDataSource | None = None, revision="unrecorded"
    ):
        self.store, self.source, self.revision = store, source, revision
        self.tapes = {}

    def collect(self, symbols, start, end):
        if self.source is None:
            raise ValueError("This instance is offline")
        return {
            s: self.store.save_dataset(d)
            for s, d in self.source.collect(symbols, start, end).items()
        }

    def run(self, dataset_ids, model_id, config, parameters=None):
        key = tuple(sorted(dataset_ids.items()))
        if key not in self.tapes:
            data = {s: self.store.load_dataset(k) for s, k in key}
            if any(s != d["symbol"] for s, d in data.items()):
                raise ValueError("Dataset symbol/ID mismatch")
            self.tapes[key] = prepare_selection(data)
        tape = self.tapes[key]
        if model_id == "sector_pullback":
            if parameters:
                raise ValueError("Blend overrides require a separate declared experiment")
            children = [self.run(dataset_ids, m, config) for m in ("sector_trend", "pullback_rsi")]
            result = blend(
                model_id,
                [(self.store.load_run(c["run_id"]), w) for c, w in zip(children, (0.7, 0.3))],
            )
            result["component_ids"] = [c["run_id"] for c in children]
            # A constituent's risk diagnostics do not describe the combined account.
            result.pop("allocation_diagnostics", None)
        else:
            if parameters and set(parameters) & {"id", "label", "kind", "candidates", "symbols"}:
                raise ValueError("Model identity cannot be overridden")
            if model_id in SELECTION_MODELS:
                model = SELECTION_MODELS[model_id]
            elif model_id == "hold_qual":
                model = replace(
                    MODELS["hold_spy"], id="hold_qual", label="QUAL 장기 보유", symbols=("QUAL",)
                )
            elif model_id in MODELS:
                model = MODELS[model_id]
            else:
                raise ValueError("Unknown selection research model")
            model = replace(model, **(parameters or {}))
            diagnostics = []
            result = replay(tape, model, config, decide_fn=decision_function(tape, diagnostics))
            result["allocation_diagnostics"] = diagnostics
            if "candidates" in result["policy"]:
                result["policy"]["candidates"] = list(result["policy"]["candidates"])
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
