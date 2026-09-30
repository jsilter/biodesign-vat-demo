"""Structural rejection and numeric definition tests without GPU inference."""

import math
import pytest
from pydantic import ValidationError
from runner.design_inputs import (DesignError, DesignSettings, ProteinMPNNRequest, ProteinMPNNSettings,
                                  full_vocabulary_log_probability, masked_metrics,
                                  prepare_backbone, prepare_target, sequence_bytes)
from runner.design_store import DesignStore
from uuid import uuid4


def pdb(residue="ALA", alt=" ", x="   1.000"):
    return f"ATOM      1  CA {alt}{residue} A   1    {x}   2.000   3.000  1.00 20.00           C  \nEND\n".encode()


def backbone(chains=("target", "binder")):
    import gemmi
    structure, model = gemmi.Structure(), gemmi.Model("1")
    for chain_index, name in enumerate(chains):
        chain = gemmi.Chain(name)
        for residue_index, residue_name in enumerate(("ALA", "CYS"), 1):
            residue = gemmi.Residue()
            residue.name, residue.het_flag = residue_name, "A"
            residue.seqid = gemmi.SeqId(residue_index * 10, " ")
            for atom_index, atom_name in enumerate(("N", "CA", "C", "O")):
                atom = gemmi.Atom()
                atom.name = atom_name
                atom.element = gemmi.Element("N" if atom_name == "N" else "O" if atom_name == "O" else "C")
                atom.pos = gemmi.Position(chain_index * 20 + residue_index * 4 + atom_index, atom_index, 0)
                residue.add_atom(atom)
            chain.add_residue(residue)
        model.add_chain(chain)
    structure.add_model(model)
    structure.setup_entities()
    return structure.make_mmcif_document().as_string().encode()


def test_general_target_is_not_gfp():
    target = prepare_target(pdb(), "pdb", "A")
    assert target["sequence"] == "A"
    assert target["mapping"][0]["original_residue"] == "1"
    assert prepare_target(target["prepared_cif"].encode(), "cif", "A")["sequence"] == "A"


@pytest.mark.parametrize("raw,format,chain,code", [
    (pdb(), "pdb", "Z", "INVALID_TARGET"),
    (pdb("MSE"), "pdb", "A", "UNSUPPORTED_CHEMISTRY"),
    (pdb(alt="A"), "pdb", "A", "INVALID_TARGET"),
    (b"not cif", "cif", "A", "INVALID_TARGET"),
    (b"", "pdb", "A", "INVALID_TARGET"),
    (pdb(), "yaml", "A", "INVALID_TARGET"),
])
def test_target_rejection(raw, format, chain, code):
    with pytest.raises(DesignError) as error:
        prepare_target(raw, format, chain)
    assert error.value.code == code


@pytest.mark.parametrize("value", ["", "AcD", "ACD\n", ">name\nACD", "AX", "A C", "Aβ"])
def test_sequence_must_be_canonical(value):
    with pytest.raises(DesignError):
        sequence_bytes(value)


@pytest.mark.parametrize("override,message", [({"name": "../outside"}, "unsafe output filename"),
    ({"sha256": "0" * 64}, "output digest mismatch"), ({"bytes_len": 99}, "output length mismatch")])
def test_remote_artifact_rejects_unsafe_names_and_false_byte_commitments(override, message):
    import hashlib
    from runner.design_inputs import RemoteArtifact
    artifact = {"name": "result.bin", "kind": "log", "raw": b"original bytes",
                "sha256": hashlib.sha256(b"original bytes").hexdigest(), "bytes_len": 14}
    assert RemoteArtifact.model_validate(artifact).raw == b"original bytes"
    with pytest.raises(ValidationError, match=message):
        RemoteArtifact.model_validate({**artifact, **override})


def test_masked_metric_definition_uses_length_and_natural_log():
    result = masked_metrics([math.log(.5), math.log(.25)])
    assert result["length"] == 2
    assert result["mean_masked_log_probability"] == pytest.approx(math.log(.125)/2)
    assert result["pseudo_perplexity"] == pytest.approx(math.sqrt(8))


def test_log_probability_uses_all_output_tokens_and_stable_log_softmax():
    assert full_vocabulary_log_probability([1000., 1000., 1000., 1000.], 1) == pytest.approx(math.log(.25))
    assert full_vocabulary_log_probability([math.log(2), math.log(3), math.log(5)], 1) == pytest.approx(math.log(.3))
    with pytest.raises(DesignError):
        full_vocabulary_log_probability([1., float("nan")], 0)


@pytest.mark.parametrize("values", [[], [float("nan")], [float("inf")], [.1], [-1000], [False], ["-1"]])
def test_invalid_probabilities_are_not_missing_score_sentinels(values):
    with pytest.raises(DesignError):
        masked_metrics(values)


@pytest.mark.parametrize("settings", [{"count": True}, {"count": 0}, {"min_length": 100, "max_length": 60},
                                     {"seed": -1}, {"binding_residues": [1, 1]}, {"command": "echo"}])
def test_generation_settings_are_strict(settings):
    with pytest.raises(ValidationError):
        DesignSettings(**settings)


