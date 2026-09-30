"""Pinned kit adapters construct server-owned inputs and retain bounded real-format outputs."""

from pathlib import Path

import pytest

from runner.design_inputs import DesignError
from runner.kit_workers import (KIT_TOOL_IDS, collect_kit_outputs, prepare_kit_command,
                                validate_kit_outputs)
from runner.tool_catalog import tool_spec
from runner.tool_contracts import tool_request


def prediction_payload(**changes):
    value = {"sequences": {"A": "ACDE", "B": "FGHI"}, "msa_mode": "none",
             "template_policy": "none", "count": 2, "seed": 7}
    value.update(changes)
    return value


def cif() -> str:
    import gemmi
    structure, model, chain, residue = gemmi.Structure(), gemmi.Model("1"), gemmi.Chain("A"), gemmi.Residue()
    residue.name, residue.het_flag, residue.seqid = "ALA", "A", gemmi.SeqId(1, " ")
    for index, name in enumerate(("N", "CA", "C", "O")):
        atom = gemmi.Atom(); atom.name = name; atom.pos = gemmi.Position(index, 0, 0)
        atom.element = gemmi.Element("N" if name == "N" else "O" if name == "O" else "C")
        residue.add_atom(atom)
    chain.add_residue(residue); model.add_chain(chain); structure.add_model(model); structure.setup_entities()
    return structure.make_mmcif_document().as_string()


@pytest.mark.parametrize("tool,words", [
    ("esmfold2-full", ("--variant", "full_nomsa", "--num_diffusion_samples", "2")),
    ("protenix-v2", ("--model_name", "protenix-v2", "--sample", "2", "--use_msa", "false")),
    ("colabfold-afmultimer-v3", ("--model-type", "alphafold2_multimer_v3", "--msa-mode", "single_sequence")),
    ("af3-openfold3", ("--num_diffusion_samples", "2")),
    ("chai1", ("--num-diffn-samples", "2", "--no-use-msa-server", "--no-use-templates-server")),
    ("boltz2", ("--diffusion_samples", "2", "--output_format", "mmcif")),
    ("openfold3", ("--runner-yaml", "--use-msa-server", "false", "--use-templates", "false")),
    ("rosettafold3", ("--seeds", "7,8")),
    ("opendde", ("--use_msa", "false", "--use_template", "false")),
])
def test_prediction_kit_commands_are_fixed_and_use_private_work_paths(tmp_path, tool, words):
    request = tool_request(tool, prediction_payload())
    command, environment, output = prepare_kit_command(tool, request["payload"], tmp_path,
                                                        kit_root=Path("/kit"))
    assert command[:3] == ["bash", f"/kit/{tool_spec(tool).kit}/run.sh", "pred"]
    for word in words:
        assert word in command
    assert output == tmp_path / "outputs"
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert not any("../" in argument or ";" in argument for argument in command)


@pytest.mark.parametrize("tool,model", [("caliby", "caliby"),
                                         ("soluble-caliby", "soluble_caliby_v1")])
def test_caliby_variants_keep_distinct_tool_and_checkpoint_identity(tmp_path, tool, model):
    payload = {"backbone_cif": cif(), "sequences": {"A": "A"},
               "design_chains": ["A"], "count": 3, "seed": 9, "sampling_temperature": .2}
    request = tool_request(tool, payload)
    command, environment, _ = prepare_kit_command(tool, request["payload"], tmp_path)
    assert command[2:8] == ["design", "--config", "h100", "--mode", "off", "--variant"]
    assert command[command.index("--model_name") + 1] == model
    assert command[command.index("--num_seqs_per_pdb") + 1] == "3"
    assert environment["MODEL_PARAMS_DIR"] == f"/weights/{tool}"


def test_generation_and_caliby_reject_declared_sequences_that_differ_from_coordinates(tmp_path):
    generation = tool_request("rfdiffusion", {"target_cif": cif(), "target_chains": ["A"],
        "target_sequences": {"A": "C"}, "count": 1, "seed": 3, "min_length": 60,
        "max_length": 80, "hotspots": []})
    with pytest.raises(DesignError, match="declared sequence"):
        prepare_kit_command("rfdiffusion", generation["payload"], tmp_path)

    sequence = tool_request("caliby", {"backbone_cif": cif(), "sequences": {"A": "C"},
        "design_chains": ["A"], "count": 1, "seed": 3, "sampling_temperature": .2})
    other = tmp_path / "caliby"; other.mkdir()
    with pytest.raises(DesignError, match="declared sequences"):
        prepare_kit_command("caliby", sequence["payload"], other)


