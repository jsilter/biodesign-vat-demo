"""Adapter math, coordinate contracts and release-lock checks without GPU calls."""

import hashlib
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from runner.design_inputs import DesignError, validate_fold_metrics
from runner.design_store import json_bytes
from runner.design_workers import (BOLTZ_REVISION, ESM_REVISION, ESM3_REVISION, PROTEINMPNN_REVISION,
                                  PROTEINMPNN_REPOSITORY, PROTEINMPNN_CHECKPOINT,
                                  PROTEINMPNN_SOLUBLE_CHECKPOINT, PROTEINMPNN_LICENSE, WEIGHTS, CHEMISTRY,
                                  esmc_masked_score, fold_result, validate_weight_lock,
                                  collect_boltz_artifacts, parse_proteinmpnn_outputs,
                                  run_proteinmpnn)


def test_masked_adapter_scores_only_one_hidden_residue_per_row_over_full_vocabulary():
    torch = pytest.importorskip("torch")
    batches = []
    class Tokenizer:
        mask_token_id = 2
        def __call__(self, sequence, **kwargs):
            return {"input_ids": torch.tensor([[0, *[3 if aa == "A" else 4 for aa in sequence], 1]])}
    class Model:
        device = torch.device("cpu")
        def __call__(self, input_ids, attention_mask):
            batches.append(input_ids.clone())
            logits = torch.zeros((*input_ids.shape, 6))
            # Context-dependent predictions include a strong non-amino-acid competitor.
            for row in range(len(input_ids)):
                context = int(input_ids[row, 1:-1].sum())
                logits[row, :, 3] = context / 10
                logits[row, :, 4] = -context / 10
                logits[row, :, 5] = 2
            return SimpleNamespace(logits=logits)
    result = esmc_masked_score(Model(), Tokenizer(), "ACA", batch_size=2)
    expected = []
    tokens = [3, 4, 3]
    for index, token in enumerate(tokens):
        context = sum(tokens) - token + 2
        weights = [1, 1, 1, math.exp(context/10), math.exp(-context/10), math.exp(2)]
        expected.append(math.log(weights[token]/sum(weights)))
    assert result["per_residue_log_probability"] == pytest.approx(expected, abs=1e-7)
    assert result["pseudo_perplexity"] == pytest.approx(math.exp(-sum(expected)/3))
    rows = torch.cat(batches)
    assert rows.shape == (3, 5)
    assert rows[:, 0].tolist() == [0, 0, 0] and rows[:, -1].tolist() == [1, 1, 1]
    assert (rows[:, 1:-1] == 2).sum(dim=1).tolist() == [1, 1, 1]
    assert rows.tolist() == [[0, 2, 4, 3, 1], [0, 3, 2, 3, 1], [0, 3, 4, 2, 1]]


@pytest.mark.parametrize("override", [{"plddt": [75, 80]}, {"plddt": [.75]}, {"mean_plddt": .1},
    {"iptm": .5}, {"pae": [[0, 1], [float('nan'), 0]]}, {"ptm": float('inf')}])
def test_folding_metrics_reject_wrong_scales_dimensions_or_monomer_interface_values(override):
    metrics = {"plddt_raw_scale": "0–1", "plddt": [.75, .8], "mean_plddt": .775, "ptm": .5, "iptm": None, **override}
    with pytest.raises(DesignError):
        validate_fold_metrics(metrics, 2, complex=False)


def test_fold_result_retains_raw_arrays_and_excludes_monomer_iptm():
    torch = pytest.importorskip("torch")
    from test_design_workflow import coordinates
    result = SimpleNamespace(plddt=torch.tensor([.5, .75]), ptm=.6, iptm=.9,
        pae=torch.tensor([[0., 2.], [3., 0.]]), complex=SimpleNamespace(to_mmcif=lambda: coordinates({"B": "AC"}).decode()))
    metrics, artifacts = fold_result(result, {"B": "AC"})
    assert metrics["mean_plddt"] == .625
    assert metrics["plddt"] == [.5, .75]
    assert metrics["iptm"] is None
    assert metrics["pae"] == [[0., 2.], [3., 0.]]
    assert artifacts[0]["raw"] == coordinates({"B": "AC"})
    with pytest.raises(DesignError, match="do not match"):
        fold_result(result, {"B": "AA"})


