"""Unpaid remote-boundary tests using real state, artifact parsing and VAT verification."""

import asyncio
import math
from pathlib import Path
from uuid import uuid4

import gemmi
import httpx
import pytest

from runner.design import DesignService, DesignStart
from runner.design_inputs import DesignError, masked_metrics
from runner.design_store import DesignStore
from runner.design_workers import parse_proteinmpnn_outputs
from runner.provider import OpenRouter
from vatlog.bundle import Bundle
from vatlog.roles import TrustBundle
from vatlog.verify import verify_bundle


def coordinates(sequences):
    structure = gemmi.Structure()
    model = gemmi.Model("1")
    for name, sequence in sequences.items():
        chain = gemmi.Chain(name)
        for index, aa in enumerate(sequence, 1):
            residue = gemmi.Residue()
            residue.name = "ALA" if aa == "A" else "CYS"
            residue.seqid = gemmi.SeqId(index, " ")
            residue.het_flag = "A"
            atom = gemmi.Atom()
            atom.name, atom.element = "CA", gemmi.Element("C")
            atom.pos = gemmi.Position(index * 3.8, 1, 1)
            residue.add_atom(atom)
            chain.add_residue(residue)
        model.add_chain(chain)
    structure.add_model(model)
    structure.setup_entities()
    return structure.make_mmcif_document().as_string().encode()


class Remote:
    configured = True
    def __init__(self, failure=None):
        self.calls, self.cancelled = [], []
        self.requests = {}
        self.failure = failure

    async def submit(self, tool, id, request):
        self.calls.append(tool)
        if self.failure == "uncertain":
            raise TimeoutError("private-remote-error")
        self.requests[id] = (tool, request)
        return id

    async def poll(self, id):
        tool, request = self.requests[id]
        if self.failure == "pending":
            return None
        if tool == "boltzgen":
            return {"candidates": [{"source": "generated", "sequence": "A" * 60, "rank": None}],
                    "artifacts": [{"name": "generated.cif", "kind": "coordinates",
                                   "raw": coordinates({"A": request["target_sequence"], "B": "A" * 60})}]}
        if tool == "esmfold2":
            if self.failure == "fold":
                return {"error": "EXECUTION_FAILED", "artifacts": []}
            sequences = {"B": request["sequence"]}
            if request["mode"] == "complex":
                sequences["A"] = request["target_sequence"]
            return {"metrics": {"mean_plddt": .75, "plddt_raw_scale": "0–1",
                                "plddt": [.75] * sum(map(len, sequences.values())), "ptm": .6,
                                "iptm": .5 if request["mode"] == "complex" else None},
                    "artifacts": [{"name": "prediction.cif", "kind": "coordinates", "raw": coordinates(sequences)}]}
        return {"metrics": {**masked_metrics([math.log(.5)]*len(request["sequence"])), "model": "ESMC-300M"}, "artifacts": []}

    async def cancel(self, id):
        self.cancelled.append(id)

    async def recover(self, id):
        if id in self.cancelled:
            return {"result": {"error": "CANCELLED", "artifacts": []}}
        return {}


def setup(tmp_path, remote=None):
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"model": "anthropic/test", "choices": [{
        "finish_reason": "stop", "message": {"content": "Actual computational summary."}}]}))
    provider = OpenRouter("sentinel-secret", httpx.AsyncClient(transport=transport))
    store = DesignStore(tmp_path)
    return DesignService(store, provider, remote or Remote())


def payload():
    return DesignStart(id=uuid4(), prompt="A generic benign-target test", model="anthropic/test",
                       structure=coordinates({"A": "AC"}).decode(), format="cif", chain="A")


def standalone_state(id):
    return {"id": id, "status": "running", "started": "2026-09-16T00:00:00+00:00", "jobs": [],
            "candidates": [], "events": [], "artifacts": [], "logs": [], "downloads": [], "text": "",
            "record_count": 1}


@pytest.fixture
def retry_delays(monkeypatch):
    """Skip wall-clock backoff while retaining an actual scheduling checkpoint."""
    delays = []
    sleep = asyncio.sleep

    async def advance(delay):
        delays.append(delay)
        await sleep(0)

    monkeypatch.setattr("runner.design.asyncio.sleep", advance)
    return delays


