"""Durable local design orchestration with observed scientific artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import math
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .agent import Event
from .design_inputs import (DesignError, DesignSettings, ProteinMPNNRequest, prepare_target,
                            sequence_bytes, checked_result, validate_fold_metrics)
from .design_store import DesignStore, json_bytes
from .evidence import FILES, capture
from .modal_client import ModalTools
from .provider import OpenRouter, ProviderError

SCIENTIFIC_TOOLS = frozenset({"boltzgen", "proteinmpnn", "esmfold2", "esmc"})


class DesignStart(BaseModel):
    """Generic single-chain target, prompt and immutable generation settings."""
    model_config = ConfigDict(extra="forbid")
    id: UUID
    prompt: str = Field(min_length=1, max_length=100000)
    model: str
    structure: str = Field(min_length=1, max_length=10*1024*1024)
    format: str
    chain: str = Field(min_length=1, max_length=16)
    settings: DesignSettings = Field(default_factory=DesignSettings)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DesignService:
    """One process owns durable jobs; transport injection permits unpaid boundary tests."""

    def __init__(self, store: DesignStore, provider: OpenRouter, transport=None):
        self.store, self.provider = store, provider
        self.transport = transport or ModalTools()
        if isinstance(self.transport, ModalTools):
            self.transport.bind_store(store)
        self.tasks: dict[str, asyncio.Task] = {}
        self.submissions: dict[str, asyncio.Task] = {}
        self.recovery_lock = asyncio.Lock()

    def snapshot(self, id: str) -> dict:
        _, state = self.store.get(id)
        snapshot = {k: v for k, v in state.items() if k not in {"events", "target"}}
        snapshot["artifacts"] = [a for a in self.store.artifacts(id)
                                 if a["name"] not in FILES or state.get("evidence_status") == "verified"]
        snapshot["jobs"] = [{k: v for k, v in job.items() if k not in {"request", "result"}} for job in state["jobs"]]
        return snapshot

    def log(self, id: str, message: str) -> None:
        _, state = self.store.get(id)
        state["logs"].append({"at": now(), "message": message})
        state["logs"] = state["logs"][-100:]
        self.store.save(id, state)

    def start(self, data: DesignStart) -> dict:
        if not data.prompt.strip() or not data.model.startswith(("anthropic/", "openai/")) or any(c.isspace() for c in data.model):
            raise DesignError("INVALID_REQUEST", "Enter a question and select an OpenAI or Anthropic model.")
        id = str(data.id)
        request = data.model_dump(mode="json")
        try:
            existing, _ = self.store.get(id)
        except DesignError as exc:
            if exc.code != "WORKFLOW_NOT_FOUND":
                raise
        else:
            if existing != request:
                raise DesignError("REQUEST_CONFLICT", "Workflow ID already belongs to a different request.")
            return self.snapshot(id)
        if not self.transport.configured:
            raise DesignError("TOOL_UNAVAILABLE", "Modal tools are not enabled. Research and examples remain available.")
        if not self.provider.key:
            raise DesignError("TOOL_UNAVAILABLE", "An OpenRouter API key is required.")
        target = prepare_target(data.structure.encode(), data.format, data.chain)
        if any(not 1 <= r <= len(target["sequence"]) for r in data.settings.binding_residues):
            raise DesignError("INVALID_TARGET", "Binding residues must reference the prepared target positions.")
        state = {"id": id, "status": "running", "started": now(), "logs": [], "text": "",
                 "returned_model": "", "downloads": [], "artifacts": [], "candidates": [], "jobs": [],
                 "events": [], "target": target, "record_count": 1, "target_summary": {
                     "sequence": target["sequence"], "mapping": target["mapping"], "limits": target["limits"]}}
        self.store.create(id, request, state)
        self.store.artifact(id, f"target.{data.format}", "coordinates", data.structure.encode())
        self.store.artifact(id, "prepared-target.cif", "coordinates", target["prepared_cif"].encode())
        self.store.artifact(id, "target-map.json", "log", json_bytes(state["target_summary"]))
        self.tasks[id] = asyncio.create_task(self.run(id))
        self.tasks[id].add_done_callback(lambda task: self.finished(id, task))
        return self.snapshot(id)

    def finished(self, id: str, task: asyncio.Task) -> None:
        """Never leave a dead unexpected task presented as ongoing execution."""
        if not task.cancelled() and task.exception() is not None:
            _, state = self.store.get(id)
            state["status"] = "needs_attention"
            self.store.save(id, state)
            self.log(id, "Unexpected workflow failure; known remote jobs require reconciliation.")

    def observe(self, id: str, event: Event, artifacts: list[dict] | None = None) -> None:
        _, state = self.store.get(id)
        index = len(state["events"])
        request = self.store.artifact(id, f"request-{index}.json", "log", event.request)
        response = self.store.artifact(id, f"response-{index}.json", "transcript", event.response)
        state["events"].append({"name": event.name, "request": request, "response": response,
                                 "started": event.started.isoformat(), "stopped": event.stopped.isoformat(),
                                 "metadata": event.metadata, "artifacts": artifacts or []})
        state["record_count"] = 1 + len(state["events"]) + (
            sum(e["name"] in SCIENTIFIC_TOOLS for e in state["events"])
            if isinstance(self.transport, ModalTools) else 0)
        state["artifacts"] = self.store.artifacts(id)
        self.store.save(id, state)

    async def scientific(self, id: str, tool: str, request: dict) -> dict:
        """Submit once; persist uncertainty rather than blindly retrying acceptance."""
        if tool not in SCIENTIFIC_TOOLS:
            raise DesignError("TOOL_UNAVAILABLE", "Unknown scientific tool.")
        if tool == "proteinmpnn":
            try:
                request = ProteinMPNNRequest.model_validate(request).model_dump()
            except ValidationError as exc:
                raise DesignError("INVALID_TARGET", "Invalid ProteinMPNN backbone or settings.") from exc
        _, state = self.store.get(id)
        identity = hashlib.sha256(json_bytes({"tool": tool, "request": request})).hexdigest()
        matching = [j for j in state["jobs"] if j["identity"] == identity]
        if matching:
            job = matching[0]
            if job["status"] == "completed":
                return job["result"]
            raise DesignError("SUBMISSION_UNCERTAIN", "This logical job already exists; reconcile it before another attempt.")
        if tool == "boltzgen" and any(j["tool"] == "boltzgen" for j in state["jobs"]):
            raise DesignError("REQUEST_CONFLICT", "A workflow authorizes only one logical generation request.")
        if state["status"] != "running":
            raise DesignError("CANCELLATION_PENDING", "Workflow no longer permits new jobs.")
        job = {"id": str(uuid4()), "tool": tool, "identity": identity, "status": "submitting",
               "call_id": None, "request": request, "attempt": 1, "started": now()}
        if isinstance(self.transport, ModalTools):
            self.transport.prepare(tool, job["id"], request)
            job["execution_id"] = job["id"]
        state["jobs"].append(job)
        self.store.save(id, state)
        self.log(id, f"Submitting {tool}")
        self.observe(id, Event("scientific_dispatch", json_bytes(request), json_bytes({"job_id": job["id"], "status": "submitting"}),
                              datetime.fromisoformat(job["started"]), datetime.now(timezone.utc),
                              {"tool": tool, "job_id": job["id"], "attempt": 1}))
        submission = asyncio.create_task(self.submit(id, job["id"]))
        submission.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        self.submissions[job["id"]] = submission
        # Cancellation must not discard the acceptance acknowledgement and its call ID.
        call_id = await asyncio.shield(submission)
        network_attempts = 0
        while True:
            try:
                result = await self.transport.poll(call_id)
                network_attempts = 0
            except (OSError, TimeoutError) as exc:
                network_attempts += 1
                self.log(id, f"Progress connection interrupted; reconnecting to the same job ({network_attempts}/3).")
                if network_attempts >= 3:
                    _, state = self.store.get(id)
                    state["status"] = "recovering"
                    self.store.save(id, state)
                    raise DesignError("SUBMISSION_UNCERTAIN", "Progress unavailable; the known remote job remains tracked.") from exc
                await asyncio.sleep(network_attempts)
                continue
            except DesignError as exc:
                if exc.code == "CONNECTION_LOST":
                    network_attempts += 1
                    if network_attempts < 3:
                        self.log(id, f"Reconnecting to the same {tool} call ({network_attempts}/3).")
                        await asyncio.sleep(network_attempts)
                        continue
                    _, current = self.store.get(id)
                    current["status"] = "recovering"
                    self.store.save(id, current)
                    raise DesignError("SUBMISSION_UNCERTAIN", "Progress unavailable; background recovery will reconnect.") from exc
                if exc.code == "EXECUTION_FAILED":
                    try:
                        recovered = await self.transport.recover(job["id"])
                    except (OSError, TimeoutError, DesignError):
                        recovered = {}
                    return self.harvest(id, job["id"], recovered.get("result", {"error": "EXECUTION_FAILED", "artifacts": []}))
                _, current = self.store.get(id)
                current["status"] = "needs_attention"
                self.store.save(id, current)
                raise DesignError("EXECUTION_FAILED", "Remote outcome needs reconciliation; no new generation will be started.") from exc
            if result is not None:
                break
            await asyncio.sleep(1)
        return self.harvest(id, job["id"], result)

    async def submit(self, id: str, job_id: str) -> str:
        """Keep acceptance handling alive independently of the local workflow coroutine."""
        _, state = self.store.get(id)
        job = next(j for j in state["jobs"] if j["id"] == job_id)
        try:
            call_id = await self.transport.submit(job["tool"], job_id, job["request"])
        except (OSError, TimeoutError, DesignError) as exc:
            _, current = self.store.get(id)
            selected = next(j for j in current["jobs"] if j["id"] == job_id)
            selected["status"] = "uncertain"
            current["status"] = "needs_attention"
            self.store.save(id, current)
            raise DesignError("SUBMISSION_UNCERTAIN", "Modal acceptance could not be confirmed; no automatic resubmission.") from exc
        _, current = self.store.get(id)
        next(j for j in current["jobs"] if j["id"] == job_id).update(call_id=call_id, status="running")
        self.store.save(id, current)
        return call_id

    def harvest(self, id: str, job_id: str, result: dict) -> dict:
        """Record invalid returned outputs as failed attempts, retaining safe partial bytes."""
        _, state = self.store.get(id)
        job = next(j for j in state["jobs"] if j["id"] == job_id)
        if job.get("result") is not None:
            return job["result"]
        if state["downloads"]:
            raise DesignError("EVIDENCE_FAILED", "Cannot add outputs to a sealed workflow.")
        try:
            if job["tool"] == "boltzgen" and (not isinstance(result, dict) or "candidates" not in result) and not (isinstance(result, dict) and result.get("error")):
                raise DesignError("OUTPUT_INVALID", "Generation result must report its retained candidates explicitly.")
            return self.ingest(id, job_id, checked_result(result))
        except (DesignError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            # Keep usable returned bytes even when scientific parsing fails. No scores/candidates are accepted.
            from .design_inputs import RemoteArtifact
            from pydantic import ValidationError
            partial = []
            if isinstance(result, dict) and isinstance(result.get("artifacts"), list):
                for artifact in result["artifacts"]:
                    try:
                        parsed = RemoteArtifact.model_validate(artifact)
                    except ValidationError:
                        continue
                    if ((parsed.sha256 is None or hashlib.sha256(parsed.raw).hexdigest() == parsed.sha256)
                            and (parsed.bytes_len is None or len(parsed.raw) == parsed.bytes_len)):
                        partial.append(parsed.model_dump(exclude_none=True))
            self.log(id, "OUTPUT_INVALID: Returned output failed validation; retained partial artifacts are not accepted candidates.")
            return self.ingest(id, job_id, {"error": "OUTPUT_INVALID", "artifacts": partial})

    def ingest(self, id: str, job_id: str, result: dict) -> dict:
        """Commit returned bytes and attempt outcome before presenting results."""
        _, state = self.store.get(id)
        job = next(j for j in state["jobs"] if j["id"] == job_id)
        if job.get("result") is not None:
            return job["result"]
        if not isinstance(result, dict) or not isinstance(result.get("artifacts"), list):
            raise DesignError("OUTPUT_INVALID", "Malformed scientific result envelope.")
        execution = result.get("execution")
        if isinstance(self.transport, ModalTools) and not execution and not result.get("error"):
            raise DesignError("OUTPUT_INVALID", "Remote attempt is missing its execution identities.")
        if execution and (execution.get("id") != job_id or execution.get("tool") != job["tool"]
                or execution.get("attempt") != job["attempt"]
                or execution.get("request_sha256") != hashlib.sha256(json_bytes(job["request"])).hexdigest()
                or execution.get("call_id") != job["call_id"]):
            raise DesignError("OUTPUT_INVALID", "Returned execution identity does not match the dispatch.")
        if not result.get("error"):
            from .design_workers import validate_cif_sequences
            coordinates = [a for a in result["artifacts"] if a.get("kind") == "coordinates"]
            if job["tool"] == "esmfold2":
                sequences = {"B": job["request"]["sequence"]}
                if job["request"]["mode"] == "complex":
                    sequences["A"] = job["request"]["target_sequence"]
                if len(coordinates) != 1:
                    raise DesignError("OUTPUT_INVALID", "Folding result is missing its predicted structure.")
                validate_cif_sequences(coordinates[0]["raw"], sequences)
                validate_fold_metrics(result.get("metrics", {}), sum(map(len, sequences.values())),
                                      complex=job["request"]["mode"] == "complex")
            elif job["tool"] == "boltzgen":
                generated = result.get("candidates")
                if not isinstance(generated, list) or len(generated) > job["request"]["count"]:
                    raise DesignError("OUTPUT_INVALID", "Invalid generated candidate count.")
                for candidate in generated:
                    if not job["request"]["min_length"] <= len(candidate["sequence"]) <= job["request"]["max_length"]:
                        raise DesignError("OUTPUT_INVALID", "Generated binder length violates the submitted range.")
                    matching = [a for a in coordinates if a["name"].endswith(candidate["source"] + ".cif")]
                    if not matching:
                        raise DesignError("OUTPUT_INVALID", "Generated candidate is missing its complex structure.")
                    validate_cif_sequences(matching[-1]["raw"], {"A": job["request"]["target_sequence"], "B": candidate["sequence"]})
            elif job["tool"] == "esmc":
                from .design_inputs import masked_metrics
                metrics = result.get("metrics", {})
                expected = masked_metrics(metrics.get("per_residue_log_probability", []))
                if metrics.get("length") != len(job["request"]["sequence"]) or any(metrics.get(k) != v for k, v in expected.items()):
                    raise DesignError("OUTPUT_INVALID", "ESMC summary does not match its masked position scores.")
            elif job["tool"] == "proteinmpnn":
                request = ProteinMPNNRequest.model_validate(job["request"])
                generated = result.get("candidates")
                expected = {(sample, chain) for sample in range(1, request.count + 1)
                            for chain in sorted(request.design_chains)}
                observed, sample_metrics = set(), {}
                if not isinstance(generated, list) or len(generated) != len(expected):
                    raise DesignError("OUTPUT_INVALID", "ProteinMPNN returned an invalid candidate count.")
                for candidate in generated:
                    identity = (candidate.get("sample"), candidate.get("chain"))
                    metrics = candidate.get("metrics", {})
                    if (identity not in expected or identity in observed or candidate.get("rank") != identity[0]
                            or candidate.get("source") != f"sample-{identity[0]}-chain-{identity[1]}"
                            or len(candidate["sequence"]) != len(request.sequences[identity[1]])
                            or set(metrics) != {"score", "global_score", "seq_recovery", "sampling_temperature"}
                            or any(type(value) not in {int, float} or not math.isfinite(value) for value in metrics.values())
                            or not 0 <= metrics["seq_recovery"] <= 1
                            or not math.isclose(metrics["sampling_temperature"], request.sampling_temperature)):
                        raise DesignError("OUTPUT_INVALID", "ProteinMPNN candidate identity or metrics are inconsistent.")
                    if identity[0] in sample_metrics and sample_metrics[identity[0]] != metrics:
                        raise DesignError("OUTPUT_INVALID", "ProteinMPNN chains from one sample have inconsistent metrics.")
                    sample_metrics[identity[0]] = metrics
                    observed.add(identity)
                if observed != expected:
                    raise DesignError("OUTPUT_INVALID", "ProteinMPNN sample/chain coverage is incomplete.")
                expected_summary = {"samples": request.count, "designed_chains": sorted(request.design_chains),
                                    "fixed_chains": sorted(set(request.sequences) - set(request.design_chains)),
                                    "model": request.model, "seed": request.seed,
                                    "sampling_temperature": request.sampling_temperature}
                if result.get("metrics") != expected_summary:
                    raise DesignError("OUTPUT_INVALID", "ProteinMPNN summary does not match the submitted request.")
        artifacts = []
        for index, artifact in enumerate(result.get("artifacts", [])):
            raw = artifact["raw"]
            if not isinstance(raw, bytes):
                raise DesignError("OUTPUT_INVALID", "Tool artifact bytes are missing.")
            if "sha256" in artifact and hashlib.sha256(raw).hexdigest() != artifact["sha256"]:
                raise DesignError("OUTPUT_INVALID", "Recovered output digest mismatch.")
            if "bytes_len" in artifact and len(raw) != artifact["bytes_len"]:
                raise DesignError("OUTPUT_INVALID", "Recovered output length mismatch.")
            artifacts.append(self.store.artifact(id, f'{job_id}-{artifact["name"]}', artifact["kind"], raw))
        if result.get("metrics"):
            artifacts.append(self.store.artifact(id, f"{job_id}-metrics.json", "metric", json_bytes(result["metrics"])))
        candidates = []
        for candidate in ([] if result.get("error") else result.get("candidates", [])):
            sequence = candidate["sequence"]
            ref = self.store.artifact(id, f'{job_id}-candidate-{len(candidates)}.txt', "aa_sequence", sequence_bytes(sequence))
            artifacts.append(ref)
            fasta = self.store.artifact(id, f'{job_id}-candidate-{len(candidates)}.fasta', "fasta",
                                        f'>{candidate["source"]}\n{sequence}\n'.encode())
            artifacts.append(fasta)
            coordinates = [a for a in artifacts if a["kind"] == "coordinates" and a["name"].endswith(candidate["source"] + ".cif")]
            candidate_metrics = ({"proteinmpnn": candidate.get("metrics", {})} if job["tool"] == "proteinmpnn" else
                                 {"esmfold2-binder": None, "esmfold2-complex": None, "esmc-sequence": None})
            candidates.append({"id": f'{job_id}-{len(candidates)}', "sha256": ref["sha256"],
                               "sequence": sequence, "rank": candidate.get("rank"),
                               "sample": candidate.get("sample"), "chain": candidate.get("chain"),
                               "source": candidate["source"], "metrics": candidate_metrics,
                               "artifacts": [ref, fasta, *coordinates]})
        summary = {k: v for k, v in result.items() if k != "artifacts"}
        summary["artifacts"] = artifacts
        job.update(status="cancelled" if result.get("error") == "CANCELLED" else "failed" if result.get("error") else "completed", result=summary)
        state["candidates"].extend(candidates)
        if job["tool"] in {"esmfold2", "esmc"}:
            mode = job["request"].get("mode", "sequence")
            for candidate in state["candidates"]:
                if candidate["sequence"] == job["request"]["sequence"]:
                    # Keep quadratic confidence arrays and position scores out of the browser/model context.
                    candidate["metrics"][f'{job["tool"]}-{mode}'] = None if result.get("error") else {
                        k: v for k, v in result["metrics"].items() if k not in {"plddt", "pae", "per_residue_log_probability"}}
                    candidate["artifacts"].extend(artifacts)
        self.store.save(id, state)
        event = Event(job["tool"], json_bytes(job["request"]), json_bytes(summary),
                      datetime.fromisoformat(job.get("started", state["started"])), datetime.now(timezone.utc),
                      {"tool": job["tool"], "job_id": job_id, "attempt": job["attempt"], "status": job["status"],
                       "call_id": job.get("call_id") or "unobserved", "input_sha256": job["identity"]})
        self.observe(id, event, artifacts)
        self.log(id, f'{job["tool"]} {job["status"]}')
        return summary

    async def run(self, id: str) -> None:
        """Run a single immutable generation and required candidate assessments."""
        request, state = self.store.get(id)
        try:
            self.log(id, "Preparing a computational design attempt; no binding or safety verdict is implied.")
            # The user authorizes this fixed pipeline. Model text cannot change GPU settings.
            reply = await self.provider.chat([
                {"role": "system", "content": "Describe the supplied computational protein-design objective briefly. Tools will run afterward; do not invent results or claim binding/safety."},
                {"role": "user", "content": request["prompt"]}], request["model"], [], lambda m: self.log(id, m))
            self.observe(id, Event("openrouter", reply.request, reply.response, reply.started, reply.stopped,
                                   {"returned_model": reply.model}))
            target = state["target"]
            generated = await self.scientific(id, "boltzgen", {**request["settings"],
                "target_cif": target["prepared_cif"], "target_sequence": target["sequence"]})
            if generated.get("error"):
                raise DesignError("EXECUTION_FAILED", "BoltzGen failed; inspect the captured outcome.")
            _, state = self.store.get(id)
            state["counts"] = {"requested": request["settings"]["count"],
                               "generated": generated.get("metrics", {}).get("generated"),
                               "retained": len(state["candidates"]), "scored": 0}
            self.store.save(id, state)
            for candidate in list(state["candidates"]):
                for tool, mode in [("esmfold2", "binder"), ("esmfold2", "complex"), ("esmc", "sequence")]:
                    payload = {"sequence": candidate["sequence"]}
                    if tool == "esmfold2":
                        payload.update(mode=mode, target_sequence=target["sequence"], seed=request["settings"]["seed"],
                                       num_loops=10, num_sampling_steps=68, num_diffusion_samples=1)
                    result = await self.scientific(id, tool, payload)
                _, current = self.store.get(id)
                current["counts"]["scored"] = sum(all(c["metrics"].get(key) is not None for key in
                    ["esmfold2-binder", "esmfold2-complex", "esmc-sequence"]) for c in current["candidates"])
                self.store.save(id, current)
            _, state = self.store.get(id)
            reply = await self.provider.chat([
                {"role": "system", "content": "Summarize actual computational results and missing stages. Never claim experimental binding, safety or independent notarization."},
                {"role": "user", "content": json_bytes({"objective": request["prompt"], "candidates": state["candidates"],
                                                        "outcomes": [{"tool": j["tool"], "status": j["status"]} for j in state["jobs"]]}).decode()}],
                request["model"], [], lambda m: self.log(id, m))
            self.observe(id, Event("openrouter", reply.request, reply.response, reply.started, reply.stopped,
                                   {"returned_model": reply.model}))
            _, state = self.store.get(id)
            state.update(text=reply.message["content"], returned_model=reply.model,
                         status="partial" if any(j["status"] == "failed" for j in state["jobs"]) else "completed")
            self.store.save(id, state)
        except asyncio.CancelledError:
            return
        except (DesignError, ProviderError, OSError, ValueError) as exc:
            _, state = self.store.get(id)
            if isinstance(exc, DesignError):
                state["error_code"] = exc.code
            if state["status"] not in {"needs_attention", "recovering", "cancelling", "cancelled"}:
                state["status"] = "partial" if state["candidates"] else "failed"
            self.store.save(id, state)
            self.log(id, str(exc) if isinstance(exc, (DesignError, ProviderError)) else "Design execution or output validation failed.")
        _, state = self.store.get(id)
        if state["status"] in {"completed", "partial", "failed", "cancelled", "interrupted"}:
            self.publish(id)

    def publish(self, id: str) -> None:
        """Seal observed events and exact sequence artifacts using existing VAT checks."""
        request, state = self.store.get(id)
        if state["downloads"]:
            return
        if any(j["status"] not in {"completed", "failed", "cancelled"} for j in state["jobs"]):
            state["status"] = "needs_attention"
            self.store.save(id, state)
            self.log(id, "Unresolved remote execution prevents sealing a completed history.")
            return
        events, scientific, execution_nodes = [], {}, {}
        for index, item in enumerate(state["events"]):
            events.append(Event(item["name"], self.store.read(id, item["request"]["sha256"], item["request"]["name"]),
                                self.store.read(id, item["response"]["sha256"], item["response"]["name"]),
                                datetime.fromisoformat(item["started"]), datetime.fromisoformat(item["stopped"]), item["metadata"]))
            scientific[index] = [(a["kind"], a["name"], self.store.read(id, a["sha256"], a["name"])) for a in item["artifacts"]]
            if isinstance(self.transport, ModalTools) and item["name"] in SCIENTIFIC_TOOLS:
                result = self.transport.result(item["metadata"]["job_id"])
                if result is None:
                    state.update(evidence_status="unverified", error_code="EVIDENCE_FAILED")
                    self.store.save(id, state)
                    self.log(id, "EVIDENCE_FAILED: Scientific outcome has no finalized VAT node.")
                    return
                execution_nodes[index] = result
        # Inputs and status are committed as an explicitly observed local event as well.
        inputs = [a for a in self.store.artifacts(id) if a["name"].startswith("target") or a["name"] == "prepared-target.cif"]
        events.append(Event("workflow_status", json_bytes({"settings": request["settings"]}),
                            json_bytes({"status": state["status"], "candidates": state["candidates"], "counts": state.get("counts")}),
                            datetime.fromisoformat(state["started"]), datetime.now(timezone.utc), {"status": state["status"]}))
        scientific[len(events)-1] = [(a["kind"], a["name"], self.store.read(id, a["sha256"], a["name"])) for a in inputs]
        candidate_summary = self.store.artifact(id, "candidate-summary.json", "log", json_bytes(state["candidates"]))
        scientific[len(events)-1].append(("log", candidate_summary["name"], json_bytes(state["candidates"])))
        state["evidence_status"] = "unverified"
        try:
            # Failed publication never appends a second history to an existing session directory.
            with tempfile.TemporaryDirectory(prefix="seal-", dir=self.store.root) as temporary:
                standalone = request.get("model") == "none"
                directory = capture(Path(temporary), id, request["prompt"], events, state["text"],
                                datetime.fromisoformat(state["started"]), scientific,
                                purpose=("Operator-authorized standalone scientific adapter; locally captured Modal tool I/O"
                                         if standalone else "Designer computational protein design; locally captured Modal tool I/O"),
                                system=("One standalone scientific adapter was explicitly selected; no model call or automatic composition."
                                        if standalone else "User-authorized fixed BoltzGen, ESMFold2-Fast and ESMC pipeline. Model instructions are captured in request bodies."),
                                keys=self.store.keys(), execution_nodes=execution_nodes,
                                publication_root=self.store.root)
                from vatlog.bundle import Bundle
                from vatlog.roles import TrustBundle
                from vatlog.verify import verify_bundle
                bundle = Bundle.load(directory / "bundle.zip")
                trust = TrustBundle.load(directory / "trust.json")
                reports = []
                for candidate in state["candidates"]:
                    report = verify_bundle(bundle, trust, submitted_sequence=candidate["sequence"],
                                           require_transparency=True)
                    if report.exit_code != 0:
                        raise DesignError("EVIDENCE_FAILED", "Candidate output binding failed verification.")
                    matched = [r.seq_no for r in bundle.records if any(
                        a.kind == "aa_sequence" and a.sha256 == candidate["sha256"] for a in r.results)]
                    candidate["verification"] = {"sha256": candidate["sha256"], "matched_records": matched,
                                                  "exit_code": report.exit_code}
                    name = f'candidate-{candidate["id"]}-report.txt'
                    raw = f'Candidate: {candidate["id"]}\nSHA-256: {candidate["sha256"]}\nMatched records: {matched}\n\n{report.render()}'.encode()
                    reports.append((name, raw))
                # All checks pass before any bundle/trust/report is offered for download.
                for name in FILES:
                    self.store.artifact(id, name, "log", (directory/name).read_bytes())
                for name, raw in reports:
                    ref = self.store.artifact(id, name, "qc_report", raw)
                    next(c for c in state["candidates"] if name == f'candidate-{c["id"]}-report.txt')["artifacts"].append(ref)
            self.store.artifact(id, "candidate-verification.json", "qc_report", json_bytes([
                {"id": c["id"], **c["verification"]} for c in state["candidates"]]))
            state["downloads"] = list(FILES)
            state["record_count"] = bundle.signed_tree_head.tree_size
            state["evidence_status"] = "verified"
        except (OSError, ValueError):
            self.log(id, "EVIDENCE_FAILED: Scientific artifacts remain available but no verified bundle was published.")
            state["downloads"] = []
            state["error_code"] = "EVIDENCE_FAILED"
        state["artifacts"] = self.store.artifacts(id)
        self.store.save(id, state)

    async def cancel(self, id: str) -> dict:
        _, state = self.store.get(id)
        if state["status"] in {"completed", "partial", "failed", "cancelled", "interrupted"}:
            return self.snapshot(id)
        state["status"] = "cancelling"
        state["cancel_requested"] = True
        self.store.save(id, state)
        task = self.tasks.get(id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        pending = [self.submissions[j["id"]] for j in state["jobs"]
                   if j["id"] in self.submissions and not self.submissions[j["id"]].done()]
        if pending:
            await asyncio.wait(pending, timeout=3)
        _, state = self.store.get(id)
        unresolved = False
        for job in state["jobs"]:
            if job["status"] not in {"completed", "failed", "cancelled"}:
                if not job["call_id"]:
                    unresolved = True
                    continue
                try:
                    result = await self.transport.poll(job["call_id"])
                    if result is not None:
                        self.harvest(id, job["id"], result)
                        continue
                    await self.transport.cancel(job["call_id"])
                    try:
                        recovered = await self.transport.recover(job["id"])
                    except (OSError, TimeoutError, DesignError):
                        recovered = {}
                    if "result" in recovered:
                        self.harvest(id, job["id"], recovered["result"])
                    else:
                        unresolved = True
                except (OSError, TimeoutError, DesignError):
                    unresolved = True
        _, current = self.store.get(id)
        for job in state["jobs"]:
            if job["status"] == "cancelled":
                next(j for j in current["jobs"] if j["id"] == job["id"])["status"] = "cancelled"
        state = current
        state["status"] = "needs_attention" if unresolved else "cancelled"
        self.store.save(id, state)
        self.log(id, "Cancellation unresolved; GPU work may still run." if unresolved else "Cancelled; incurred charges cannot be reversed.")
        if not unresolved:
            for job in state["jobs"]:
                if job["status"] == "cancelled" and not job.get("cancellation_observed"):
                    self.observe(id, Event("scientific_cancellation", json_bytes({"call_id": job["call_id"]}),
                        json_bytes({"status": "cancelled"}), datetime.fromisoformat(job.get("started", state["started"])),
                        datetime.now(timezone.utc), {"tool": job["tool"], "job_id": job["id"]}))
                    _, state = self.store.get(id)
                    next(j for j in state["jobs"] if j["id"] == job["id"])["cancellation_observed"] = True
                    self.store.save(id, state)
            self.publish(id)
        return self.snapshot(id)

    async def recover(self) -> None:
        """Serialize background reconciliation against cancel/recovery requests."""
        async with self.recovery_lock:
            await self.reconcile()

    async def reconcile(self) -> None:
        """Harvest known jobs; never silently replay a model turn or GPU submission."""
        for request, state in self.store.all():
            id = state["id"]
            if id in self.tasks and not self.tasks[id].done():
                continue
            if state["status"] not in {"running", "recovering", "cancelling", "needs_attention"}:
                continue
            cancel_requested = state["status"] == "cancelling" or state.get("cancel_requested", False)
            unresolved = False
            mismatch = False
            for job in state["jobs"]:
                if job["status"] in {"completed", "failed", "cancelled"}:
                    continue
                try:
                    recovered = {}
                    if job["call_id"]:
                        try:
                            result = await self.transport.poll(job["call_id"])
                        except DesignError as exc:
                            if exc.code != "EXECUTION_FAILED":
                                raise
                            result = {"error": "EXECUTION_FAILED", "artifacts": []}
                            recovered = await self.transport.recover(job["id"])
                            result = recovered.get("result", {**result, "artifacts": recovered.get("partial_artifacts", [])})
                        if result is not None:
                            recovered["result"] = result
                    else:
                        recovered = await self.transport.recover(job["id"])
                    if "start" in recovered:
                        start = recovered["start"]
                        if (start.get("id") != job["id"] or start.get("tool") != job["tool"]
                                or start.get("request_sha256") != hashlib.sha256(json_bytes(job["request"])).hexdigest()
                                or start.get("attempt") != job["attempt"]
                                or (job["call_id"] and job["call_id"] != start.get("call_id"))):
                            raise DesignError("DEPENDENCY_MISMATCH", "Recovery manifest does not match the original dispatch.")
                        job["call_id"] = start["call_id"]
                        _, current = self.store.get(id)
                        next(j for j in current["jobs"] if j["id"] == job["id"])["call_id"] = job["call_id"]
                        self.store.save(id, current)
                    if "result" not in recovered and job["call_id"]:
                        try:
                            result = await self.transport.poll(job["call_id"])
                            if result is not None:
                                recovered["result"] = result
                        except DesignError as exc:
                            if exc.code == "EXECUTION_FAILED":
                                recovered["result"] = {"error": "EXECUTION_FAILED", "artifacts": []}
                            else:
                                raise
                    if "result" in recovered:
                        self.harvest(id, job["id"], recovered["result"])
                    else:
                        unresolved = True
                except (OSError, TimeoutError, DesignError) as exc:
                    unresolved = True
                    if isinstance(exc, DesignError) and exc.code == "DEPENDENCY_MISMATCH":
                        mismatch = True
                        if state.get("error_code") != exc.code:
                            self.log(id, "DEPENDENCY_MISMATCH: Recovery identity differs from the recorded dispatch; no call was adopted.")
                        _, current = self.store.get(id)
                        current["error_code"] = exc.code
                        self.store.save(id, current)
            _, current = self.store.get(id)
            current["status"] = "needs_attention" if mismatch else "recovering" if unresolved else "interrupted"
            # Keep recovered remote IDs even if the result is not yet available.
            for old, new in zip(state["jobs"], current["jobs"]):
                if old["call_id"]:
                    new["call_id"] = old["call_id"]
            self.store.save(id, current)
            if cancel_requested:
                await self.cancel(id)
            elif not unresolved:
                self.publish(id)

    async def monitor(self) -> None:
        """Track known jobs after restarts even when no browser is connected."""
        while True:
            await self.recover()
            await asyncio.sleep(2)

    async def shutdown(self) -> None:
        """Journal cancellation for every active workflow, including recovered remote jobs."""
        for _, state in self.store.all():
            if state["status"] in {"running", "recovering", "cancelling", "needs_attention"}:
                await self.cancel(state["id"])
        for submission in self.submissions.values():
            if not submission.done():
                submission.cancel()
        await asyncio.gather(*self.submissions.values(), return_exceptions=True)
        if isinstance(self.transport, ModalTools):
            await self.transport.drain()