def test_staged_release_requires_review_pins_and_matching_file_bytes(tmp_path):
    (tmp_path / "esmc").mkdir()
    (tmp_path / "esmc/config.json").write_bytes(b"{}")
    (tmp_path / "esmc/model.safetensors").write_bytes(b"checkpoint-test-bytes")
    lock = {"reviewed": True, "esm_revision": ESM_REVISION, "esm3_revision": ESM3_REVISION,
            "boltz_revision": BOLTZ_REVISION,
            "proteinmpnn_revision": PROTEINMPNN_REVISION,
            "proteinmpnn_repository": PROTEINMPNN_REPOSITORY,
            "repositories": {k: list(v) for k, v in WEIGHTS.items()}, "chemistry": list(CHEMISTRY),
            "files": {"esmc/config.json": hashlib.sha256(b"{}").hexdigest(),
                      "esmc/model.safetensors": hashlib.sha256(b"checkpoint-test-bytes").hexdigest()}}
    lock["identity"] = hashlib.sha256(json_bytes(lock)).hexdigest()
    validate_weight_lock(tmp_path, lock, "esmc")
    (tmp_path / "esmc/config.json").write_bytes(b"changed")
    with pytest.raises(DesignError, match="digest mismatch"):
        validate_weight_lock(tmp_path, lock, "esmc")
    lock["reviewed"] = False
    with pytest.raises(DesignError, match="unreviewed"):
        validate_weight_lock(tmp_path, lock, "esmc")


def proteinmpnn_request():
    return {"backbone_cif": "prepared-cif", "sequences": {"A": "AC", "B": "CA"},
            "chain_map": [{"original_chain": "target", "prepared_chain": "A"},
                          {"original_chain": "binder", "prepared_chain": "B"}],
            "mapping": [{"prepared_chain": chain, "prepared_index": index,
                         "original_chain": "target" if chain == "A" else "binder",
                         "original_residue": str(index), "residue_name": name}
                        for chain, names in {"A": ["ALA", "CYS"], "B": ["CYS", "ALA"]}.items()
                        for index, name in enumerate(names, 1)],
            "design_chains": ["A", "B"], "count": 2, "sampling_temperature": .1,
            "seed": 1, "model": "v_48_020"}


def proteinmpnn_bytes(score=(.7291, .7414), global_score=(.9330, .9355)):
    np = pytest.importorskip("numpy")
    import io
    stream = io.BytesIO()
    np.savez(stream, score=np.array(score, dtype=np.float32),
             global_score=np.array(global_score, dtype=np.float32))
    fasta = (f">backbone, score=1.1, global_score=1.2, fixed_chains=[], designed_chains=['A', 'B'], "
             f"model_name=v_48_020, git_hash={PROTEINMPNN_REVISION}, seed=1\nAC/CA\n"
             ">T=0.1, sample=1, score=0.7291, global_score=0.9330, seq_recovery=0.5\nAA/CC\n"
             ">T=0.1, sample=2, score=0.7414, global_score=0.9355, seq_recovery=0.5\nAA/CC\n").encode()
    return fasta, stream.getvalue()


def test_proteinmpnn_parser_binds_chains_scores_and_preserves_duplicate_samples():
    fasta, scores = proteinmpnn_bytes()
    candidates, metrics = parse_proteinmpnn_outputs(fasta, scores, proteinmpnn_request())
    assert [(candidate["sample"], candidate["chain"], candidate["sequence"])
            for candidate in candidates] == [(1, "A", "AA"), (1, "B", "CC"),
                                               (2, "A", "AA"), (2, "B", "CC")]
    assert candidates[0]["metrics"] == {"score": .7291, "global_score": .933,
                                         "seq_recovery": .5, "sampling_temperature": .1}
    assert metrics == {"samples": 2, "designed_chains": ["A", "B"], "fixed_chains": [],
                       "model": "v_48_020", "seed": 1, "sampling_temperature": .1}


@pytest.mark.parametrize("mutation", ["missing", "bad_chain", "bad_score", "object_array",
                                      "complex_array", "native_nonfinite", "oversized"])