@pytest.mark.parametrize("failure", [OSError, TimeoutError, "CONNECTION_LOST"])
@pytest.mark.parametrize("exhausted", [False, True])
def test_poll_retries_preserve_call_identity_and_restart_without_resubmission(
        tmp_path, retry_delays, failure, exhausted):
    class DisconnectingRemote(Remote):
        def __init__(self):
            super().__init__()
            self.polls = []
            self.remaining = 3 if exhausted else 2

        async def poll(self, call_id):
            self.polls.append(call_id)
            if self.remaining:
                self.remaining -= 1
                if failure == "CONNECTION_LOST":
                    raise DesignError(failure, "Private transport detail")
                raise failure("Private transport detail")
            return await super().poll(call_id)

    async def run():
        remote = DisconnectingRemote()
        service = setup(tmp_path, remote)
        id = str(uuid4())
        request = {"sequence": "AC"}
        service.store.create(id, {"prompt": "Score supplied sequence", "settings": request, "model": "none"},
                             standalone_state(id))
        try:
            if exhausted:
                with pytest.raises(DesignError) as error:
                    await service.scientific(id, "esmc", request)
                assert error.value.code == "SUBMISSION_UNCERTAIN"
                _, state = service.store.get(id)
                assert state["status"] == "recovering"
                assert state["jobs"][0]["status"] == "running"
                assert state["downloads"] == []
                assert [event["name"] for event in state["events"]] == ["scientific_dispatch"]
                await service.provider.client.aclose()
                service.store.close()
                service = setup(tmp_path, remote)
                await service.recover()
                assert service.snapshot(id)["status"] == "interrupted"
                assert service.snapshot(id)["evidence_status"] == "verified"
            else:
                result = await service.scientific(id, "esmc", request)
                assert result["metrics"]["length"] == 2
                # An identical completed call must reuse its durable result.
                assert await service.scientific(id, "esmc", request) == result
            _, state = service.store.get(id)
            job = state["jobs"][0]
            assert job["status"] == "completed"
            assert remote.calls == ["esmc"]
            assert remote.polls == [job["call_id"]] * (4 if exhausted else 3)
            assert retry_delays == [1, 2]
            assert [event["name"] for event in state["events"]] == ["scientific_dispatch", "esmc"]
            assert "Private transport detail" not in str(service.snapshot(id))
        finally:
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


@pytest.mark.parametrize("recovery", ["partial", "missing", "unavailable"])
def test_execution_failure_retains_recovered_bytes_without_accepting_candidates(tmp_path, recovery):
    class FailedRemote(Remote):
        async def poll(self, call_id):
            raise DesignError("EXECUTION_FAILED", "Worker failed")

        async def recover(self, job_id):
            if recovery == "unavailable":
                raise OSError("Private recovery detail")
            if recovery == "missing":
                return {}
            return {"result": {"error": "EXECUTION_FAILED", "artifacts": [
                {"name": "partial.txt", "kind": "log", "raw": b"partial worker output"}]}}

    async def run():
        remote = FailedRemote()
        service = setup(tmp_path, remote)
        id = str(uuid4())
        service.store.create(id, {"prompt": "Score", "settings": {}, "model": "none"}, standalone_state(id))
        try:
            result = await service.scientific(id, "esmc", {"sequence": "AC"})
            assert result["error"] == "EXECUTION_FAILED"
            _, state = service.store.get(id)
            assert state["jobs"][0]["status"] == "failed"
            assert state["candidates"] == []
            assert remote.calls == ["esmc"]
            assert len(state["events"]) == 2
            assert len(result["artifacts"]) == (1 if recovery == "partial" else 0)
            if recovery == "partial":
                ref = result["artifacts"][0]
                assert service.store.read(id, ref["sha256"], ref["name"]) == b"partial worker output"
        finally:
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


