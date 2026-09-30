"""Narrow input builders and output capture for Anthropic's pinned stock kit routes."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

from .design_inputs import DesignError
from .design_store import json_bytes
from .suite_workers import MAX_UTILITY_OUTPUT, _run
from .tool_catalog import ANTHROPIC_KIT_REVISION, tool_spec

KIT_TOOL_IDS = frozenset({
    "pxdesign", "rfdiffusion3", "genie3", "rfdiffusion", "proteina-complexa", "mosaic",
    "soluble-caliby", "caliby", "esmfold2-full", "protenix-v2",
    "colabfold-afmultimer-v3", "af3-openfold3", "chai1", "boltz2",
    "af-unmasked", "openfold3", "rosettafold3", "opendde",
})
ALLOWED_OUTPUT_SUFFIXES = frozenset({
    ".cif", ".pdb", ".json", ".csv", ".npz", ".a3m", ".fasta", ".fa", ".yaml", ".yml", ".txt", ".log",
})
MAX_KIT_ARTIFACTS = 256
MAX_KIT_OUTPUT_BYTES = 64 * 1024 * 1024


def _write_json(path: Path, value: object) -> Path:
    path.write_bytes(json_bytes(value))
    return path


def _fasta(sequences: dict[str, str], *, chai: bool = False) -> str:
    return "".join(f">{'protein|name=' if chai else ''}{chain}\n{sequence}\n"
                   for chain, sequence in sequences.items())


def _no_unimplemented_features(payload: dict, *, templates: bool = False, msa: bool = False) -> None:
    if payload.get("template_policy") != ("provided" if templates else "none"):
        raise DesignError("INVALID_REQUEST", "This pinned adapter does not accept that template policy.")
    if payload.get("msa_mode") != ("provided" if msa else "none"):
        raise DesignError("INVALID_REQUEST", "This pinned adapter does not accept that MSA mode.")


def _cif_sequences(raw: str) -> tuple[object, dict[str, str]]:
    """Parse one prepared protein model and derive the exact modeled chain sequences."""
    try:
        import gemmi
        structure = gemmi.make_structure_from_block(gemmi.cif.read_string(raw).sole_block())
    except (ImportError, ValueError, RuntimeError) as exc:
        raise DesignError("INVALID_REQUEST", "Kit structure input must be valid prepared mmCIF.") from exc
    if len(structure) != 1:
        raise DesignError("INVALID_REQUEST", "Kit structure input must contain exactly one model.")
    sequences = {}
    for chain in structure[0]:
        residues = []
        for residue in chain:
            info = gemmi.find_tabulated_residue(residue.name)
            if (not info.is_amino_acid() or info.one_letter_code not in "ACDEFGHIKLMNPQRSTVWY"
                    or not any(atom.name == "CA" for atom in residue)
                    or any(not all(math.isfinite(value) for value in (atom.pos.x, atom.pos.y, atom.pos.z))
                           for atom in residue)):
                raise DesignError("INVALID_REQUEST", "Kit structure input contains unsupported protein chemistry.")
            residues.append(info.one_letter_code)
        if residues:
            if chain.name in sequences:
                raise DesignError("INVALID_REQUEST", "Kit structure input repeats a chain identity.")
            sequences[chain.name] = "".join(residues)
    if not sequences:
        raise DesignError("INVALID_REQUEST", "Kit structure input contains no modeled protein chain.")
    return structure, sequences


def _target_pdb(payload: dict, work: Path) -> tuple[Path, str, int]:
    chain = payload["target_chains"][0]
    if len(chain) != 1:
        raise DesignError("INVALID_REQUEST", "Generation kits require a one-character prepared chain identity.")
    structure, sequences = _cif_sequences(payload["target_cif"])
    if sequences != payload["target_sequences"] or list(sequences) != [chain]:
        raise DesignError("INVALID_REQUEST", "Generation target bytes and declared sequence identity differ.")
    path = work / "target.pdb"
    structure.write_pdb(str(path))
    return path, chain, len(payload["target_sequences"][chain])


def prepare_kit_command(tool: str, payload: dict, work: Path,
                        *, kit_root: Path = Path("/kit")) -> tuple[list[str], dict[str, str], Path]:
    """Write server-owned inputs and construct one fixed upstream stock command."""
    spec = tool_spec(tool)
    if tool not in KIT_TOOL_IDS or not spec.kit or spec.revision != ANTHROPIC_KIT_REVISION:
        raise DesignError("TOOL_UNAVAILABLE", "Pinned Anthropic kit adapter is unavailable.")
    run = kit_root / spec.kit / "run.sh"
    out = work / "outputs"
    out.mkdir()
    environment = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                   "VAT_KIT_REVISION": ANTHROPIC_KIT_REVISION}
    base = ["bash", str(run)]

    if tool in {"caliby", "soluble-caliby"}:
        inputs = [work / "backbone.cif"] + [work / f"conformer-{index}.cif"
                                             for index in range(1, len(payload["conformers_cif"]) + 1)]
        structures = [payload["backbone_cif"], *payload["conformers_cif"]]
        for raw in structures:
            _, sequences = _cif_sequences(raw)
            if sequences != payload["sequences"]:
                raise DesignError("INVALID_REQUEST", "Caliby backbone bytes and declared sequences differ.")
        for path, raw in zip(inputs, structures):
            path.write_text(raw, encoding="utf-8")
        model = "caliby" if tool == "caliby" else "soluble_caliby_v1"
        command = base + ["design", "--config", "h100", "--mode", "off", "--variant", "single",
                          "--input", *map(str, inputs), "--out_dir", str(out), "--seed", str(payload["seed"]),
                          "--model_name", model, "--num_seqs_per_pdb", str(payload["count"]),
                          "--temperature", str(payload["sampling_temperature"]), "--num_workers", "1"]
        environment["MODEL_PARAMS_DIR"] = f"/weights/{tool}"
        return command, environment, out

    if tool in {"pxdesign", "rfdiffusion3", "genie3", "rfdiffusion", "proteina-complexa", "mosaic"}:
        target, chain, target_length = _target_pdb(payload, work)
        hotspots = [f"{chain}{index}" for index in payload["hotspots"]]
        if tool == "pxdesign":
            request = {"target": {"file": str(target), "chains": {chain: {
                "crop": [f"1-{target_length}"], "hotspots": payload["hotspots"]}}},
                "binder_length": payload["min_length"]}
            input_path = _write_json(work / "input.yaml", request)
            environment.update(PXDESIGN_CKPT_DIR="/weights/pxdesign/checkpoint",
                               PROTENIX_DATA_ROOT_DIR="/weights/pxdesign/ccd_cache")
            return base + ["design", "--config", "h100", "--mode", "off", "-i", str(input_path),
                           "-o", str(out), "--seeds", str(payload["seed"]),
                           "--N_sample", str(payload["count"])], environment, out
        if tool == "rfdiffusion":
            environment["WEIGHTS"] = "/weights/rfdiffusion"
            contig = f"[{chain}1-{target_length}/0 {payload['min_length']}-{payload['max_length']}]"
            command = base + ["design", "--config", "h100", "--mode", "off",
                              "--det", "1", f"inference.design_startnum={payload['seed']}",
                              f"inference.input_pdb={target}", f"contigmap.contigs={contig}",
                              f"inference.num_designs={payload['count']}",
                              f"inference.output_prefix={out / 'design'}"]
            if hotspots:
                command.append(f"ppi.hotspot_res=[{','.join(hotspots)}]")
            return command, environment, out
        if tool == "rfdiffusion3":
            if hotspots:
                raise DesignError("INVALID_REQUEST", "RFdiffusion3 requires atom-specific hotspots not present in this contract.")
            input_path = _write_json(work / "input.json", {"request": {"dialect": 2, "input": str(target),
                "contig": f"{payload['min_length']}-{payload['max_length']},/0,{chain}1-{target_length}",
                "is_non_loopy": True}})
            environment["RFD3_CKPT"] = "/weights/rfdiffusion3/rfd3_latest.ckpt"
            return base + ["design", "--config", "h100", "--mode", "off", f"inputs={input_path}",
                           f"out_dir={out}", f"n_batches={payload['count']}",
                           "diffusion_batch_size=1", f"seed={payload['seed']}"], environment, out
        if tool == "proteina-complexa":
            request = {"request": {"source": "custom", "target_filename": "target",
                "target_path": str(target), "target_input": f"{chain}1-{target_length}",
                "hotspot_residues": hotspots, "binder_length": [payload["min_length"], payload["max_length"]],
                "pdb_id": None}}
            input_path = _write_json(work / "input.json", request)
            environment["CKPT_PATH"] = "/weights/complexa"
            return base + ["design", "--config", "h100", "--mode", "off", "--input", str(input_path),
                           "--out", str(out), "--", "++generation.search.algorithm=single-pass",
                           "++generation.reward_model=null",
                           f"++generation.dataloader.dataset.nres.nsamples={payload['count']}",
                           f"++seed={payload['seed']}"], environment, out
        if tool == "genie3":
            dataset = work / "dataset"; (dataset / "problems").mkdir(parents=True)
            fasta = work / "target.fasta"; fasta.write_text(_fasta({chain: payload["target_sequences"][chain]}), encoding="ascii")
            problem = {"key": "request", "name": "request", "target_pdb_filepath": str(target),
                       "target_fasta_filepath": str(fasta), "target_pdb_filepath_by_chain": [str(target)],
                       "target_fasta_filepath_by_chain": [str(fasta)],
                       "target_chain_and_residues": [f"{chain}1-{target_length}"],
                       "target_interface_residues": {"hotspot": hotspots, "extended": hotspots, "common": hotspots},
                       "binder_min_length": payload["min_length"], "binder_max_length": payload["max_length"],
                       "tag": ["VAT"], "pdb_id": None}
            _write_json(dataset / "problems" / "request.json", problem)
            config = ("experiment:\n  name: vat_request\npaths:\n"
                      f"  rootdir: {out}\n  dataset: {dataset}\ngeneration:\n  dataset:\n"
                      f"    source: target\n    selections: request\n    n_sample: {payload['count']}\n"
                      "  sampler:\n    sampler:\n      direction_scale: 0.0\n")
            input_path = work / "experiment.yaml"; input_path.write_text(config, encoding="utf-8")
            environment["GENIE3_WEIGHTS"] = "/weights/genie3/pretrained/v1"
            return base + ["design", "--config", "h100", "--mode", "off", "--input", str(input_path),
                           "--out_dir", str(out), "--seed", str(payload["seed"]),
                           "--batch_size", str(payload["count"])], environment, out
        if tool == "mosaic":
            fasta = work / "target.fasta"; fasta.write_text(_fasta({chain: payload["target_sequences"][chain]}), encoding="ascii")
            environment["MOSAIC_CACHE_DIR"] = "/weights/mosaic"
            command = base + ["design", "--config", "h100", "--mode", "off", "--out", str(out),
                              "--target-fasta", str(fasta), "--binder-length", str(payload["min_length"]),
                              "--seed", str(payload["seed"]), "--tag", "request"]
            if payload["hotspots"]:
                command += ["--epitope", ",".join(map(str, payload["hotspots"]))]
            return command, environment, out

    sequences, seed, count = payload["sequences"], payload["seed"], payload["count"]
    if tool == "esmfold2-full":
        _no_unimplemented_features(payload, msa=payload["msa_mode"] == "provided")
        msa_path = None
        if payload["msa_mode"] == "provided":
            if len(sequences) != 1:
                raise DesignError("INVALID_REQUEST", "A provided ESMFold2 MSA currently requires one chain.")
            msa_path = work / "query.a3m"
            msa_path.write_text(payload["msa_a3m"], encoding="utf-8")
        document = [{"id": "request", "sequences": [
            {"type": "protein", "id": chain, "sequence": sequence,
             "msa": str(msa_path) if msa_path else None} for chain, sequence in sequences.items()],
            "seeds": [seed], "num_diffusion_samples": count}]
        input_path = _write_json(work / "input.json", document)
        variant = "full_msa" if msa_path else "full_nomsa"
        environment["HF_HOME"] = "/weights/esmfold2-full"
        return base + ["pred", "--config", "h100", "--mode", "off", "--variant", variant,
                       "--input", str(input_path), "--out_dir", str(out), "--seeds", str(seed),
                       "--num_diffusion_samples", str(count)], environment, out

    if tool == "protenix-v2":
        _no_unimplemented_features(payload)
        document = [{"name": "request", "sequences": [
            {"proteinChain": {"sequence": sequence, "count": 1}} for sequence in sequences.values()]}]
        input_path = _write_json(work / "input.json", document)
        environment["PROTENIX_ROOT_DIR"] = "/weights/protenix-v2"
        return base + ["pred", "--config", "h100", "--mode", "off", "--input", str(input_path),
                       "--out_dir", str(out), "--model_name", "protenix-v2", "--seeds", str(seed),
                       "--sample", str(count), "--use_msa", "false"], environment, out

    if tool == "colabfold-afmultimer-v3":
        _no_unimplemented_features(payload)
        input_path = work / "input.fasta"
        input_path.write_text(">request\n" + ":".join(sequences.values()) + "\n", encoding="ascii")
        environment["COLABFOLD_OPT_DATA_DIR"] = "/weights/af2-multimer-v3"
        return base + ["pred", "--config", "h100", "--mode", "off", str(input_path), str(out),
                       "--model-type", "alphafold2_multimer_v3", "--msa-mode", "single_sequence",
                       "--num-models", "1", "--num-seeds", str(count), "--random-seed", str(seed)], environment, out

    if tool == "af3-openfold3":
        _no_unimplemented_features(payload)
        document = {"name": "request", "dialect": "alphafold3", "version": 2, "sequences": [
            {"protein": {"id": chain, "sequence": sequence, "modifications": [], "templates": [],
                         "unpairedMsa": f">query\n{sequence}\n", "pairedMsa": ""}}
            for chain, sequence in sequences.items()], "modelSeeds": [seed]}
        input_path = _write_json(work / "input.json", document)
        environment["AF3_TORCH_PARAMS_DIR"] = "/weights/openfold3-openbind0"
        return base + ["pred", "--config", "h100", "--mode", "off", "--json_path", str(input_path),
                       "--output_dir", str(out), "--num_diffusion_samples", str(count)], environment, out

    if tool == "chai1":
        _no_unimplemented_features(payload)
        input_path = work / "input.fasta"
        input_path.write_text(_fasta(sequences, chai=True), encoding="ascii")
        environment["CHAI_DOWNLOADS_DIR"] = "/weights/chai1"
        return base + ["pred", "--config", "h100", "--mode", "off", "--seed", str(seed),
                       "--input", str(input_path), "--out_dir", str(out), "--num-diffn-samples", str(count),
                       "--no-use-msa-server", "--no-use-templates-server"], environment, out

    if tool == "boltz2":
        _no_unimplemented_features(payload)
        input_path = work / "input.yaml"
        lines = ["version: 1", "sequences:"]
        for chain, sequence in sequences.items():
            lines += ["  - protein:", f"      id: {chain}", f"      sequence: {sequence}", "      msa: empty"]
        input_path.write_text("\n".join(lines) + "\n", encoding="ascii")
        environment["BOLTZ_CACHE"] = "/weights/boltz2"
        return base + ["pred", "--config", "h100", "--mode", "off", "--input", str(input_path),
                       "--out_dir", str(out), "--seed", str(seed), "--diffusion_samples", str(count),
                       "--output_format", "mmcif"], environment, out

    if tool == "openfold3":
        _no_unimplemented_features(payload)
        document = {"queries": {"request": {"chains": [
            {"molecule_type": "protein", "chain_ids": [chain], "sequence": sequence}
            for chain, sequence in sequences.items()]}}}
        input_path = _write_json(work / "input.json", document)
        runner_yaml = work / "runner.yaml"
        runner_yaml.write_text(f"experiment_settings:\n  seeds: [{seed}]\n", encoding="ascii")
        environment["OPENFOLD3_CKPT"] = "/weights/openfold3/of3-p2-155k.pt"
        return base + ["pred", "--config", "h100", "--mode", "off", "--query-json", str(input_path),
                       "--output-dir", str(out), "--runner-yaml", str(runner_yaml),
                       "--num-diffusion-samples", str(count),
                       "--use-msa-server", "false", "--use-templates", "false"], environment, out

    if tool == "rosettafold3":
        _no_unimplemented_features(payload)
        document = [{"name": "request", "components": [
            {"seq": sequence, "chain_id": chain, "chain_type": "polypeptide(L)"}
            for chain, sequence in sequences.items()]}]
        input_path = _write_json(work / "input.json", document)
        environment["ROSETTAFOLD3_OPT_CKPT"] = "/weights/rosettafold3/rf3_foundry_01_24_latest_remapped.ckpt"
        seeds = ",".join(str(seed + index) for index in range(count))
        return base + ["pred", "--config", "h100", "--mode", "off", "--input", str(input_path),
                       "--out_dir", str(out), "--seeds", seeds], environment, out

    if tool == "opendde":
        _no_unimplemented_features(payload)
        document = [{"name": "request", "sequences": [
            {"proteinChain": {"sequence": sequence, "count": 1}} for sequence in sequences.values()]}]
        input_path = _write_json(work / "input.json", document)
        environment["OPENDDE_ROOT_DIR"] = "/weights/opendde"
        return base + ["pred", "--config", "h100", "--mode", "off", "-i", str(input_path), "-o", str(out),
                       "--seeds", str(seed), "--sample", str(count), "--use_msa", "false",
                       "--use_template", "false"], environment, out

    if tool == "af-unmasked":
        _no_unimplemented_features(payload, templates=True)
        template_dir = work / "templates"
        template_dir.mkdir()
        for index, raw in enumerate(payload["templates_cif"], 1):
            structure, observed = _cif_sequences(raw)
            if observed != payload["sequences"]:
                raise DesignError("INVALID_REQUEST", "AF_unmasked template bytes and submitted sequences differ.")
            structure.write_pdb(str(template_dir / f"template-{index}.pdb"))
        environment["AF2_PARAMS"] = "/weights/af2"
        return base + ["pred", "--config", "h100", "--mode", "off", "--pdbdir", str(template_dir),
                       "--out", str(out), "--det", "1"], environment, out

    raise DesignError("TOOL_UNAVAILABLE", "Pinned Anthropic kit adapter is unavailable.")


def collect_kit_outputs(out: Path) -> list[dict]:
    artifacts, total, names = [], 0, set()
    for path in sorted(out.rglob("*")):
        if path.is_symlink():
            raise DesignError("OUTPUT_INVALID", "Kit output contains a symbolic link.")
        if not path.is_file():
            continue
        if path.suffix.lower() not in ALLOWED_OUTPUT_SUFFIXES:
            raise DesignError("OUTPUT_INVALID", "Kit output contains an unexpected file type.")
        raw = path.read_bytes()
        total += len(raw)
        if len(raw) > MAX_UTILITY_OUTPUT or total > MAX_KIT_OUTPUT_BYTES:
            raise DesignError("OUTPUT_INVALID", "Kit output exceeds the retained artifact limit.")
        name = "__".join(path.relative_to(out).parts)
        if not name or name in names or len(name) > 255:
            raise DesignError("OUTPUT_INVALID", "Kit output has an unsafe or duplicate artifact identity.")
        names.add(name)
        suffix = path.suffix.lower()
        kind = ("coordinates" if suffix in {".cif", ".pdb"} else "fasta" if suffix in {".fa", ".fasta"}
                else "msa" if suffix == ".a3m" else "log" if suffix in {".log", ".txt"} else "metric")
        artifacts.append({"name": name, "kind": kind, "raw": raw})
        if len(artifacts) > MAX_KIT_ARTIFACTS:
            raise DesignError("OUTPUT_INVALID", "Kit returned too many artifact files.")
    if not artifacts or not any(artifact["kind"] in {"coordinates", "fasta"} for artifact in artifacts):
        raise DesignError("OUTPUT_INVALID", "Kit returned no retained scientific output.")
    return artifacts


def _coordinate_sequences(raw: bytes, suffix: str) -> list[str]:
    """Parse retained coordinates and reject malformed or non-protein model bytes."""
    try:
        import gemmi
        text = raw.decode("utf-8")
        structure = (gemmi.read_pdb_string(text) if suffix == ".pdb"
                     else gemmi.make_structure_from_block(gemmi.cif.read_string(text).sole_block()))
    except (ImportError, UnicodeError, ValueError, RuntimeError) as exc:
        raise DesignError("OUTPUT_INVALID", "Kit returned unreadable coordinate bytes.") from exc
    if len(structure) != 1:
        raise DesignError("OUTPUT_INVALID", "Kit coordinates must contain exactly one model.")
    sequences = []
    for chain in structure[0]:
        residues = []
        for residue in chain:
            info = gemmi.find_tabulated_residue(residue.name)
            if not info.is_amino_acid() or info.one_letter_code not in "ACDEFGHIKLMNPQRSTVWY":
                raise DesignError("OUTPUT_INVALID", "Kit coordinates contain unsupported protein chemistry.")
            if not any(atom.name == "CA" for atom in residue):
                raise DesignError("OUTPUT_INVALID", "Kit coordinates contain a residue without a C-alpha atom.")
            if any(not all(math.isfinite(value) for value in (atom.pos.x, atom.pos.y, atom.pos.z))
                   for atom in residue):
                raise DesignError("OUTPUT_INVALID", "Kit coordinates contain non-finite values.")
            residues.append(info.one_letter_code)
        if residues:
            sequences.append("".join(residues))
    if not sequences:
        raise DesignError("OUTPUT_INVALID", "Kit coordinates contain no protein chain.")
    return sequences


def _fasta_sequences(raw: bytes) -> list[str]:
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeError as exc:
        raise DesignError("OUTPUT_INVALID", "Kit FASTA output must be ASCII.") from exc
    sequences, current, saw_header = [], [], False
    for line in lines:
        if line.startswith(">"):
            if not line[1:].strip() or (saw_header and not current):
                raise DesignError("OUTPUT_INVALID", "Kit FASTA output is malformed.")
            if current:
                sequences.append("".join(current))
                current = []
            saw_header = True
        elif line.strip():
            if not saw_header:
                raise DesignError("OUTPUT_INVALID", "Kit FASTA output lacks a header.")
            current.append(line.strip())
    if current:
        sequences.append("".join(current))
    if not sequences:
        raise DesignError("OUTPUT_INVALID", "Kit FASTA output contains no sequences.")
    for sequence in sequences:
        from .design_inputs import sequence_bytes
        sequence_bytes(sequence)
    return sequences


def validate_kit_outputs(tool: str, payload: dict, artifacts: list[dict]) -> None:
    """Bind native kit artifacts to the bounded request before VAT capture."""
    coordinate_samples = []
    fasta_samples = []
    for artifact in artifacts:
        suffix = Path(artifact["name"].replace("__", "/")).suffix.lower()
        if artifact["kind"] == "coordinates":
            coordinate_samples.append(_coordinate_sequences(artifact["raw"], suffix))
        elif artifact["kind"] == "fasta":
            fasta_samples.extend(_fasta_sequences(artifact["raw"]))
    expected = payload["count"]
    if tool in {"caliby", "soluble-caliby"}:
        if len(fasta_samples) < expected and len(coordinate_samples) < expected:
            raise DesignError("OUTPUT_INVALID", "Caliby returned fewer sequence-design samples than requested.")
        return
    if len(coordinate_samples) < expected:
        raise DesignError("OUTPUT_INVALID", "Kit returned fewer coordinate samples than requested.")
    if tool_spec(tool).family == "prediction":
        expected_sequences = sorted(payload["sequences"].values())
        if any(sorted(sample) != expected_sequences for sample in coordinate_samples):
            raise DesignError("OUTPUT_INVALID", "Prediction coordinates do not match the submitted protein sequences.")
    elif tool_spec(tool).family == "generation":
        target = payload["target_sequences"][payload["target_chains"][0]]
        if any(target not in sample or len(sample) < 2 or not any(sequence != target for sequence in sample)
               for sample in coordinate_samples):
            raise DesignError("OUTPUT_INVALID", "Generated coordinates do not preserve target and designed chains.")


def run_kit(tool: str, payload: dict, work: Path, *, kit_root: Path = Path("/kit"),
            assets: Path = Path("/weights")) -> dict:
    from .kit_assets import asset_root_name, validate_asset_manifest

    pins = kit_root / tool_spec(tool).kit / "stock" / "PINS.json"
    if pins.is_symlink() or not pins.is_file():
        raise DesignError("DEPENDENCY_MISMATCH", "Pinned kit asset identities are absent from the image.")
    validate_asset_manifest(tool, assets / asset_root_name(tool), pins.read_bytes())
    command, environment, out = prepare_kit_command(tool, payload, work, kit_root=kit_root)
    if not Path(command[1]).is_file():
        raise DesignError("ASSET_MISSING", "The pinned Anthropic kit source image is not staged.")
    completed = _run(command, cwd=work, timeout=1800, env=environment)
    artifacts = collect_kit_outputs(out)
    validate_kit_outputs(tool, payload, artifacts)
    log = completed.stdout + completed.stderr
    if sum(len(artifact["raw"]) for artifact in artifacts) + len(log) > MAX_KIT_OUTPUT_BYTES:
        raise DesignError("OUTPUT_INVALID", "Kit outputs exceed the VAT capture limit.")
    artifacts.append({"name": "kit.log", "kind": "log", "raw": log})
    return {"metrics": {"kit_revision": ANTHROPIC_KIT_REVISION, "mode": "off",
                        "retained_outputs": len(artifacts) - 1}, "artifacts": artifacts}