def test_proteinmpnn_parser_rejects_inconsistent_or_unsafe_output(mutation):
    np = pytest.importorskip("numpy")
    import io
    fasta, scores = proteinmpnn_bytes()
    if mutation == "missing":
        fasta = b"\n".join(fasta.splitlines()[:-2]) + b"\n"
    elif mutation == "bad_chain":
        fasta = fasta.replace(b"AA/CC", b"AAA/CC", 1)
    elif mutation == "bad_score":
        fasta = fasta.replace(b"score=0.7291", b"score=0.1000")
    elif mutation == "native_nonfinite":
        fasta = fasta.replace(b"score=1.1", b"score=nan", 1)
    elif mutation == "oversized":
        fasta += b" " * (1024 * 1024)
    elif mutation == "object_array":
        stream = io.BytesIO()
        np.savez(stream, score=np.array([object(), object()], dtype=object),
                 global_score=np.array([.933, .9355]))
        scores = stream.getvalue()
    elif mutation == "complex_array":
        stream = io.BytesIO()
        np.savez(stream, score=np.array([.7291j, .7414j]),
                 global_score=np.array([.933, .9355]))
        scores = stream.getvalue()
    with pytest.raises(DesignError):
        parse_proteinmpnn_outputs(fasta, scores, proteinmpnn_request())


def test_proteinmpnn_release_lock_requires_exact_official_checkpoint(tmp_path):
    (tmp_path / "proteinmpnn").mkdir()
    (tmp_path / "proteinmpnn/v_48_020.pt").write_bytes(b"not-the-reviewed-checkpoint")
    (tmp_path / "proteinmpnn/LICENSE").write_text("MIT")
    lock = {"reviewed": True, "esm_revision": ESM_REVISION, "esm3_revision": ESM3_REVISION,
            "boltz_revision": BOLTZ_REVISION,
            "proteinmpnn_revision": PROTEINMPNN_REVISION,
            "proteinmpnn_repository": PROTEINMPNN_REPOSITORY,
            "repositories": {k: list(v) for k, v in WEIGHTS.items()}, "chemistry": list(CHEMISTRY),
            "files": {"proteinmpnn/v_48_020.pt": PROTEINMPNN_CHECKPOINT,
                      "proteinmpnn/LICENSE": PROTEINMPNN_LICENSE}}
    lock["identity"] = hashlib.sha256(json_bytes(lock)).hexdigest()
    with pytest.raises(DesignError, match="digest mismatch"):
        validate_weight_lock(tmp_path, lock, "proteinmpnn")


def test_proteinmpnn_runner_constructs_fixed_arguments_and_parses_outputs(tmp_path, monkeypatch):
    from test_design_inputs import backbone
    from runner.design_inputs import prepare_backbone
    from types import SimpleNamespace

    prepared = prepare_backbone(backbone(), "cif")
    request = {"backbone_cif": prepared["prepared_cif"], "sequences": prepared["sequences"],
               "chain_map": prepared["chain_map"], "mapping": prepared["mapping"],
               "design_chains": ["A", "B"], "count": 2, "sampling_temperature": .1,
               "seed": 1, "model": "v_48_020"}
    fasta, scores = proteinmpnn_bytes()
    fasta = fasta.replace(b"AC/CA", b"AC/AC", 1)
    observed = []
    def execute(command, **options):
        observed.append((command, options))
        output = Path(command[command.index("--out_folder") + 1])
        (output / "seqs").mkdir(parents=True)
        (output / "scores").mkdir()
        (output / "seqs/backbone.fa").write_bytes(fasta)
        (output / "scores/backbone.npz").write_bytes(scores)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("subprocess.run", execute)
    result = run_proteinmpnn(request, tmp_path / "weights", tmp_path / "work", source=tmp_path / "source")
    command, options = observed[0]
    assert command[command.index("--pdb_path_chains") + 1] == "A B"
    assert command[command.index("--seed") + 1] == "1"
    assert command[command.index("--sampling_temp") + 1] == "0.1"
    assert command[command.index("--path_to_model_weights") + 1] == str(tmp_path / "weights/proteinmpnn")
    assert options == {"cwd": tmp_path / "source", "capture_output": True, "timeout": 1740, "check": False}
    assert len(result["candidates"]) == 4
    assert {artifact["kind"] for artifact in result["artifacts"]} == {"fasta", "metric"}