def test_cancel_during_retry_backoff_stops_workflow_retries_without_resubmission(tmp_path):
    class DisconnectedRemote(Remote):
        polls = 0

        async def poll(self, call_id):
            self.polls += 1
            raise OSError("connection interrupted")

    async def run():
        remote = DisconnectedRemote()
        service = setup(tmp_path, remote)
        try:
            id = service.start(payload())["id"]
            async with asyncio.timeout(5):
                while not any("reconnecting" in entry["message"] for entry in service.snapshot(id)["logs"]):
                    await asyncio.sleep(.001)
            state = await service.cancel(id)
            assert service.tasks[id].done()
            assert state["status"] == "needs_attention"
            assert state["cancel_requested"] is True
            assert state["downloads"] == []
            assert remote.calls == ["boltzgen"]
            # One workflow poll and one cancellation reconciliation poll; no retry.
            assert remote.polls == 2
            _, persisted = service.store.get(id)
            assert [event["name"] for event in persisted["events"]] == ["openrouter", "scientific_dispatch"]
            assert sum("reconnecting" in entry["message"] for entry in state["logs"]) == 1
        finally:
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


@pytest.mark.parametrize("unexpected", [False, True])
def test_nonretryable_poll_failure_is_visible_and_does_not_publish_or_resubmit(tmp_path, unexpected):
    class BrokenRemote(Remote):
        async def poll(self, call_id):
            if unexpected:
                raise RuntimeError("Private unexpected failure")
            raise DesignError("EVIDENCE_FAILED", "Private evidence detail")

    async def run():
        remote = BrokenRemote()
        service = setup(tmp_path, remote)
        try:
            id = service.start(payload())["id"]
            if unexpected:
                with pytest.raises(RuntimeError, match="Private unexpected failure"):
                    await service.tasks[id]
            else:
                await service.tasks[id]
            state = service.snapshot(id)
            assert state["status"] == "needs_attention"
            assert state["downloads"] == []
            assert state["jobs"][0]["status"] == "running"
            assert remote.calls == ["boltzgen"]
            assert "Private" not in str(state)
            assert not any("reconnecting" in entry["message"] for entry in state["logs"])
        finally:
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


@pytest.mark.parametrize("case,code", [
    ("blank_prompt", "INVALID_REQUEST"),
    ("unsupported_model", "INVALID_REQUEST"),
    ("model_whitespace", "INVALID_REQUEST"),
    ("disabled_tools", "TOOL_UNAVAILABLE"),
    ("missing_key", "TOOL_UNAVAILABLE"),
    ("binding_position", "INVALID_TARGET"),
])
def test_invalid_start_creates_no_workflow_or_remote_work(tmp_path, case, code):
    async def run():
        service = setup(tmp_path)
        data = payload()
        if case == "blank_prompt":
            data.prompt = "   "
        elif case == "unsupported_model":
            data.model = "unknown/test"
        elif case == "model_whitespace":
            data.model = "anthropic/test model"
        elif case == "disabled_tools":
            service.transport.configured = False
        elif case == "missing_key":
            service.provider.key = ""
        else:
            data.settings = type(data.settings)(binding_residues=[3])
        try:
            with pytest.raises(DesignError) as error:
                service.start(data)
            assert error.value.code == code
            assert service.store.all() == []
            assert service.store.artifacts(str(data.id)) == []
            assert service.tasks == {}
            assert service.transport.calls == []
        finally:
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure_stage", ["poll", "cancel", "recover"])
def test_cancel_transport_failure_stays_unsealed_until_restart_reconciles(tmp_path, failure_stage):
    class UnavailableRemote(Remote):
        failure_active = True

        async def poll(self, call_id):
            if self.failure_active and failure_stage == "poll":
                raise OSError("Private poll detail")
            return None

        async def cancel(self, call_id):
            if self.failure_active and failure_stage == "cancel":
                raise DesignError("CANCELLATION_PENDING", "Private cancel detail")
            await super().cancel(call_id)

        async def recover(self, job_id):
            if self.failure_active and failure_stage == "recover":
                raise TimeoutError("Private recovery detail")
            return await super().recover(job_id)

    async def run():
        remote = UnavailableRemote()
        service = setup(tmp_path, remote)
        try:
            id = service.start(payload())["id"]
            while not remote.requests:
                await asyncio.sleep(.001)
            state = await service.cancel(id)
            call_id = state["jobs"][0]["call_id"]
            assert state["status"] == "needs_attention"
            assert state["cancel_requested"] is True
            assert state["downloads"] == []
            assert state["jobs"][0]["status"] == "running"
            assert "Private" not in str(state)
            await service.provider.client.aclose()
            service.store.close()
            service = setup(tmp_path, remote)
            remote.failure_active = False
            await service.recover()
            state = service.snapshot(id)
            assert state["status"] == "cancelled"
            assert state["jobs"][0]["status"] == "cancelled"
            assert state["jobs"][0]["call_id"] == call_id
            assert state["evidence_status"] == "verified"
            assert remote.calls == ["boltzgen"]
            count = state["record_count"]
            await service.recover()
            assert service.snapshot(id)["record_count"] == count
        finally:
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


