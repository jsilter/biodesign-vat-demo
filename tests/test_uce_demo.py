"""The UCE composition job: pinned public input, standard-library source, checkable result."""

import collections
import io
import json
import runpy
import zipfile
from pathlib import Path

import pytest

from runner.dynamic_contracts import validate_dynamic_proposal

ROOT = Path(__file__).resolve().parents[1]
DEMO = runpy.run_path(str(ROOT / "scripts" / "uce_demo.py"))
FIXTURE = ROOT / "fixtures" / "uce_100set_subset.json"


def synthetic_zip(tmp_path: Path, rows: int = 279) -> Path:
    lines = [f"NC_{index % 5:06d}.1\t{1000 * index}\t{1000 * index + 100 + index % 7}" for index in range(rows)]
    path = tmp_path / "supplement.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(DEMO["SET_FILE"], "\n".join(lines) + "\n")
        archive.writestr("other.txt", "ignored")
    return path


def test_selection_is_evenly_spaced_and_reproducible():
    rows = list(range(279))
    assert DEMO["select_elements"](rows, 10) == [0, 27, 55, 83, 111, 139, 167, 195, 223, 251]
    assert DEMO["select_elements"](rows, 279) == rows
    for count in (0, 280):
        with pytest.raises(ValueError):
            DEMO["select_elements"](rows, count)


def test_efetch_region_is_one_based_inclusive_and_fasta_is_checked():
    assert "seq_start=10298972&seq_stop=10299071" in DEMO["efetch_url"]("NC_000001.11", 10298971, 10299071)
    assert DEMO["parse_fasta"](">NC_000001.11:1-4 test\nac\ngt\n", 4) == "ACGT"
    for text in (">x\nACG\n", ">x\nACGN\n", "no header\n"):
        with pytest.raises(ValueError):
            DEMO["parse_fasta"](text, 4)


def test_fixture_pins_the_supplement_and_every_fetched_sequence(tmp_path):
    zip_path = synthetic_zip(tmp_path)
    requested = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(url, timeout):
        requested.append(url)
        length = int(url.split("seq_stop=")[1].split("&")[0]) - int(url.split("seq_start=")[1].split("&")[0]) + 1
        return Response(f">record\n{'ACGT' * (length // 4)}{'A' * (length % 4)}\n".encode())

    fixture = DEMO["build_fixture"](zip_path, 3, opener=opener, pause=0)
    with zipfile.ZipFile(zip_path) as archive:
        file_bytes = archive.read(DEMO["SET_FILE"])
    assert fixture["supplement"]["zip_sha256"] == DEMO["sha256_hex"](zip_path.read_bytes())
    assert fixture["supplement"]["file_sha256"] == DEMO["sha256_hex"](file_bytes)
    assert len(requested) == 3 and all(url.startswith(DEMO["EFETCH"]) for url in requested)
    assert [len(e["sequence"]) == e["end"] - e["start"] for e in fixture["elements"]] == [True] * 3
    validate_dynamic_proposal(DEMO["build_proposal"](fixture))


def test_wrong_element_count_in_the_supplement_is_refused(tmp_path):
    with pytest.raises(ValueError, match="expected 279"):
        DEMO["read_set"](synthetic_zip(tmp_path, rows=278))


def test_committed_fixture_matches_an_independent_count_and_the_real_zip_digest():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["supplement"]["zip_sha256"] == "562fbbfd429b041609259e39e17ab8c1d43cc49c8ed6cb61f5c15e387d661caf"
    assert len(fixture["elements"]) == 10
    first = fixture["elements"][0]
    assert (first["accession"], first["start"], first["end"]) == ("NC_000001.11", 10298971, 10299071)
    assert first["sequence"].startswith("GCTTCGGCAGCACATATACTAAAATTGGAACGATACAGAGAAGATTAGCA")
    counts = collections.Counter("".join(e["sequence"] for e in fixture["elements"]))
    total = sum(counts.values())
    result = DEMO["run_locally"](fixture)
    assert result["total_bp"] == total and result["counts"] == dict(counts)
    assert result["at_per_base"] == round((counts["A"] + counts["T"]) / total / 2, 4)
    assert result["cg_per_base"] == round((counts["C"] + counts["G"]) / total / 2, 4)
    assert abs(result["at_per_base"] - 0.312) < 0.02 and abs(result["cg_per_base"] - 0.188) < 0.02
    proposal = validate_dynamic_proposal(DEMO["build_proposal"](fixture))
    assert proposal.packages == [] and proposal.resources.network is False and proposal.authorized


def test_job_refuses_a_sequence_that_disagrees_with_its_coordinates():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["elements"][0]["sequence"] = fixture["elements"][0]["sequence"][:-1]
    with pytest.raises(ValueError, match="declared coordinates"):
        DEMO["run_locally"](fixture)


class LocalDynamicTransport:
    """Runs the approved source here instead of on Modal; the service and evidence path are real."""
    configured = True

    def __init__(self, store):
        from runner.dynamic_worker import dynamic_result_adapter
        from vatlog.execution import VatRecorder
        from vatlog.local import LocalFunction
        from vatlog.node import FunctionIdentity
        from runner.dynamic_worker import launch_dynamic
        self.recorder = VatRecorder(store.root / "executions", store.keys().recorder)
        store.execution_recorder = self.recorder
        self.function = LocalFunction(lambda execution_id, proposal: launch_dynamic(proposal),
                                      descriptor=FunctionIdentity(app_name="test-dynamic", function_name="dynamic-job"),
                                      result_adapter=dynamic_result_adapter).with_recorder(self.recorder)
        self.prepared, self.results, self.submissions = {}, {}, 0

    def prepare(self, id, value):
        self.prepared[id] = self.function.prepare_call((id, value), {}, execution_id=id)

    async def submit(self, id, value):
        self.submissions += 1
        self.results[id] = await self.prepared[id].run.aio()
        return "local:" + id

    async def poll(self, call_id):
        return self.results[call_id.removeprefix("local:")].value

    def result(self, id):
        return self.results.get(id)

    async def cancel(self, call_id):
        return None

    async def drain(self):
        await self.recorder.drain()


def test_dispatch_seals_one_run_and_the_tampered_copy_fails_verification(tmp_path):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    proposal = DEMO["build_proposal"](fixture)
    digest = validate_dynamic_proposal(proposal).digest
    with pytest.raises(ValueError, match="--authorized"):
        DEMO["dispatch"](proposal, tmp_path / "out", approve_digest=digest, authorized=False, transport=LocalDynamicTransport)
    with pytest.raises(ValueError, match="does not match"):
        DEMO["dispatch"](proposal, tmp_path / "out", approve_digest="0" * 64, authorized=True, transport=LocalDynamicTransport)
    summary = DEMO["dispatch"](proposal, tmp_path / "out", approve_digest=digest, authorized=True, transport=LocalDynamicTransport)
    assert summary["status"] == "completed" and summary["digest"] == digest
    assert summary["result"] == DEMO["run_locally"](fixture)
    assert (summary["bundle_outcome"], summary["bundle_exit_code"]) == ("OK", 0)
    assert (summary["tampered_outcome"], summary["tampered_exit_code"]) == ("TAMPERED", 2)
    for name in ("bundle.zip", "trust.json", "tampered-bundle.zip", "result.json"):
        assert (tmp_path / "out" / name).is_file()
    assert json.loads((tmp_path / "out" / "result.json").read_text())["elements"] == 10