def test_soluble_proteinmpnn_uses_distinct_staged_checkpoint_and_variant_flag(tmp_path, monkeypatch):
    from test_design_inputs import backbone
    from runner.design_inputs import prepare_backbone

    prepared = prepare_backbone(backbone(), "cif")
    request = {"backbone_cif": prepared["prepared_cif"], "sequences": prepared["sequences"],
               "chain_map": prepared["chain_map"], "mapping": prepared["mapping"],
               "design_chains": ["A", "B"], "count": 2, "sampling_temperature": .1,
               "seed": 1, "model": "v_48_020"}
    fasta, scores = proteinmpnn_bytes()
    fasta = fasta.replace(b"AC/CA", b"AC/AC", 1)
    observed = []
    def execute(command, **options):
        observed.append(command)
        output = Path(command[command.index("--out_folder") + 1])
        (output / "seqs").mkdir(parents=True)
        (output / "scores").mkdir()
        (output / "seqs/backbone.fa").write_bytes(fasta)
        (output / "scores/backbone.npz").write_bytes(scores)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("subprocess.run", execute)
    result = run_proteinmpnn(request, tmp_path / "weights", tmp_path / "work",
                             source=tmp_path / "source", variant="soluble")
    command = observed[0]
    assert command[command.index("--path_to_model_weights") + 1] == str(tmp_path / "weights/proteinmpnn-soluble")
    assert command[-1] == "--use_soluble_model"
    assert result["metrics"]["variant"] == "soluble"


def test_soluble_proteinmpnn_release_lock_has_distinct_reviewed_digest(tmp_path):
    directory = tmp_path / "proteinmpnn-soluble"
    directory.mkdir()
    directory.joinpath("v_48_020.pt").write_bytes(b"not-reviewed")
    directory.joinpath("LICENSE").write_bytes(b"not-reviewed")
    lock = {"reviewed": True, "esm_revision": ESM_REVISION, "esm3_revision": ESM3_REVISION,
            "boltz_revision": BOLTZ_REVISION,
            "proteinmpnn_revision": PROTEINMPNN_REVISION,
            "proteinmpnn_repository": PROTEINMPNN_REPOSITORY,
            "repositories": {k: list(v) for k, v in WEIGHTS.items()}, "chemistry": list(CHEMISTRY),
            "files": {"proteinmpnn-soluble/v_48_020.pt": PROTEINMPNN_SOLUBLE_CHECKPOINT,
                      "proteinmpnn-soluble/LICENSE": PROTEINMPNN_LICENSE}}
    lock["identity"] = hashlib.sha256(json_bytes(lock)).hexdigest()
    with pytest.raises(DesignError, match="digest mismatch"):
        validate_weight_lock(tmp_path, lock, "proteinmpnn-soluble")


def test_proteinmpnn_runner_maps_process_timeout_to_safe_failure(tmp_path, monkeypatch):
    import subprocess
    from test_design_inputs import backbone
    from runner.design_inputs import prepare_backbone

    prepared = prepare_backbone(backbone(), "cif")
    request = {"backbone_cif": prepared["prepared_cif"], "sequences": prepared["sequences"],
               "chain_map": prepared["chain_map"], "mapping": prepared["mapping"],
               "design_chains": ["B"], "count": 1, "sampling_temperature": .1,
               "seed": 1, "model": "v_48_020"}
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs:
                        (_ for _ in ()).throw(subprocess.TimeoutExpired(args[0], kwargs["timeout"])))
    with pytest.raises(DesignError) as error:
        run_proteinmpnn(request, tmp_path / "weights", tmp_path / "work", source=tmp_path / "source")
    assert error.value.code == "EXECUTION_FAILED"


def test_failed_boltz_attempt_can_preserve_outputs_without_reading_pickle(tmp_path):
    (tmp_path / "output/intermediate_designs").mkdir(parents=True)
    raw = b"original-npz-bytes-not-loaded"
    (tmp_path / "output/intermediate_designs/candidate.npz").write_bytes(raw)
    (tmp_path / "output/private.env").write_bytes(b"not-scientific-output")
    (tmp_path / "output/link.cif").symlink_to(tmp_path / "output/private.env")
    artifacts = collect_boltz_artifacts(tmp_path)
    assert artifacts == [{"name": "intermediate_designs--candidate.npz", "kind": "log", "raw": raw}]