def test_full_offline_pipeline_commits_actual_artifacts_and_exact_sequence(tmp_path):
    async def run():
        service = setup(tmp_path)
        data = payload()
        try:
            snapshot = service.start(data)
            id = snapshot["id"]
            await service.tasks[id]
            snapshot = service.snapshot(id)
            assert snapshot["status"] == "completed"
            assert service.transport.calls == ["boltzgen", "esmfold2", "esmfold2", "esmc"]
            assert len(snapshot["candidates"]) == 1
            assert service.start(data)["id"] == id
            assert len(service.transport.calls) == 4
            bundle_ref = next(a for a in snapshot["artifacts"] if a["name"] == "bundle.zip")
            trust_ref = next(a for a in snapshot["artifacts"] if a["name"] == "trust.json")
            path = tmp_path/"check.zip"
            path.write_bytes(service.store.read(id, bundle_ref["sha256"], bundle_ref["name"]))
            bundle = Bundle.load(path)
            trust = TrustBundle.model_validate_json(service.store.read(id, trust_ref["sha256"], trust_ref["name"]))
            assert verify_bundle(bundle, trust, submitted_sequence="A"*60).exit_code == 0
            assert verify_bundle(bundle, trust, submitted_sequence="C"+"A"*59).exit_code == 5
            assert snapshot["record_count"] == bundle.signed_tree_head.tree_size
            assert b"sentinel-secret" not in b"".join(bundle.blobs.values())
            assert any(r.record_type.value == "design_run" for r in bundle.records)
            damaged = bundle.model_copy(deep=True)
            structure = next(a for r in damaged.records for a in r.results if a.kind == "coordinates")
            damaged.blobs[structure.sha256] += b"changed"
            assert verify_bundle(damaged, trust, submitted_sequence="A"*60).exit_code == 2
            withheld = bundle.model_copy(deep=True)
            withheld.records = withheld.records[:-1]
            assert verify_bundle(withheld, trust, submitted_sequence="A"*60).exit_code == 6
        finally:
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