def test_af_unmasked_converts_only_valid_supplied_templates(tmp_path):
    request = tool_request("af-unmasked", prediction_payload(
        sequences={"A": "A"}, template_policy="provided", templates_cif=[cif()], count=1, seed=0))
    command, environment, _ = prepare_kit_command("af-unmasked", request["payload"], tmp_path)
    assert command[2:8] == ["pred", "--config", "h100", "--mode", "off", "--pdbdir"]
    assert (tmp_path / "templates" / "template-1.pdb").read_text().startswith("HEADER")
    assert environment["AF2_PARAMS"] == "/weights/af2"
    mismatched = tool_request("af-unmasked", prediction_payload(
        sequences={"A": "C"}, template_policy="provided", templates_cif=[cif()], count=1, seed=0))
    other = tmp_path / "mismatched"; other.mkdir()
    with pytest.raises(DesignError, match="submitted sequences"):
        prepare_kit_command("af-unmasked", mismatched["payload"], other)


@pytest.mark.parametrize("tool,words", [
    ("pxdesign", ("--N_sample", "2", "--seeds", "11")),
    ("rfdiffusion", ("--det", "1", "inference.design_startnum=11", "inference.num_designs=2", "ppi.hotspot_res=[A1]")),
    ("rfdiffusion3", ("n_batches=2", "diffusion_batch_size=1", "seed=11")),
    ("proteina-complexa", ("++generation.reward_model=null",
                           "++generation.dataloader.dataset.nres.nsamples=2")),
    ("genie3", ("--seed", "11", "--batch_size", "2")),
    ("mosaic", ("--binder-length", "60", "--epitope", "1")),
])
def test_generation_kit_commands_bind_validated_targets_and_counts(tmp_path, tool, words):
    fixed = tool in {"pxdesign", "mosaic"}
    payload = {"target_cif": cif(), "target_chains": ["A"], "target_sequences": {"A": "A"},
               "count": 1 if tool == "mosaic" else 2, "seed": 11, "min_length": 60,
               "max_length": 60 if fixed else 80, "hotspots": [1] if tool != "rfdiffusion3" else []}
    request = tool_request(tool, payload)
    command, environment, output = prepare_kit_command(tool, request["payload"], tmp_path)
    assert command[2] == "design" and output == tmp_path / "outputs"
    for word in words:
        assert word in command
    assert environment["VAT_KIT_REVISION"]


def test_output_capture_rejects_symlinks_unknown_files_and_missing_science(tmp_path):
    output = tmp_path / "output"; output.mkdir()
    (output / "ranked.cif").write_bytes(b"data_prediction\n#\n")
    (output / "scores.json").write_bytes(b'{"confidence":0.5}')
    artifacts = collect_kit_outputs(output)
    assert [(artifact["name"], artifact["kind"]) for artifact in artifacts] == [
        ("ranked.cif", "coordinates"), ("scores.json", "metric")]

    (output / "bad.bin").write_bytes(b"opaque")
    with pytest.raises(DesignError, match="unexpected"):
        collect_kit_outputs(output)
    (output / "bad.bin").unlink()
    (output / "link.cif").symlink_to(output / "ranked.cif")
    with pytest.raises(DesignError, match="symbolic"):
        collect_kit_outputs(output)


def test_output_validation_binds_prediction_and_generation_coordinates_to_inputs():
    from test_design_workflow import coordinates

    prediction = [{"name": "rank-1.cif", "kind": "coordinates",
                   "raw": coordinates({"X": "AC", "Y": "CC"})}]
    validate_kit_outputs("boltz2", prediction_payload(
        sequences={"A": "AC", "B": "CC"}, count=1), prediction)
    with pytest.raises(DesignError, match="submitted protein sequences"):
        validate_kit_outputs("boltz2", prediction_payload(
            sequences={"A": "AC", "B": "FA"}, count=1), prediction)

    generation = [{"name": "design-1.cif", "kind": "coordinates",
                   "raw": coordinates({"A": "AC", "B": "GG"})}]
    payload = {"target_chains": ["A"], "target_sequences": {"A": "AC"}, "count": 1}
    validate_kit_outputs("rfdiffusion", payload, generation)
    with pytest.raises(DesignError, match="target and designed"):
        validate_kit_outputs("rfdiffusion", payload, [{**generation[0],
            "raw": coordinates({"A": "AC"})}])


def test_caliby_output_validation_preserves_each_requested_sequence_sample():
    artifacts = [{"name": "designs.fasta", "kind": "fasta",
                  "raw": b">sample-1\nAC\n>sample-2\nAC\n"}]
    validate_kit_outputs("caliby", {"count": 2}, artifacts)
    with pytest.raises(DesignError, match="fewer"):
        validate_kit_outputs("caliby", {"count": 3}, artifacts)


def test_all_declared_sequence_and_prediction_kits_have_command_builders(tmp_path):
    assert KIT_TOOL_IDS == {"pxdesign", "rfdiffusion3", "genie3", "rfdiffusion", "proteina-complexa", "mosaic",
                            "soluble-caliby", "caliby", "esmfold2-full", "protenix-v2",
                            "colabfold-afmultimer-v3", "af3-openfold3", "chai1", "boltz2",
                            "af-unmasked", "openfold3", "rosettafold3", "opendde"}