@pytest.mark.parametrize("old,new", [
    (b"backbone,", b"other,"), (b"seed=1", b"seed=2"),
    (b"score=1.1", b"score=word"), (b"fixed_chains=[]", b"fixed_chains=oops"),
    (b"designed_chains=['A', 'B']", b"designed_chains=['B']"),
    (b"AC/CA", b"AC/AC"), (b"T=0.1", b"temperature=0.1"),
    (b"sample=1", b"sample=word"), (b"AA/CC", b"AACC"),
    (b"score=1.1", b"score=1.1, score=1.1"),
    (b"fixed_chains=[]", b"fixed_chains=["),
    (b"fixed_chains=[]", b"fixed_chains=]"),
    (b"score=1.1", b"invalid-field"),
    (b"backbone", b"\xff"),
])
def test_proteinmpnn_parser_rejects_forged_headers_and_sequence_layout(old, new):
    fasta, scores = proteinmpnn_bytes()
    with pytest.raises(DesignError) as error:
        parse_proteinmpnn_outputs(fasta.replace(old, new, 1), scores, proteinmpnn_request())
    assert error.value.code == "OUTPUT_INVALID"


@pytest.mark.parametrize("plddt,ptm,pae", [(None, .5, None), ([75., 80.], .5, None),
                                         ([.5, .6], 2., None), ([.5, .6], .5, [[-1., 0.], [0., 0.]])])
def test_fold_result_rejects_invalid_tensor_outputs(plddt, ptm, pae):
    torch = pytest.importorskip("torch")
    result = SimpleNamespace(plddt=None if plddt is None else torch.tensor(plddt), ptm=ptm, iptm=None,
                             pae=None if pae is None else torch.tensor(pae))
    with pytest.raises(DesignError) as error:
        fold_result([result], {"B": "AC"})
    assert error.value.code == "OUTPUT_INVALID"


def test_fold_result_rejects_multiple_samples():
    with pytest.raises(DesignError, match="one folding sample"):
        fold_result([None, None], {"B": "AC"})


@pytest.mark.parametrize("mode,bundled", [("binder", True), ("complex", True), ("binder", False)])
def test_folding_entrypoint_uses_staged_checkpoint_and_validates_real_outputs(tmp_path, monkeypatch, mode, bundled):
    import sys
    from runner.design_workers import run_fold
    from test_design_workflow import coordinates
    torch = pytest.importorskip("torch")
    calls = []
    sequences = {"B": "AC"} if mode == "binder" else {"A": "CA", "B": "AC"}

    class Model:
        config = SimpleNamespace(esmc_config={} if bundled else None)

        def eval(self):
            return self

    def load(path, **kwargs):
        calls.append((path, kwargs))
        return Model()

    class Builder:
        def fold(self, model, inputs, **kwargs):
            assert {item.id: item.sequence for item in inputs.sequences} == sequences
            assert kwargs == {"seed": 7, "num_loops": 10, "num_sampling_steps": 68, "num_diffusion_samples": 1}
            return [SimpleNamespace(plddt=torch.full((sum(map(len, sequences.values())),), .75),
                ptm=.6, iptm=.4, pae=None,
                complex=SimpleNamespace(to_mmcif=lambda: coordinates(sequences).decode()))]

    monkeypatch.setitem(sys.modules, "esm.models.esmfold2", SimpleNamespace(
        EsmFold2Model=SimpleNamespace(from_pretrained=load), ESMFold2InputBuilder=Builder,
        ProteinInput=SimpleNamespace, StructurePredictionInput=SimpleNamespace,
        load_ccd=lambda path: calls.append(path)))
    request = {"mode": mode, "sequence": "AC", "target_sequence": "CA", "seed": 7}
    if not bundled:
        with pytest.raises(DesignError) as error:
            run_fold(request, tmp_path)
        assert error.value.code == "DEPENDENCY_MISMATCH"
    else:
        result = run_fold(request, tmp_path)
        assert result["metrics"]["mean_plddt"] == .75
        assert result["metrics"]["iptm"] == (.4 if mode == "complex" else None)
        assert result["artifacts"][0]["raw"] == coordinates(sequences)
    assert calls == [tmp_path / "esmfold2", (str(tmp_path / "esmfold2"),
                    {"device": "cuda", "local_files_only": True, "load_esmc": False})]