def test_standalone_proteinmpnn_preserves_samples_and_exact_sequence_evidence_without_model_call(tmp_path):
    from test_design_inputs import backbone
    from test_design_workers import proteinmpnn_bytes
    from runner.design_inputs import prepare_backbone

    prepared = prepare_backbone(backbone(), "cif")
    request = {"backbone_cif": prepared["prepared_cif"], "sequences": prepared["sequences"],
               "chain_map": prepared["chain_map"], "mapping": prepared["mapping"],
               "design_chains": ["A", "B"], "count": 2, "sampling_temperature": .1,
               "seed": 1, "model": "v_48_020"}
    fasta, scores = proteinmpnn_bytes()
    fasta = fasta.replace(b"AC/CA", b"AC/AC", 1)
    parsed, metrics = parse_proteinmpnn_outputs(fasta, scores, request)

    class ProteinRemote(Remote):
        async def poll(self, id):
            return {"candidates": parsed, "metrics": metrics,
                    "artifacts": [{"name": "backbone.fa", "kind": "fasta", "raw": fasta},
                                  {"name": "backbone.npz", "kind": "metric", "raw": scores}]}

    async def run():
        provider = OpenRouter("", httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: pytest.fail("Standalone tool made an OpenRouter call"))))
        service = DesignService(DesignStore(tmp_path), provider, ProteinRemote())
        id = str(uuid4())
        service.store.create(id, {"prompt": "Standalone ProteinMPNN", "settings": request, "model": "none"},
                             standalone_state(id))
        try:
            result = await service.scientific(id, "proteinmpnn", request)
            _, state = service.store.get(id)
            state["status"] = "completed"
            service.store.save(id, state)
            service.publish(id)
            state = service.snapshot(id)
            assert len(state["candidates"]) == 4
            assert [(candidate["sample"], candidate["chain"], candidate["sequence"])
                    for candidate in state["candidates"]] == [(1, "A", "AA"), (1, "B", "CC"),
                                                               (2, "A", "AA"), (2, "B", "CC")]
            assert result["metrics"] == metrics and state["evidence_status"] == "verified"
            bundle_ref = next(a for a in state["artifacts"] if a["name"] == "bundle.zip")
            trust_ref = next(a for a in state["artifacts"] if a["name"] == "trust.json")
            path = tmp_path / "proteinmpnn.zip"
            path.write_bytes(service.store.read(id, bundle_ref["sha256"], bundle_ref["name"]))
            bundle = Bundle.load(path)
            trust = TrustBundle.model_validate_json(service.store.read(id, trust_ref["sha256"], trust_ref["name"]))
            assert verify_bundle(bundle, trust, submitted_sequence="AA").exit_code == 0
            assert verify_bundle(bundle, trust, submitted_sequence="AC").exit_code == 5
            assert [r.record_type.value for r in bundle.records].count("design_run") == 1
        finally:
            await provider.client.aclose()
            service.store.close()
    asyncio.run(run())


def test_standalone_proteinmpnn_rejects_forged_request_summary(tmp_path):
    from test_design_inputs import backbone
    from test_design_workers import proteinmpnn_bytes
    from runner.design_inputs import prepare_backbone

    prepared = prepare_backbone(backbone(), "cif")
    request = {"backbone_cif": prepared["prepared_cif"], "sequences": prepared["sequences"],
               "chain_map": prepared["chain_map"], "mapping": prepared["mapping"],
               "design_chains": ["A", "B"], "count": 2, "sampling_temperature": .1,
               "seed": 1, "model": "v_48_020"}
    fasta, scores = proteinmpnn_bytes()
    fasta = fasta.replace(b"AC/CA", b"AC/AC", 1)
    parsed, metrics = parse_proteinmpnn_outputs(fasta, scores, request)

    class ForgedSummary(Remote):
        async def poll(self, id):
            return {"candidates": parsed, "metrics": {**metrics, "seed": 2},
                    "artifacts": [{"name": "backbone.fa", "kind": "fasta", "raw": fasta}]}

    async def run():
        service = setup(tmp_path, ForgedSummary())
        id = str(uuid4())
        service.store.create(id, {"prompt": "Standalone ProteinMPNN", "settings": request, "model": "none"},
                             standalone_state(id))
        try:
            result = await service.scientific(id, "proteinmpnn", request)
            snapshot = service.snapshot(id)
            assert result["error"] == "OUTPUT_INVALID"
            assert snapshot["candidates"] == []
            assert snapshot["jobs"][0]["status"] == "failed"
            assert any(artifact["name"].endswith("backbone.fa") for artifact in snapshot["artifacts"])
        finally:
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


def test_scientific_boundary_rejects_unknown_tool_before_dispatch(tmp_path):
    async def run():
        service = setup(tmp_path)
        id = str(uuid4())
        service.store.create(id, {"prompt": "x"}, standalone_state(id))
        try:
            with pytest.raises(DesignError) as error:
                await service.scientific(id, "arbitrary-shell", {})
            assert error.value.code == "TOOL_UNAVAILABLE"
            assert service.transport.calls == []
        finally:
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