def test_multichain_backbone_has_stable_pdb_chain_and_residue_mapping():
    result = prepare_backbone(backbone(), "cif")
    assert result["sequences"] == {"A": "AC", "B": "AC"}
    assert result["chain_map"] == [{"original_chain": "target", "prepared_chain": "A"},
                                   {"original_chain": "binder", "prepared_chain": "B"}]
    assert result["mapping"][2] == {"prepared_chain": "B", "prepared_index": 1,
                                    "original_chain": "binder", "original_residue": "10",
                                    "residue_name": "ALA"}
    round_trip = prepare_backbone(result["prepared_pdb"].encode(), "pdb")
    assert round_trip["sequences"] == result["sequences"]
    assert round_trip["chain_map"] == [{"original_chain": "A", "prepared_chain": "A"},
                                       {"original_chain": "B", "prepared_chain": "B"}]


def test_backbone_requires_complete_unique_finite_ordinary_backbone_atoms():
    import gemmi
    document = gemmi.cif.read_string(backbone().decode())
    structure = gemmi.make_structure_from_block(document.sole_block())
    structure[0][0][0].remove_atom("O", " ")
    with pytest.raises(DesignError, match="exactly one N, CA, C and O"):
        prepare_backbone(structure.make_mmcif_document().as_string().encode(), "cif")
    structure = gemmi.make_structure_from_block(document.sole_block())
    structure[0][0][0].name = "MSE"
    with pytest.raises(DesignError) as error:
        prepare_backbone(structure.make_mmcif_document().as_string().encode(), "cif")
    assert error.value.code == "UNSUPPORTED_CHEMISTRY"


@pytest.mark.parametrize("settings", [
    {"design_chains": ["A"], "seed": 0},
    {"design_chains": ["A"], "sampling_temperature": .0},
    {"design_chains": ["A"], "count": 101},
    {"design_chains": ["AA"]},
    {"design_chains": ["A", "A"]},
    {"design_chains": ["A"], "model": "v_48_030"},
    {"design_chains": ["A"], "path": "/private"},
])
def test_proteinmpnn_settings_reject_nondeterministic_or_unsafe_values(settings):
    with pytest.raises(ValidationError):
        ProteinMPNNSettings(**settings)


@pytest.mark.parametrize("mutation", ["wrong_source_chain", "wrong_residue", "duplicate_source_chain"])
def test_proteinmpnn_request_rejects_forged_provenance_mapping(mutation):
    prepared = prepare_backbone(backbone(), "cif")
    request = {"backbone_cif": prepared["prepared_cif"], "sequences": prepared["sequences"],
               "chain_map": prepared["chain_map"], "mapping": prepared["mapping"],
               "design_chains": ["B"], "count": 1, "sampling_temperature": .1,
               "seed": 1, "model": "v_48_020"}
    if mutation == "wrong_source_chain":
        request["mapping"][0]["original_chain"] = "binder"
    elif mutation == "wrong_residue":
        request["mapping"][0]["residue_name"] = "CYS"
    else:
        request["chain_map"][1]["original_chain"] = "target"
    with pytest.raises(ValidationError):
        ProteinMPNNRequest.model_validate(request)


def test_store_restart_deduplication_and_integrity(tmp_path):
    id = str(uuid4())
    store = DesignStore(tmp_path)
    assert store.create(id, {"prompt": "original"}, {"status": "running"})
    art = store.artifact(id, "binder.txt", "aa_sequence", b"ACD")
    store.save(id, {"status": "completed"})
    store.close()
    store = DesignStore(tmp_path)
    try:
        assert not store.create(id, {"prompt": "original"}, {})
        assert store.get(id)[1]["status"] == "completed"
        assert store.read(id, art["sha256"], art["name"]) == b"ACD"
        with pytest.raises(DesignError, match="different request"):
            store.create(id, {"prompt": "changed"}, {})
        with pytest.raises(DesignError, match="unavailable"):
            store.read(str(uuid4()), art["sha256"], art["name"])
        (tmp_path/id/"blobs"/art["sha256"]).write_bytes(b"ACE")
        with pytest.raises(DesignError, match="integrity"):
            store.read(id, art["sha256"], art["name"])
    finally:
        store.close()


def test_second_owner_and_unsafe_artifact_names_are_rejected(tmp_path):
    store = DesignStore(tmp_path)
    try:
        with pytest.raises(DesignError, match="Another backend"):
            DesignStore(tmp_path)
        for name in ["../secret", "..", "/absolute", "folder\\file", ""]:
            with pytest.raises(DesignError, match="Unsafe"):
                store.artifact(str(uuid4()), name, "log", b"x")
    finally:
        store.close()


def test_private_keys_survive_restart_and_never_enter_download_index(tmp_path):
    store = DesignStore(tmp_path)
    trust = store.keys().trust_bundle()
    assert all(path.stat().st_mode & 0o077 == 0 for path in (tmp_path / "keys").iterdir())
    assert (tmp_path / "state.sqlite").stat().st_mode & 0o077 == 0
    store.close()
    store = DesignStore(tmp_path)
    try:
        assert store.keys().trust_bundle() == trust
        assert not store.artifacts(str(uuid4()))
    finally:
        store.close()


def test_artifact_directory_symlink_is_rejected(tmp_path):
    store = DesignStore(tmp_path)
    id = str(uuid4())
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / id).symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(DesignError, match="directory cannot be a symlink"):
            store.artifact(id, "output.cif", "coordinates", b"data")
        assert not list(outside.iterdir())
    finally:
        store.close()
