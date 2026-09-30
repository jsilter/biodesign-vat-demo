"""Scientific adapters, imported inside isolated Modal images only."""

from __future__ import annotations

import math
import random
import hashlib
import json
import ast
import io
from pathlib import Path

from .design_inputs import DesignError, full_vocabulary_log_probability, masked_metrics, sequence_bytes

ESM_REVISION = "bf343ba264b650dff7a073643725f9aaa1fdbe8d"
ESM3_REVISION = "43b4548b86762edfa747b07d5f440aad3c33acee"
BOLTZ_REVISION = "a3149cf18eeb58648d1abbb27539bd73f746cdda"
PROTEINMPNN_REVISION = "8907e6671bfbfc92303b5f79c4b5e6ce47cdef57"
PROTEINMPNN_REPOSITORY = "https://github.com/dauparas/ProteinMPNN.git"
PROTEINMPNN_CHECKPOINT = "c9cb4a671d79604111231f8dbfc7c590e06f1197453b7a6854ac6661a642f5bd"
PROTEINMPNN_SOLUBLE_CHECKPOINT = "7af52d090172c230c7f0e9d21e02203f6b3a38b16db58d3c7a3960e0a9a6e31a"
PROTEINMPNN_LICENSE = "82009d25ce585631f452b2b24589bdb29c559ccfefa2f200ef312ed5b501a586"
WEIGHTS = {
    "esmfold2": ("biohub/ESMFold2-Fast", "93a69ea81c2c1fa593292b385c62d5f5b19893f9"),
    "esmc": ("biohub/ESMC-300M", "f0d413606442e6b433d5e75e9aae3285ca9b137f"),
    "boltzgen": ("boltzgen/boltzgen-1", "c1be29e1f82ffcc72264f64b993c43fb4e0d17f0"),
}
ESM3_WEIGHTS = ("biohub/esm3-sm-open-v1", "47f0545b2b6daf26a93439a3cd610f4f7f3d5478")
CHEMISTRY = ("biohub/ESMFold2", "b1324ddff3a32630bd5d36dd47213b2723b4ef50")


def validate_weight_lock(cache: Path, lock: dict, tool: str) -> None:
    """Check release identity and every staged file needed by this adapter."""
    from .design_store import json_bytes

    identity = hashlib.sha256(json_bytes({k: v for k, v in lock.items() if k != "identity"})).hexdigest()
    if (not lock.get("reviewed") or lock.get("identity") != identity
            or lock.get("esm_revision") != ESM_REVISION
            or lock.get("boltz_revision") != BOLTZ_REVISION
            or lock.get("proteinmpnn_revision") != PROTEINMPNN_REVISION
            or lock.get("proteinmpnn_repository") != PROTEINMPNN_REPOSITORY
            or any(lock.get("repositories", {}).get(key) != list(value) for key, value in WEIGHTS.items())
            or lock.get("chemistry") != list(CHEMISTRY)):
        raise DesignError("TOOL_UNAVAILABLE", "Staged release lock is unreviewed or has mismatched identities.")
    if tool == "esm3" and (lock.get("esm3_revision") != ESM3_REVISION
                            or lock.get("repositories", {}).get("esm3") != list(ESM3_WEIGHTS)):
        raise DesignError("TOOL_UNAVAILABLE", "Staged ESM3 release identity does not match the reviewed source and checkpoint.")
    files = lock.get("files", {})
    required = {"boltzgen": ["boltzgen/boltzgen1_diverse.ckpt", "boltzgen/boltzgen1_adherence.ckpt",
                            "boltzgen/boltzgen1_ifold.ckpt", "boltzgen/boltz2_conf_final.ckpt", "mols/mols.zip"],
                "esmfold2": ["esmfold2/config.json", "esmfold2/ccd.pkl"],
                "esmc": ["esmc/config.json"],
                "esm3": ["esm3/data/weights/esm3_sm_open_v1.pth",
                         "esm3/data/weights/esm3_structure_encoder_v0.pth",
                         "esm3/data/weights/esm3_structure_decoder_v0.pth",
                         "esm3/data/weights/esm3_function_decoder_v0.pth"],
                "proteinmpnn": ["proteinmpnn/v_48_020.pt", "proteinmpnn/LICENSE"],
                "proteinmpnn-soluble": ["proteinmpnn-soluble/v_48_020.pt", "proteinmpnn-soluble/LICENSE"]}[tool]
    if (any(name not in files for name in required)
            or (tool in {"esmfold2", "esmc"} and not any(name.startswith(tool + "/") and name.endswith(".safetensors") for name in files))):
        raise DesignError("TOOL_UNAVAILABLE", "Required staged model files are missing from the release lock.")
    if tool == "proteinmpnn" and (files.get("proteinmpnn/v_48_020.pt") != PROTEINMPNN_CHECKPOINT
                                  or files.get("proteinmpnn/LICENSE") != PROTEINMPNN_LICENSE):
        raise DesignError("TOOL_UNAVAILABLE", "ProteinMPNN checkpoint or licence identity does not match the reviewed release.")
    if tool == "proteinmpnn-soluble" and (
            files.get("proteinmpnn-soluble/v_48_020.pt") != PROTEINMPNN_SOLUBLE_CHECKPOINT
            or files.get("proteinmpnn-soluble/LICENSE") != PROTEINMPNN_LICENSE):
        raise DesignError("TOOL_UNAVAILABLE", "SolubleMPNN checkpoint or licence identity does not match the reviewed release.")
    prefixes = ("boltzgen/", "mols/") if tool == "boltzgen" else (tool + "/",)
    for name, digest in files.items():
        if not name.startswith(prefixes):
            continue
        path = cache / name
        if path.is_symlink() or not path.resolve().is_relative_to(cache.resolve()) or ".." in Path(name).parts:
            raise DesignError("OUTPUT_INVALID", "Unsafe staged weight path.")
        with path.open("rb") as stream:
            calculated = hashlib.file_digest(stream, "sha256").hexdigest()
        if calculated != digest:
            raise DesignError("OUTPUT_INVALID", "Staged model weight digest mismatch.")