def test_unresolved_job_cannot_be_sealed_or_dispatched_again(tmp_path):
    async def run():
        remote = Remote("pending")
        service = setup(tmp_path, remote)
        try:
            id = service.start(payload())["id"]
            async with asyncio.timeout(5):
                while not remote.requests:
                    await asyncio.sleep(.001)
            service.publish(id)
            _, state = service.store.get(id)
            assert state["status"] == "needs_attention"
            assert state["downloads"] == []
            job = state["jobs"][0]
            for tool, request, code in [
                ("boltzgen", job["request"], "SUBMISSION_UNCERTAIN"),
                ("boltzgen", {**job["request"], "seed": 123}, "REQUEST_CONFLICT"),
                ("esmc", {"sequence": "AC"}, "CANCELLATION_PENDING"),
            ]:
                with pytest.raises(DesignError) as error:
                    await service.scientific(id, tool, request)
                assert error.value.code == code
            assert remote.calls == ["boltzgen"]
            assert len(service.snapshot(id)["jobs"]) == 1
        finally:
            await service.shutdown()
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure,expected", [("fold", "partial"), ("uncertain", "needs_attention")])
def test_partial_or_uncertain_result_is_never_completed(tmp_path, failure, expected):
    async def run():
        service = setup(tmp_path, Remote(failure))
        try:
            id = service.start(payload())["id"]
            await service.tasks[id]
            snapshot = service.snapshot(id)
            assert snapshot["status"] == expected
            assert service.transport.calls.count("boltzgen") == 1
            if failure == "uncertain":
                assert snapshot["downloads"] == []
                assert "private-remote-error" not in str(snapshot)
            else:
                assert snapshot["downloads"]
                assert len(snapshot["candidates"]) == 1
        finally:
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


def test_cancel_propagates_to_known_remote_call(tmp_path):
    async def run():
        remote = Remote("pending")
        service = setup(tmp_path, remote)
        try:
            id = service.start(payload())["id"]
            for _ in range(100):
                if remote.calls:
                    break
                await asyncio.sleep(.001)
            assert remote.calls == ["boltzgen"]
            snapshot = await service.cancel(id)
            assert snapshot["status"] == "cancelled"
            assert remote.cancelled == list(remote.requests)
            assert remote.calls == ["boltzgen"]
        finally:
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


def test_missing_complex_structure_is_rejected_before_scoring(tmp_path):
    class MissingStructure(Remote):
        async def poll(self, id):
            result = await super().poll(id)
            if self.requests[id][0] == "esmfold2":
                result["artifacts"] = []
            return result

    async def run():
        service = setup(tmp_path, MissingStructure())
        try:
            data = payload()
            id = service.start(data)["id"]
            await service.tasks[id]
            _, state = service.store.get(id)
            job = next(j for j in state["jobs"] if j["tool"] == "esmfold2")
            assert job["status"] == "failed"
            assert job["result"]["error"] == "OUTPUT_INVALID"
            assert service.snapshot(id)["status"] == "partial"
            assert service.snapshot(id)["downloads"]
            assert service.snapshot(id)["candidates"][0]["metrics"]["esmfold2-complex"] is None
        finally:
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


def test_changed_request_cannot_launch_a_second_generation(tmp_path):
    async def run():
        service = setup(tmp_path)
        try:
            data = payload()
            id = service.start(data)["id"]
            await service.tasks[id]
            with pytest.raises(DesignError) as error:
                service.start(data.model_copy(update={"prompt": "Different objective"}))
            assert error.value.code == "REQUEST_CONFLICT"
            assert service.transport.calls.count("boltzgen") == 1
        finally:
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


@pytest.mark.parametrize("bad_output", [{"artifacts": []}, {"candidates": "not a list", "artifacts": []},
    {"candidates": [{"sequence": "AX", "source": "bad"}], "artifacts": []}])
def test_malformed_generation_is_a_recorded_failure(tmp_path, bad_output):
    class InvalidRemote(Remote):
        async def poll(self, id):
            return bad_output
    async def run():
        service = setup(tmp_path, InvalidRemote())
        try:
            id = service.start(payload())["id"]
            await service.tasks[id]
            _, state = service.store.get(id)
            assert state["status"] == "failed"
            assert state["jobs"][0]["result"]["error"] == "OUTPUT_INVALID"
            assert state["downloads"]
            assert not state["candidates"]
        finally:
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