def test_esmc_entrypoint_scores_staged_model_over_full_vocabulary(tmp_path, monkeypatch):
    import sys
    from runner.design_workers import run_esmc
    torch = pytest.importorskip("torch")
    loads = []

    class Model:
        device = "cpu"

        def eval(self):
            return self

        def __call__(self, input_ids, attention_mask):
            return SimpleNamespace(logits=torch.zeros((*input_ids.shape, 6)))

    class Tokenizer:
        mask_token_id = 2

        def __call__(self, sequence, **kwargs):
            return {"input_ids": torch.tensor([[0, 3, 4, 1]])}

    def load(path, **kwargs):
        loads.append((path, kwargs))
        return Model()

    monkeypatch.setitem(sys.modules, "esm.models.esmc", SimpleNamespace(
        EsmcForMaskedLM=SimpleNamespace(from_pretrained=load), EsmcTokenizer=Tokenizer))
    result = run_esmc({"sequence": "AC"}, tmp_path)
    assert loads == [(str(tmp_path / "esmc"), {"device": "cuda", "local_files_only": True})]
    assert result["metrics"]["pseudo_perplexity"] == pytest.approx(6)
    assert result["metrics"]["per_residue_log_probability"] == pytest.approx([-math.log(6)] * 2)
    assert result["artifacts"] == []


@pytest.mark.parametrize("outcome", ["valid", "wrong_target", "missing", "exception"])
def test_boltz_entrypoint_preserves_inputs_selects_outputs_and_restores_argv(tmp_path, monkeypatch, outcome):
    import sys
    import yaml
    from runner.design_workers import run_boltzgen
    from test_design_workflow import coordinates
    pytest.importorskip("torch")
    original = sys.argv
    observed = []
    request = {"seed": 7, "target_cif": coordinates({"A": "AC"}).decode(), "target_sequence": "AC",
               "binding_residues": [1], "min_length": 2, "max_length": 3, "count": 1}

    def main():
        observed.extend(sys.argv)
        if outcome == "exception":
            raise RuntimeError("upstream failure")
        if outcome == "missing":
            return
        output = tmp_path / "output/final_ranked_designs/final_1_designs"
        output.mkdir(parents=True)
        (output / "rank1_candidate.cif").write_bytes(coordinates(
            {"A": "CA" if outcome == "wrong_target" else "AC", "B": "AA"}))
        (tmp_path / "output/intermediate_designs").mkdir()
        (tmp_path / "output/intermediate_designs/candidate.cif").write_bytes(coordinates({"B": "AA"}))

    cli = SimpleNamespace(ARTIFACTS={}, main=main)
    monkeypatch.setitem(sys.modules, "boltzgen.cli", SimpleNamespace(boltzgen=cli))
    if outcome == "exception":
        with pytest.raises(RuntimeError, match="upstream failure"):
            run_boltzgen(request, tmp_path / "cache", tmp_path)
    elif outcome != "valid":
        with pytest.raises(DesignError) as error:
            run_boltzgen(request, tmp_path / "cache", tmp_path)
        assert error.value.code == "OUTPUT_INVALID"
    else:
        result = run_boltzgen(request, tmp_path / "cache", tmp_path)
        assert result["candidates"] == [{"source": "rank1_candidate", "sequence": "AA", "rank": 1}]
        assert result["metrics"] == {"requested": 1, "generated": 1, "retained": 1,
                                     "seed": 7, "filter_random_state": 7, "execution_mode": "in-process"}
        assert any(a["kind"] == "coordinates" for a in result["artifacts"])
    assert sys.argv is original
    assert observed[observed.index("--num_designs") + 1] == "1"
    assert "--no_subprocess" in observed
    spec = yaml.safe_load((tmp_path / "design.yaml").read_text())
    assert spec["entities"][0]["file"]["binding_types"] == [{"chain": {"id": "A", "binding": "1"}}]
    assert spec["entities"][1]["protein"]["sequence"] == "2..3"
    assert (tmp_path / "target.cif").read_text() == request["target_cif"]
    assert cli.ARTIFACTS["design-diverse"][0] == str(tmp_path / "cache/boltzgen/boltzgen1_diverse.ckpt")