def collect_boltz_artifacts(work: Path) -> list[dict]:
    """Preserve emitted scientific files on both successful and failed pipeline attempts."""
    artifacts = []
    if (work / "design.yaml").is_file():
        artifacts.append({"name": "design.yaml", "kind": "log", "raw": (work / "design.yaml").read_bytes()})
    output = work / "output"
    for path in sorted(output.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.suffix in {".cif", ".csv", ".yaml", ".json", ".npz"}:
            artifacts.append({"name": str(path.relative_to(output)).replace("/", "--"),
                              "kind": "coordinates" if path.suffix == ".cif" else "metric" if path.suffix == ".csv" else "log",
                              "raw": path.read_bytes()})
    return artifacts


def _header_fields(header: str) -> dict[str, str]:
    if len(header) > 2048:
        raise DesignError("OUTPUT_INVALID", "ProteinMPNN FASTA header is too large.")
    items, start, depth, quote = [], 0, 0, None
    for index, character in enumerate(header):
        if character in {"'", '"'}:
            quote = None if quote == character else character if quote is None else quote
        elif quote is None and character in "[({":
            depth += 1
        elif quote is None and character in "])}":
            depth -= 1
            if depth < 0:
                raise DesignError("OUTPUT_INVALID", "Malformed ProteinMPNN FASTA header.")
        elif quote is None and depth == 0 and header[index:index + 2] == ", ":
            items.append(header[start:index])
            start = index + 2
    if depth or quote:
        raise DesignError("OUTPUT_INVALID", "Malformed ProteinMPNN FASTA header.")
    items.append(header[start:])
    fields = {}
    for item in items:
        if "=" not in item:
            raise DesignError("OUTPUT_INVALID", "Malformed ProteinMPNN FASTA header.")
        key, value = item.split("=", 1)
        if not key or key in fields:
            raise DesignError("OUTPUT_INVALID", "Malformed ProteinMPNN FASTA header fields.")
        fields[key] = value
    return fields


def parse_proteinmpnn_outputs(fasta: bytes, scores: bytes, request: dict) -> tuple[list[dict], dict]:
    """Validate exact ProteinMPNN samples, metrics and score arrays before ingestion."""
    import numpy as np
    from .design_inputs import ProteinMPNNRequest

    parsed = ProteinMPNNRequest.model_validate(request)
    if len(fasta) > 1024 * 1024 or len(scores) > 1024 * 1024:
        raise DesignError("OUTPUT_INVALID", "ProteinMPNN FASTA or score output is too large.")
    try:
        lines = fasta.decode("ascii").splitlines()
    except UnicodeError as exc:
        raise DesignError("OUTPUT_INVALID", "ProteinMPNN FASTA must be ASCII.") from exc
    if len(lines) != 2 * (parsed.count + 1) or any(not lines[index].startswith(">") for index in range(0, len(lines), 2)):
        raise DesignError("OUTPUT_INVALID", "ProteinMPNN returned an unexpected FASTA record count.")
    native_header = lines[0][1:].split(", ", 1)
    if len(native_header) != 2 or native_header[0] != "backbone":
        raise DesignError("OUTPUT_INVALID", "ProteinMPNN native record name is unexpected.")
    native = _header_fields(native_header[1])
    required_native = {"score", "global_score", "fixed_chains", "designed_chains", "model_name", "git_hash", "seed"}
    if set(native) != required_native or native["model_name"] != parsed.model \
            or native["git_hash"] != PROTEINMPNN_REVISION or native["seed"] != str(parsed.seed):
        raise DesignError("OUTPUT_INVALID", "ProteinMPNN native header identity does not match the request.")
    try:
        native_scores = (float(native["score"]), float(native["global_score"]))
    except ValueError as exc:
        raise DesignError("OUTPUT_INVALID", "ProteinMPNN native scores are malformed.") from exc
    if any(not math.isfinite(value) for value in native_scores):
        raise DesignError("OUTPUT_INVALID", "ProteinMPNN native scores must be finite.")
    try:
        designed = ast.literal_eval(native["designed_chains"])
        fixed = ast.literal_eval(native["fixed_chains"])
    except (ValueError, SyntaxError) as exc:
        raise DesignError("OUTPUT_INVALID", "ProteinMPNN chain metadata is malformed.") from exc
    expected_design = sorted(parsed.design_chains)
    expected_fixed = sorted(set(parsed.sequences) - set(parsed.design_chains))
    if designed != expected_design or fixed != expected_fixed:
        raise DesignError("OUTPUT_INVALID", "ProteinMPNN designed/fixed chains do not match the request.")
    expected_native = "/".join(parsed.sequences[chain] for chain in expected_design)
    if lines[1] != expected_native:
        raise DesignError("OUTPUT_INVALID", "ProteinMPNN native designed-chain sequence is inconsistent.")
    try:
        with np.load(io.BytesIO(scores), allow_pickle=False) as arrays:
            if set(arrays.files) != {"score", "global_score"}:
                raise DesignError("OUTPUT_INVALID", "ProteinMPNN score archive has unexpected arrays.")
            raw_score = arrays["score"]
            raw_global = arrays["global_score"]
            if (raw_score.shape != (parsed.count,) or raw_global.shape != (parsed.count,)
                    or raw_score.dtype.kind not in "fiu" or raw_global.dtype.kind not in "fiu"):
                raise DesignError("OUTPUT_INVALID", "ProteinMPNN score arrays have invalid dimensions.")
            score_values = raw_score.astype(float).tolist()
            global_values = raw_global.astype(float).tolist()
    except (ValueError, OSError) as exc:
        raise DesignError("OUTPUT_INVALID", "ProteinMPNN score archive is invalid or unsafe.") from exc
    if any(not math.isfinite(value) for value in score_values + global_values):
        raise DesignError("OUTPUT_INVALID", "ProteinMPNN scores must be finite.")

    candidates = []
    seen_samples = set()
    for offset in range(parsed.count):
        fields = _header_fields(lines[2 + offset * 2][1:])
        if set(fields) != {"T", "sample", "score", "global_score", "seq_recovery"}:
            raise DesignError("OUTPUT_INVALID", "ProteinMPNN sample header has unexpected fields.")
        try:
            temperature = float(fields["T"])
            sample = int(fields["sample"])
            score = float(fields["score"])
            global_score = float(fields["global_score"])
            recovery = float(fields["seq_recovery"])
        except ValueError as exc:
            raise DesignError("OUTPUT_INVALID", "ProteinMPNN sample metrics are malformed.") from exc
        if (sample in seen_samples or sample != offset + 1 or not math.isclose(temperature, parsed.sampling_temperature)
                or any(not math.isfinite(value) for value in (score, global_score, recovery)) or not 0 <= recovery <= 1
                or not math.isclose(score, score_values[offset], abs_tol=5e-4)
                or not math.isclose(global_score, global_values[offset], abs_tol=5e-4)):
            raise DesignError("OUTPUT_INVALID", "ProteinMPNN sample metrics do not match the request or score archive.")
        seen_samples.add(sample)
        groups = lines[3 + offset * 2].split("/")
        if len(groups) != len(expected_design):
            raise DesignError("OUTPUT_INVALID", "ProteinMPNN sample chain count is inconsistent.")
        for chain, sequence in zip(expected_design, groups):
            sequence_bytes(sequence)
            if len(sequence) != len(parsed.sequences[chain]):
                raise DesignError("OUTPUT_INVALID", "ProteinMPNN sample chain length is inconsistent.")
            candidates.append({"source": f"sample-{sample}-chain-{chain}", "sequence": sequence,
                               "rank": sample, "sample": sample, "chain": chain,
                               "metrics": {"score": score, "global_score": global_score,
                                           "seq_recovery": recovery, "sampling_temperature": temperature}})
    return candidates, {"samples": parsed.count, "designed_chains": expected_design,
                        "fixed_chains": expected_fixed, "model": parsed.model,
                        "seed": parsed.seed, "sampling_temperature": parsed.sampling_temperature}


def collect_proteinmpnn_artifacts(output: Path) -> list[dict]:
    """Collect only expected non-symlink ProteinMPNN output formats."""
    artifacts = []
    for path in (output / "seqs" / "backbone.fa", output / "scores" / "backbone.npz"):
        if path.is_file() and not path.is_symlink():
            artifacts.append({"name": str(path.relative_to(output)).replace("/", "--"),
                              "kind": "fasta" if path.suffix == ".fa" else "metric", "raw": path.read_bytes()})
    return artifacts


def esmc_masked_score(model, tokenizer, sequence: str, *, batch_size: int = 16) -> dict:
    """Mask each position independently and score it over the full model vocabulary."""
    import torch

    sequence_bytes(sequence)
    if batch_size < 1:
        raise ValueError("mask batch size must be positive")
    tokens = tokenizer(sequence, return_tensors="pt")["input_ids"].to(model.device)
    if tokens.shape[1] != len(sequence) + 2:
        raise DesignError("OUTPUT_INVALID", "Tokenizer does not map one token per residue plus BOS/EOS.")
    mask_id = tokenizer.mask_token_id
    if not isinstance(mask_id, int):
        raise DesignError("OUTPUT_INVALID", "Tokenizer has no mask token.")
    values = []
    with torch.inference_mode():
        for start in range(1, len(sequence) + 1, batch_size):
            positions = torch.arange(start, min(start + batch_size, len(sequence) + 1), device=model.device)
            rows = torch.arange(len(positions), device=model.device)
            batch = tokens.repeat(len(positions), 1)
            original = batch[rows, positions].clone()
            batch[rows, positions] = mask_id
            logits = model(input_ids=batch, attention_mask=torch.ones_like(batch)).logits
            selected_logits = logits[rows, positions].float().cpu().tolist()
            values.extend(full_vocabulary_log_probability(row, token)
                          for row, token in zip(selected_logits, original.cpu().tolist()))
    return {**masked_metrics(values), "model": "ESMC-300M", "mask_batch_size": batch_size}


def fold_result(result, sequences: dict[str, str]) -> tuple[dict, list[dict]]:
    """Validate available confidence values and retain exact prediction arrays."""
    if isinstance(result, list):
        if len(result) != 1:
            raise DesignError("OUTPUT_INVALID", "Expected one folding sample.")
        result = result[0]
    plddt = result.plddt.detach().float().cpu().reshape(-1).tolist() if result.plddt is not None else None
    length = sum(len(s) for s in sequences.values())
    if plddt is None or len(plddt) != length or any(not math.isfinite(v) or not 0 <= v <= 1 for v in plddt):
        raise DesignError("OUTPUT_INVALID", "Invalid pLDDT dimensions or raw 0–1 scale.")
    metrics = {"plddt_raw_scale": "0–1", "mean_plddt": math.fsum(plddt)/length,
               "plddt": plddt, "ptm": None, "iptm": None}
    for name in ["ptm", "iptm"]:
        value = getattr(result, name)
        if name == "iptm" and len(sequences) == 1:
            continue
        if value is not None:
            value = float(value)
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise DesignError("OUTPUT_INVALID", f"Invalid {name} confidence.")
            metrics[name] = value
    if result.pae is not None:
        pae = result.pae.detach().float().cpu()
        if tuple(pae.shape) != (length, length) or not pae.isfinite().all() or (pae < 0).any():
            raise DesignError("OUTPUT_INVALID", "Invalid PAE dimensions or values.")
        metrics["pae"] = pae.tolist()
    cif = result.complex.to_mmcif().encode()
    validate_cif_sequences(cif, sequences)
    return metrics, [{"name": "prediction.cif", "kind": "coordinates", "raw": cif}]


def validate_cif_sequences(raw: bytes, sequences: dict[str, str]) -> None:
    """Check predicted coordinate-chain identities against exact inference inputs."""
    import gemmi

    structure = gemmi.make_structure_from_block(gemmi.cif.read_string(raw.decode()).sole_block())
    if len(structure) != 1:
        raise DesignError("OUTPUT_INVALID", "Prediction must have one coordinate model.")
    if len(structure[0]) != len(sequences) or len({c.name for c in structure[0]}) != len(sequences):
        raise DesignError("OUTPUT_INVALID", "Prediction must have exactly the expected distinct protein chains.")
    actual = {chain.name: "".join(gemmi.find_tabulated_residue(r.name).one_letter_code for r in chain)
              for chain in structure[0]}
    if actual != sequences:
        raise DesignError("OUTPUT_INVALID", "Prediction chain sequences do not match inference inputs.")
    for chain in structure[0]:
        for residue in chain:
            if not any(atom.name == "CA" for atom in residue):
                raise DesignError("OUTPUT_INVALID", "Predicted residue has no C-alpha coordinate.")
            for atom in residue:
                if not all(math.isfinite(v) for v in [atom.pos.x, atom.pos.y, atom.pos.z]):
                    raise DesignError("OUTPUT_INVALID", "Prediction has invalid coordinates.")


def run_esmc(request: dict, cache: Path) -> dict:
    """Run local ESMC-300M masked scoring from a pinned staged checkpoint."""
    from esm.models.esmc import EsmcForMaskedLM, EsmcTokenizer

    model = EsmcForMaskedLM.from_pretrained(str(cache / "esmc"), device="cuda", local_files_only=True).eval()
    tokenizer = EsmcTokenizer()
    return {"metrics": esmc_masked_score(model, tokenizer, request["sequence"]), "artifacts": []}


def run_esm3(request: dict, cache: Path, work: Path) -> dict:
    """Generate one ESM3 sequence or structure track from the staged open checkpoint."""
    import os
    import torch
    from esm.models.esm3 import ESM3
    from esm.sdk.api import ESMProtein, ESMProteinError, GenerationConfig

    sequence = request["sequence"]
    task = request["task"]
    if task == "score":
        raise DesignError("INVALID_REQUEST", "ESM3 scoring has no defined metric in this adapter.")
    if task == "generate_sequence" and "_" not in sequence:
        raise DesignError("INVALID_REQUEST", "ESM3 sequence generation requires at least one masked residue.")
    if task == "generate_structure" and "_" in sequence:
        raise DesignError("INVALID_REQUEST", "ESM3 structure generation requires a complete sequence.")

    root = (cache / "esm3").resolve()
    if not root.is_dir():
        raise DesignError("TOOL_UNAVAILABLE", "The staged ESM3 checkpoint directory is missing.")
    previous_directory = Path.cwd()
    previous_provider = os.environ.get("INFRA_PROVIDER")
    artifacts = []
    outputs = []
    try:
        os.environ["INFRA_PROVIDER"] = "vat-local-checkpoint"
        os.chdir(root)
        model = ESM3.from_pretrained("esm3-sm-open-v1", device=torch.device("cuda")).eval()
        for offset in range(request["count"]):
            sample = offset + 1
            sample_seed = request["seed"] + offset
            random.seed(sample_seed)
            torch.manual_seed(sample_seed)
            torch.cuda.manual_seed_all(sample_seed)
            track = "sequence" if task == "generate_sequence" else "structure"
            steps = min(20, len(sequence))
            generated = model.generate(
                ESMProtein(sequence=sequence),
                GenerationConfig(track=track, num_steps=steps,
                                 temperature=request["temperature"]),
            )
            if isinstance(generated, ESMProteinError):
                raise DesignError("OUTPUT_INVALID", "ESM3 returned a generation error.")
            if task == "generate_sequence":
                output = generated.sequence
                if not isinstance(output, str) or len(output) != len(sequence) or "_" in output:
                    raise DesignError("OUTPUT_INVALID", "ESM3 returned an incomplete sequence track.")
                sequence_bytes(output)
                raw = f">esm3-sample-{sample}\n{output}\n".encode("ascii")
                artifacts.append({"name": f"sample-{sample}.fasta", "kind": "aa_sequence", "raw": raw})
                outputs.append({"sample": sample, "seed": sample_seed, "sequence": output})
            else:
                if generated.sequence != sequence or generated.coordinates is None:
                    raise DesignError("OUTPUT_INVALID", "ESM3 structure output does not match its sequence input.")
                raw = generated.to_pdb_string().encode("utf-8")
                _validate_esm3_pdb(raw, sequence)
                artifacts.append({"name": f"sample-{sample}.pdb", "kind": "coordinates", "raw": raw})
                outputs.append({"sample": sample, "seed": sample_seed,
                                "mean_plddt_raw_0_1": _finite_optional_mean(generated.plddt, len(sequence))})
    finally:
        os.chdir(previous_directory)
        if previous_provider is None:
            os.environ.pop("INFRA_PROVIDER", None)
        else:
            os.environ["INFRA_PROVIDER"] = previous_provider
    return {"metrics": {"model": "esm3-sm-open-v1", "task": task,
                        "requested": request["count"], "generated": len(outputs),
                        "temperature": request["temperature"], "samples": outputs},
            "artifacts": artifacts}


def _finite_optional_mean(values, length: int) -> float | None:
    """Return an optional ESM confidence mean while preserving absence honestly."""
    if values is None:
        return None
    raw = values.detach().float().cpu().reshape(-1).tolist()
    if len(raw) != length or any(not math.isfinite(value) or not 0 <= value <= 1 for value in raw):
        raise DesignError("OUTPUT_INVALID", "ESM3 returned invalid per-residue confidence values.")
    return math.fsum(raw) / length


def _validate_esm3_pdb(raw: bytes, sequence: str) -> None:
    """Bind a generated PDB artifact to the complete input sequence and finite coordinates."""
    import gemmi

    try:
        structure = gemmi.read_pdb_string(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RuntimeError) as exc:
        raise DesignError("OUTPUT_INVALID", "ESM3 returned an unreadable PDB artifact.") from exc
    chains = list(structure[0]) if len(structure) else []
    if len(chains) != 1:
        raise DesignError("OUTPUT_INVALID", "ESM3 structure generation must return one protein chain.")
    actual = "".join(gemmi.find_tabulated_residue(residue.name).one_letter_code
                     for residue in chains[0])
    if actual != sequence:
        raise DesignError("OUTPUT_INVALID", "ESM3 structure residues do not match the input sequence.")
    for residue in chains[0]:
        if not any(atom.name == "CA" for atom in residue):
            raise DesignError("OUTPUT_INVALID", "ESM3 returned a residue without a C-alpha atom.")
        if any(not all(math.isfinite(value) for value in (atom.pos.x, atom.pos.y, atom.pos.z))
               for atom in residue):
            raise DesignError("OUTPUT_INVALID", "ESM3 returned non-finite coordinates.")


def run_fold(request: dict, cache: Path) -> dict:
    """Predict one binder or protein complex with ESMFold2-Fast; no templates/MSAs."""
    from esm.models.esmfold2 import EsmFold2Model, ESMFold2InputBuilder, ProteinInput, StructurePredictionInput, load_ccd

    sequences = {"B": request["sequence"]}
    if request["mode"] == "complex":
        sequences = {"A": request["target_sequence"], **sequences}
    for sequence in sequences.values():
        sequence_bytes(sequence)
    # The selected Fast checkpoint bundles its ESMC backbone. Never fetch an unpinned fallback.
    load_ccd(cache / "esmfold2")
    model = EsmFold2Model.from_pretrained(str(cache / "esmfold2"), device="cuda", local_files_only=True, load_esmc=False).eval()
    if model.config.esmc_config is None:
        raise DesignError("DEPENDENCY_MISMATCH", "Folding checkpoint must bundle its language-model backbone.")
    inputs = StructurePredictionInput(sequences=[ProteinInput(id=id, sequence=s) for id, s in sequences.items()])
    result = ESMFold2InputBuilder().fold(model, inputs, seed=request["seed"], num_loops=request.get("num_loops", 10),
                                       num_sampling_steps=request.get("num_sampling_steps", 68), num_diffusion_samples=1)
    metrics, artifacts = fold_result(result, sequences)
    return {"metrics": metrics, "artifacts": artifacts}


def run_boltzgen(request: dict, cache: Path, work: Path) -> dict:
    """Run BoltzGen's protein-anything pipeline and retain candidate outputs."""
    import numpy as np
    import torch
    import sys
    import gemmi
    import yaml
    from boltzgen.cli import boltzgen as cli

    random.seed(request["seed"])
    np.random.seed(request["seed"])
    torch.manual_seed(request["seed"])
    # Replace all upstream floating repository defaults with staged local artifacts.
    cli.ARTIFACTS.update({
        "design-diverse": (str(cache / "boltzgen" / "boltzgen1_diverse.ckpt"), "model"),
        "design-adherence": (str(cache / "boltzgen" / "boltzgen1_adherence.ckpt"), "model"),
    })
    (work / "target.cif").write_text(request["target_cif"], encoding="utf-8")
    target = {"path": "target.cif", "include": [{"chain": {"id": "A"}}]}
    if request["binding_residues"]:
        target["binding_types"] = [{"chain": {"id": "A", "binding": ",".join(map(str, request["binding_residues"]))}}]
    spec = {"entities": [{"file": target}, {"protein": {"id": "B", "sequence": f'{request["min_length"]}..{request["max_length"]}'}}]}
    (work / "design.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    # Run the official entrypoint in-process so all three RNG seeds are initialized.
    previous = sys.argv
    sys.argv = ["boltzgen", "run", str(work / "design.yaml"), "--output", str(work / "output"),
                "--protocol", "protein-anything", "--num_designs", str(request["count"]),
                "--no_subprocess",
                "--devices", "1", "--num_workers", "0",
                "--config", "filtering", f'random_state={request["seed"]}',
                "--budget", str(request["count"]), "--cache", str(cache),
                "--inverse_fold_checkpoint", str(cache / "boltzgen" / "boltzgen1_ifold.ckpt"),
                "--folding_checkpoint", str(cache / "boltzgen" / "boltz2_conf_final.ckpt"),
                "--moldir", str(cache / "mols" / "mols.zip")]
    try:
        cli.main()
    finally:
        sys.argv = previous
    output = work / "output"
    artifacts = collect_boltz_artifacts(work)
    candidates = []
    directory = output / "final_ranked_designs" / f'final_{request["count"]}_designs'
    if not directory.is_dir():
        raise DesignError("OUTPUT_INVALID", "BoltzGen did not publish its final selection directory.")
    for path in sorted(directory.glob("*.cif")):
        raw = path.read_bytes()
        structure = gemmi.make_structure_from_block(gemmi.cif.read_string(raw.decode()).sole_block())
        chains = {c.name: "".join(gemmi.find_tabulated_residue(r.name).one_letter_code for r in c) for c in structure[0]}
        if "B" not in chains or chains.get("A") != request["target_sequence"]:
            raise DesignError("OUTPUT_INVALID", "Generated complex has unexpected target/binder chains.")
        sequence_bytes(chains["B"])
        validate_cif_sequences(raw, {"A": request["target_sequence"], "B": chains["B"]})
        import re
        rank = re.match(r"rank(\d+)_", path.stem)
        candidates.append({"source": path.stem, "sequence": chains["B"], "rank": int(rank[1]) if rank else None})
    generated = len(list((output / "intermediate_designs").glob("*.cif")))
    return {"candidates": candidates, "artifacts": artifacts,
            "metrics": {"requested": request["count"], "generated": generated, "retained": len(candidates),
                        "seed": request["seed"], "filter_random_state": request["seed"], "execution_mode": "in-process"}}


def run_proteinmpnn(request: dict, cache: Path, work: Path, *, source: Path = Path("/opt/proteinmpnn"),
                    variant: str = "vanilla") -> dict:
    """Run the pinned ProteinMPNN entry point with a fixed, typed argument surface."""
    import subprocess
    import sys
    from .design_inputs import ProteinMPNNRequest, prepare_backbone

    if variant not in {"vanilla", "soluble"}:
        raise DesignError("INVALID_REQUEST", "Unknown ProteinMPNN checkpoint variant.")
    parsed = ProteinMPNNRequest.model_validate(request)
    prepared = prepare_backbone(parsed.backbone_cif.encode(), "cif")
    if prepared["sequences"] != parsed.sequences:
        raise DesignError("OUTPUT_INVALID", "Prepared ProteinMPNN backbone does not match declared sequences.")
    output = work / "proteinmpnn-output"
    output.mkdir(parents=True, exist_ok=True)
    backbone = work / "backbone.pdb"
    backbone.write_text(prepared["prepared_pdb"], encoding="utf-8")
    command = [sys.executable, str(source / "protein_mpnn_run.py"),
               "--pdb_path", str(backbone), "--pdb_path_chains", " ".join(sorted(parsed.design_chains)),
               "--out_folder", str(output), "--path_to_model_weights",
               str(cache / ("proteinmpnn-soluble" if variant == "soluble" else "proteinmpnn")),
               "--model_name", parsed.model, "--num_seq_per_target", str(parsed.count), "--batch_size", "1",
               "--sampling_temp", str(parsed.sampling_temperature), "--seed", str(parsed.seed),
               "--save_score", "1", "--suppress_print", "1"]
    if variant == "soluble":
        command.append("--use_soluble_model")
    try:
        completed = subprocess.run(command, cwd=source, capture_output=True, timeout=1740, check=False)
    except subprocess.TimeoutExpired as exc:
        raise DesignError("EXECUTION_FAILED", "ProteinMPNN timed out; inspect the retained attempt.") from exc
    if completed.returncode:
        raise DesignError("EXECUTION_FAILED", "ProteinMPNN exited unsuccessfully; inspect the retained attempt.")
    fasta_path = output / "seqs" / "backbone.fa"
    score_path = output / "scores" / "backbone.npz"
    if not fasta_path.is_file() or fasta_path.is_symlink() or not score_path.is_file() or score_path.is_symlink():
        raise DesignError("OUTPUT_INVALID", "ProteinMPNN did not publish its expected FASTA and scores.")
    fasta, scores = fasta_path.read_bytes(), score_path.read_bytes()
    candidates, metrics = parse_proteinmpnn_outputs(fasta, scores, parsed.model_dump())
    metrics["variant"] = variant
    return {"candidates": candidates, "metrics": metrics,
            "artifacts": collect_proteinmpnn_artifacts(output)}