def test_zero_retained_candidates_does_not_fabricate_scoring_jobs(tmp_path):
    class EmptyRemote(Remote):
        async def poll(self, id):
            return {"candidates": [], "artifacts": [], "metrics": {"generated": 4, "retained": 0}}
    async def run():
        service = setup(tmp_path, EmptyRemote())
        try:
            id = service.start(payload())["id"]
            await service.tasks[id]
            state = service.snapshot(id)
            assert state["status"] == "completed"
            assert state["counts"] == {"requested": 4, "generated": 4, "retained": 0, "scored": 0}
            assert service.transport.calls == ["boltzgen"]
            assert state["downloads"]
        finally:
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


def test_cancellation_during_submission_keeps_and_cancels_acceptance_id(tmp_path):
    class SlowAcceptance(Remote):
        def __init__(self):
            super().__init__("pending")
            self.accepting, self.release = asyncio.Event(), asyncio.Event()
        async def submit(self, tool, id, request):
            self.accepting.set()
            await self.release.wait()
            return await super().submit(tool, id, request)
    async def run():
        remote = SlowAcceptance()
        service = setup(tmp_path, remote)
        try:
            id = service.start(payload())["id"]
            await remote.accepting.wait()
            cancellation = asyncio.create_task(service.cancel(id))
            await asyncio.sleep(0)
            remote.release.set()
            state = await cancellation
            assert state["status"] == "cancelled"
            assert remote.cancelled == list(remote.requests)
            assert len(remote.cancelled) == 1
            assert state["jobs"][0]["call_id"] == remote.cancelled[0]
        finally:
            await service.shutdown()
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


def test_recovery_uses_known_result_without_replaying_model_or_gpu(tmp_path):
    async def run():
        remote = Remote("pending")
        service = setup(tmp_path, remote)
        id = service.start(payload())["id"]
        while not remote.requests:
            await asyncio.sleep(.001)
        service.tasks[id].cancel()
        await service.tasks[id]
        await service.provider.client.aclose()
        keys = service.store.keys().trust_bundle()
        service.store.close()
        remote.failure = None
        restarted = setup(tmp_path, remote)
        try:
            await restarted.recover()
            state = restarted.snapshot(id)
            assert state["status"] == "interrupted"
            assert state["jobs"][0]["status"] == "completed"
            assert len(state["candidates"]) == 1
            assert state["downloads"]
            assert remote.calls == ["boltzgen"]
            assert restarted.store.keys().trust_bundle() == keys
            # Reconciliation of an already harvested result cannot add duplicate candidates/records.
            count = state["record_count"]
            await restarted.recover()
            assert restarted.snapshot(id)["record_count"] == count
        finally:
            await restarted.provider.client.aclose()
            restarted.store.close()
    asyncio.run(run())


@pytest.mark.parametrize("completed_before_cancel", [True, False])
def test_cancel_preserves_completion_observed_before_or_after_remote_cancel(tmp_path, completed_before_cancel):
    class CompletionRace(Remote):
        async def cancel(self, call_id):
            await super().cancel(call_id)
            self.failure = None

        async def recover(self, job_id):
            return {"result": await super().poll(job_id)}

    async def run():
        remote = CompletionRace("pending")
        service = setup(tmp_path, remote)
        try:
            id = service.start(payload())["id"]
            while not remote.requests:
                await asyncio.sleep(.001)
            if completed_before_cancel:
                remote.failure = None
            state = await service.cancel(id)
            assert state["status"] == "cancelled"
            assert state["jobs"][0]["status"] == "completed"
            assert len(state["candidates"]) == 1
            assert state["evidence_status"] == "verified"
            assert remote.cancelled == ([] if completed_before_cancel else list(remote.requests))
            assert remote.calls == ["boltzgen"]
        finally:
            await service.shutdown()
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


@pytest.mark.parametrize("mismatch", [False, True])
def test_uncertain_submission_reconciles_only_matching_start_manifest(tmp_path, mismatch):
    class StartedRemote(Remote):
        async def recover(self, job_id):
            _, state = service.store.get(id)
            job = next(j for j in state["jobs"] if j["id"] == job_id)
            import hashlib
            from runner.design_store import json_bytes
            return {"start": {"id": job_id, "tool": "esmc" if mismatch else job["tool"], "call_id": "accepted-call", "attempt": 1,
                              "request_sha256": hashlib.sha256(json_bytes(job["request"])).hexdigest()}}
        async def poll(self, call_id):
            return None
    async def run():
        nonlocal_service = setup(tmp_path, StartedRemote("uncertain"))
        return nonlocal_service
    async def scenario():
        nonlocal service, id
        service = await run()
        try:
            id = service.start(payload())["id"]
            await service.tasks[id]
            await service.recover()
            state = service.snapshot(id)
            assert state["jobs"][0]["call_id"] == (None if mismatch else "accepted-call")
            assert service.transport.calls == ["boltzgen"]
            assert state["downloads"] == []
            if not mismatch:
                # A start manifest and an acknowledged cancel request are not a terminal observation.
                assert (await service.cancel(id))["status"] == "needs_attention"
                assert service.snapshot(id)["downloads"] == []
                assert service.transport.cancelled == ["accepted-call"]
        finally:
            await service.provider.client.aclose()
            service.store.close()
    service, id = None, None
    asyncio.run(scenario())


def test_evidence_failure_preserves_scientific_results_without_publishing_success(tmp_path, monkeypatch):
    def failed_capture(*args, **kwargs):
        raise ValueError("Publication failed")
    monkeypatch.setattr("runner.design.capture", failed_capture)
    async def run():
        service = setup(tmp_path)
        try:
            id = service.start(payload())["id"]
            await service.tasks[id]
            state = service.snapshot(id)
            assert state["evidence_status"] == "unverified"
            assert state["downloads"] == []
            assert state["candidates"]
            assert state["error_code"] == "EVIDENCE_FAILED"
            assert all(a["name"] != "bundle.zip" for a in state["artifacts"])
        finally:
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


def test_uncertain_acceptance_harvests_identity_bound_result_without_resubmitting(tmp_path):
    class AcceptedRemote(Remote):
        async def recover(self, job_id):
            import hashlib
            from runner.design_store import json_bytes
            _, state = service.store.get(id)
            job = state["jobs"][0]
            self.requests["accepted-call"] = (job["tool"], job["request"])
            result = await super().poll("accepted-call")
            start = {"id": job_id, "tool": job["tool"], "attempt": job["attempt"],
                     "call_id": "accepted-call", "request_sha256": hashlib.sha256(json_bytes(job["request"])).hexdigest()}
            return {"start": start, "result": {**result, "execution": start}}

    async def run():
        nonlocal service, id
        service = setup(tmp_path, AcceptedRemote("uncertain"))
        try:
            id = service.start(payload())["id"]
            await service.tasks[id]
            await service.recover()
            state = service.snapshot(id)
            assert state["status"] == "interrupted"
            assert state["jobs"][0]["status"] == "completed"
            assert state["jobs"][0]["call_id"] == "accepted-call"
            assert len(state["candidates"]) == 1
            assert state["evidence_status"] == "verified"
            assert service.transport.calls == ["boltzgen"]
        finally:
            await service.provider.client.aclose()
            service.store.close()
    service, id = None, None
    asyncio.run(run())


def test_final_model_error_cannot_erase_successful_scientific_outputs(tmp_path):
    async def run():
        service = setup(tmp_path)
        calls = []
        def upstream(request):
            calls.append(request)
            return httpx.Response(200, json={"model": "anthropic/test", "choices": [{"finish_reason": "stop",
                "message": {"content": "Describe the objective."}}]}) if len(calls) == 1 else httpx.Response(500)
        await service.provider.client.aclose()
        service.provider.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        try:
            id = service.start(payload())["id"]
            await service.tasks[id]
            state = service.snapshot(id)
            assert state["status"] == "partial"
            assert all(job["status"] == "completed" for job in state["jobs"])
            assert state["candidates"] and state["evidence_status"] == "verified"
            assert state["text"] == ""
            assert b'per_residue_log_probability' not in calls[-1].content
            assert b'"plddt": [' not in calls[-1].content
            assert any(a["kind"] == "metric" for a in state["artifacts"])
        finally:
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())
