"""Bounded CPU and shared workers for the standalone scientific suite."""

from __future__ import annotations

import hashlib
import math
import re
import subprocess
import sys
from pathlib import Path

from .design_inputs import DesignError
from .design_store import json_bytes
from vatlog.canon import parse_json

MAX_UTILITY_OUTPUT = 16 * 1024 * 1024
IPSAE_COLUMNS = ("chain1", "chain2", "pae_cutoff", "distance_cutoff", "type", "ipsae",
                 "ipsae_d0chn", "ipsae_d0dom", "iptm_af", "iptm_d0chn", "pdockq", "pdockq2",
                 "lis", "n0res", "n0chn", "n0dom", "d0res", "d0chn", "d0dom", "nres1",
                 "nres2", "dist1", "dist2", "model")


def build_search_database_manifest(tool: str, identity: str, release: str,
                                   prefix: str, directory: Path) -> dict:
    """Create the exact flat-file manifest accepted by the search workers."""
    if (tool not in {"mmseqs2", "foldseek"} or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", identity)
            or not release.strip() or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", prefix)
            or directory.is_symlink() or not directory.is_dir()):
        raise DesignError("INVALID_REQUEST", "Search database staging identity is invalid.")
    files = []
    for path in sorted(directory.iterdir()):
        if path.name == "manifest.json":
            continue
        if path.is_symlink() or not path.is_file():
            raise DesignError("ASSET_MISMATCH", "Search database must contain only flat regular files.")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        files.append({"path": path.name, "sha256": digest, "bytes_len": path.stat().st_size})
    if not files or not any(entry["path"] == prefix or entry["path"].startswith(prefix + ".")
                            for entry in files):
        raise DesignError("ASSET_MISSING", "Search database prefix files are absent.")
    return {"schema": "protein-search-database/1", "tool": tool, "identity": identity,
            "release": release, "prefix": prefix, "files": files}


def _run(command: list[str], *, cwd: Path, timeout: int = 300,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    try:
        completed = subprocess.run(command, cwd=cwd, capture_output=True, timeout=timeout, check=False, env=env)
    except subprocess.TimeoutExpired as exc:
        raise DesignError("EXECUTION_FAILED", "Scientific utility timed out; inspect the retained attempt.") from exc
    if completed.returncode:
        raise DesignError("EXECUTION_FAILED", "Scientific utility exited unsuccessfully; inspect the retained attempt.")
    if len(completed.stdout) > MAX_UTILITY_OUTPUT or len(completed.stderr) > MAX_UTILITY_OUTPUT:
        raise DesignError("OUTPUT_INVALID", "Scientific utility emitted an oversized log.")
    return completed


def parse_dssp(raw: bytes) -> list[dict]:
    """Parse DSSP's documented fixed-column residue table with exact identities."""
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeError as exc:
        raise DesignError("OUTPUT_INVALID", "DSSP output must be ASCII.") from exc
    try:
        start = next(index for index, line in enumerate(lines)
                     if line.startswith("  #  RESIDUE AA STRUCTURE")) + 1
    except StopIteration as exc:
        raise DesignError("OUTPUT_INVALID", "DSSP output lacks its residue-table header.") from exc
    assignments, identities = [], set()
    codes = frozenset(" HBEGIPTS")
    for line in lines[start:]:
        if not line.strip():
            continue
        if len(line) < 17:
            raise DesignError("OUTPUT_INVALID", "DSSP residue row is truncated.")
        try:
            index = int(line[0:5])
        except ValueError as exc:
            raise DesignError("OUTPUT_INVALID", "DSSP residue index is malformed.") from exc
        amino_acid = line[13]
        if amino_acid == "!":
            continue
        chain, residue, code = line[11].strip(), line[5:11].strip(), line[16]
        if (index < 1 or not chain or not residue or not amino_acid.isalpha()
                or code not in codes or (chain, residue) in identities):
            raise DesignError("OUTPUT_INVALID", "DSSP residue identity or assignment is invalid.")
        identities.add((chain, residue))
        assignments.append({"index": index, "chain": chain, "residue": residue,
                            "amino_acid": amino_acid.upper(), "secondary_structure": code})
    if not assignments:
        raise DesignError("OUTPUT_INVALID", "DSSP returned no residue assignments.")
    return assignments


def parse_tmalign(raw: bytes) -> dict:
    """Retain both asymmetric TM-score normalizations and the reported alignment."""
    try:
        text = raw.decode("ascii")
    except UnicodeError as exc:
        raise DesignError("OUTPUT_INVALID", "TM-align output must be ASCII.") from exc
    lengths = re.findall(r"Length of Chain_(\d):\s+(\d+) residues", text)
    summary = re.search(r"Aligned length=\s*(\d+),\s*RMSD=\s*([0-9.eE+-]+),\s*Seq_ID=n_identical/n_aligned=\s*([0-9.eE+-]+)", text)
    scores = re.findall(r"TM-score=\s*([0-9.eE+-]+).*normalized by length of Chain_(\d)", text)
    marker = '(":" denotes residue pairs of d <  5.0 Angstrom'
    lines = text.splitlines()
    marker_indices = [index for index, line in enumerate(lines) if line.startswith(marker)]
    if len(lengths) != 2 or summary is None or len(scores) != 2 or len(marker_indices) != 1:
        raise DesignError("OUTPUT_INVALID", "TM-align output lacks required metrics or alignment.")
    length_by_chain = {int(chain): int(length) for chain, length in lengths}
    score_by_chain = {int(chain): float(score) for score, chain in scores}
    aligned, rmsd, identity = int(summary[1]), float(summary[2]), float(summary[3])
    alignment = [line.rstrip("\r") for line in lines[marker_indices[0] + 1:] if line][:3]
    values = [rmsd, identity, *score_by_chain.values()]
    if (set(length_by_chain) != {1, 2} or set(score_by_chain) != {1, 2}
            or not 1 <= aligned <= min(length_by_chain.values()) or len(alignment) != 3
            or any(not math.isfinite(value) for value in values) or rmsd < 0
            or not 0 <= identity <= 1 or any(not 0 <= value <= 1 for value in score_by_chain.values())):
        raise DesignError("OUTPUT_INVALID", "TM-align metrics are inconsistent or nonfinite.")
    if len(alignment[0]) != len(alignment[1]) or len(alignment[0]) != len(alignment[2]):
        raise DesignError("OUTPUT_INVALID", "TM-align alignment rows have inconsistent lengths.")
    return {"length_chain_1": length_by_chain[1], "length_chain_2": length_by_chain[2],
            "aligned_length": aligned, "rmsd_angstrom": rmsd, "sequence_identity": identity,
            "tm_score_normalized_chain_1": score_by_chain[1],
            "tm_score_normalized_chain_2": score_by_chain[2],
            "alignment": {"chain_1": alignment[0], "markers": alignment[1], "chain_2": alignment[2]}}


def parse_dockq(raw: bytes, expected_mapping: dict[str, str]) -> dict:
    """Validate DockQ's JSON report without trusting its file path fields."""
    try:
        value = parse_json(raw)
    except ValueError as exc:
        raise DesignError("OUTPUT_INVALID", "DockQ JSON output is malformed.") from exc
    if not isinstance(value, dict) or not isinstance(value.get("best_mapping"), dict):
        raise DesignError("OUTPUT_INVALID", "DockQ JSON output lacks its mapping.")
    observed = value["best_mapping"]
    if not ({str(k): str(v) for k, v in observed.items()} == expected_mapping
            or {str(v): str(k) for k, v in observed.items()} == expected_mapping):
        raise DesignError("OUTPUT_INVALID", "DockQ mapping differs from the requested chain identity.")
    global_score = value.get("GlobalDockQ")
    interfaces = value.get("best_result")
    if type(global_score) not in {int, float} or not math.isfinite(global_score) or not 0 <= global_score <= 1 \
            or not isinstance(interfaces, dict) or not interfaces:
        raise DesignError("OUTPUT_INVALID", "DockQ output lacks a valid global or interface score.")
    checked = []
    for identity, metrics in sorted(interfaces.items()):
        if not isinstance(identity, str) or not isinstance(metrics, dict):
            raise DesignError("OUTPUT_INVALID", "DockQ interface result is malformed.")
        retained = {}
        for name in ("DockQ", "DockQ_F1", "iRMSD", "LRMSD", "fnat", "fnonnat", "F1", "clashes"):
            if name not in metrics:
                continue
            number = metrics[name]
            if type(number) not in {int, float} or not math.isfinite(number) or number < 0 \
                    or (name not in {"iRMSD", "LRMSD", "clashes"} and number > 1):
                raise DesignError("OUTPUT_INVALID", "DockQ interface metric is invalid.")
            retained[name] = number
        if "DockQ" not in retained:
            raise DesignError("OUTPUT_INVALID", "DockQ interface score is absent.")
        checked.append({"interface": identity, "metrics": retained})
    return {"global_dockq": global_score, "mapping": expected_mapping, "interfaces": checked}


def parse_ipsae(raw: bytes, *, expected_pae_cutoff: float | None = None,
                expected_distance_cutoff: float | None = None) -> dict:
    """Parse directional ipSAE rows and independently derive each unordered minimum."""
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise DesignError("OUTPUT_INVALID", "ipSAE output must be UTF-8.") from exc
    rows = []
    for line in lines:
        fields = line.split()
        if len(fields) != len(IPSAE_COLUMNS) or fields[4] != "asym":
            continue
        row = dict(zip(IPSAE_COLUMNS, fields))
        try:
            for key in ("pae_cutoff", "distance_cutoff", "ipsae", "ipsae_d0chn", "ipsae_d0dom",
                        "iptm_af", "iptm_d0chn", "pdockq", "pdockq2", "lis", "d0res", "d0chn", "d0dom"):
                row[key] = float(row[key])
            for key in ("n0res", "n0chn", "n0dom", "nres1", "nres2", "dist1", "dist2"):
                row[key] = int(row[key])
        except ValueError as exc:
            raise DesignError("OUTPUT_INVALID", "ipSAE output contains malformed metrics.") from exc
        unit_metrics = ("ipsae", "ipsae_d0chn", "ipsae_d0dom", "iptm_af", "iptm_d0chn",
                        "pdockq", "pdockq2", "lis")
        scale_metrics = ("pae_cutoff", "distance_cutoff", "d0res", "d0chn", "d0dom")
        count_metrics = ("n0res", "n0chn", "n0dom", "nres1", "nres2", "dist1", "dist2")
        if (row["chain1"] == row["chain2"]
                or any(not math.isfinite(row[key]) for key in (*unit_metrics, *scale_metrics))
                or any(not 0 <= row[key] <= 1 for key in unit_metrics)
                or any(row[key] <= 0 for key in scale_metrics)
                or any(row[key] < 0 for key in count_metrics)):
            raise DesignError("OUTPUT_INVALID", "ipSAE directional metrics are inconsistent or nonfinite.")
        if ((expected_pae_cutoff is not None and row["pae_cutoff"] != expected_pae_cutoff)
                or (expected_distance_cutoff is not None
                    and row["distance_cutoff"] != expected_distance_cutoff)):
            raise DesignError("OUTPUT_INVALID", "ipSAE output cutoffs differ from the submitted request.")
        rows.append(row)
    if not rows:
        raise DesignError("OUTPUT_INVALID", "ipSAE returned no directional chain-pair scores.")
    directed = {(row["chain1"], row["chain2"]): row for row in rows}
    summaries = []
    for pair in sorted({tuple(sorted(pair)) for pair in directed}):
        forward, reverse = directed.get(pair), directed.get(pair[::-1])
        if forward is None or reverse is None:
            raise DesignError("OUTPUT_INVALID", "ipSAE lacks both directions for a chain pair.")
        summaries.append({"chains": list(pair), "minimum_ipsae": min(forward["ipsae"], reverse["ipsae"]),
                          "forward_ipsae": forward["ipsae"], "reverse_ipsae": reverse["ipsae"]})
    return {"definition": "IPSAE-v4/directional-and-derived-minimum/1",
            "directional": rows, "pairs": summaries}


def _write_structures(structures: list[str], work: Path) -> list[Path]:
    paths = []
    for index, raw in enumerate(structures, 1):
        path = work / f"structure-{index}.cif"
        path.write_text(raw, encoding="utf-8")
        paths.append(path)
    return paths


def _search_database(tool: str, identity: str, root: Path) -> tuple[Path, dict, str]:
    """Authenticate one staged immutable search database and return its database prefix."""
    directory = root / identity
    manifest_path = directory / "manifest.json"
    if directory.is_symlink() or manifest_path.is_symlink() or not manifest_path.is_file():
        raise DesignError("ASSET_MISSING", "The requested search database is not staged.")
    try:
        manifest = parse_json(manifest_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise DesignError("ASSET_MISMATCH", "The search database manifest is malformed.") from exc
    if (not isinstance(manifest, dict) or manifest.get("schema") != "protein-search-database/1"
            or manifest.get("tool") != tool or manifest.get("identity") != identity
            or not isinstance(manifest.get("release"), str) or not manifest["release"]
            or not isinstance(manifest.get("prefix"), str)
            or not isinstance(manifest.get("files"), list) or not manifest["files"]):
        raise DesignError("ASSET_MISMATCH", "The search database identity does not match the request.")
    prefix = Path(manifest["prefix"])
    if prefix.is_absolute() or len(prefix.parts) != 1 or prefix.name in {"", ".", ".."}:
        raise DesignError("ASSET_MISMATCH", "The search database prefix is unsafe.")
    seen = set()
    for entry in manifest["files"]:
        if (not isinstance(entry, dict) or set(entry) != {"path", "sha256", "bytes_len"}
                or not isinstance(entry["path"], str) or not isinstance(entry["sha256"], str)
                or type(entry["bytes_len"]) is not int or entry["bytes_len"] < 0):
            raise DesignError("ASSET_MISMATCH", "The search database file manifest is malformed.")
        relative = Path(entry["path"])
        if (relative.is_absolute() or len(relative.parts) != 1 or relative.name in {"", ".", ".."}
                or relative.name in seen):
            raise DesignError("ASSET_MISMATCH", "The search database file path is unsafe or duplicated.")
        seen.add(relative.name)
        path = directory / relative
        if path.is_symlink() or not path.is_file():
            raise DesignError("ASSET_MISSING", "A staged search database file is absent.")
        raw = path.read_bytes()
        if len(raw) != entry["bytes_len"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise DesignError("ASSET_MISMATCH", "A staged search database file failed integrity validation.")
    actual = {path.name for path in directory.iterdir() if path.name != "manifest.json"}
    if actual != seen or not any(name == prefix.name or name.startswith(prefix.name + ".") for name in seen):
        raise DesignError("ASSET_MISMATCH", "The search database contains unmanifested or unrelated files.")
    return directory / prefix, manifest, hashlib.sha256(json_bytes(manifest)).hexdigest()


def _parse_search_tsv(raw: bytes, *, foldseek: bool, max_hits: int) -> list[dict]:
    columns = (["query", "target", "evalue", "bits", "fident", "alntmscore", "qtmscore", "ttmscore"]
               if foldseek else ["query", "target", "evalue", "bits", "fident", "qcov", "tcov"])
    hits = []
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise DesignError("OUTPUT_INVALID", "Search output must be UTF-8 TSV.") from exc
    if len(lines) > max_hits:
        raise DesignError("OUTPUT_INVALID", "Search returned more hits than requested.")
    for line in lines:
        fields = line.split("\t")
        if len(fields) != len(columns) or any(not field for field in fields[:2]):
            raise DesignError("OUTPUT_INVALID", "Search output has malformed columns.")
        if any(len(field) > 1024 or any(ord(character) < 32 for character in field)
               for field in fields[:2]):
            raise DesignError("OUTPUT_INVALID", "Search output contains an invalid identifier.")
        try:
            numbers = [float(value) for value in fields[2:]]
        except ValueError as exc:
            raise DesignError("OUTPUT_INVALID", "Search output contains malformed metrics.") from exc
        if any(not math.isfinite(value) or value < 0 for value in numbers[:2]):
            raise DesignError("OUTPUT_INVALID", "Search output contains invalid significance metrics.")
        if any(not math.isfinite(value) or value < 0 or value > 1 for value in numbers[2:]):
            raise DesignError("OUTPUT_INVALID", "Search output contains invalid normalized metrics.")
        hits.append(dict(zip(columns, fields[:2] + numbers)))
    return hits


def _as_pdb(source: Path, destination: Path) -> int:
    import gemmi
    try:
        structure = gemmi.make_structure_from_block(gemmi.cif.read(source).sole_block())
    except (ValueError, RuntimeError) as exc:
        raise DesignError("INVALID_REQUEST", "Utility structure is not valid mmCIF.") from exc
    if len(structure) != 1:
        raise DesignError("INVALID_REQUEST", "Utility structure must contain exactly one model.")
    if any(len(chain.name) != 1 for chain in structure[0]):
        raise DesignError("INVALID_REQUEST", "This utility requires one-character prepared chain IDs.")
    residues = [residue for chain in structure[0] for residue in chain]
    if not residues or any(not gemmi.find_tabulated_residue(residue.name).is_amino_acid()
                           or not any(atom.name == "CA" for atom in residue) for residue in residues):
        raise DesignError("INVALID_REQUEST", "This utility requires modeled protein residues with C-alpha atoms.")
    structure.write_pdb(str(destination))
    return len(residues)


def run_utility(tool: str, payload: dict, work: Path, *, databases: Path = Path("/databases")) -> dict:
    """Run one fixed CPU utility command; callers cannot supply commands or paths."""
    structures = _write_structures(payload.get("structures", []), work)
    if tool == "dssp":
        output = work / "assignments.dssp"
        _run(["mkdssp", "--output-format=dssp", str(structures[0]), str(output)], cwd=work)
        raw = output.read_bytes()
        return {"metrics": {"assignments": parse_dssp(raw)},
                "artifacts": [{"name": "assignments.dssp", "kind": "metric", "raw": raw}]}
    if tool == "tmalign":
        completed = _run(["/opt/tmalign/TMalign", str(structures[0]), str(structures[1])], cwd=work)
        return {"metrics": parse_tmalign(completed.stdout),
                "artifacts": [{"name": "tmalign.txt", "kind": "metric", "raw": completed.stdout}]}
    if tool == "dockq":
        output = work / "dockq.json"
        mapping = payload["chain_map"]
        command = ["DockQ", str(structures[0]), str(structures[1]), "--json", str(output),
                   "--mapping", "".join(mapping) + ":" + "".join(mapping.values()), "--n_cpu", "1"]
        completed = _run(command, cwd=work)
        raw = output.read_bytes()
        return {"metrics": parse_dockq(raw, mapping), "artifacts": [
            {"name": "dockq.json", "kind": "metric", "raw": raw},
            {"name": "dockq.log", "kind": "log", "raw": completed.stdout}]}
    if tool == "ipsae":
        pdb = work / "model.pdb"
        residue_count = _as_pdb(structures[0], pdb)
        if len(payload["pae"]) != residue_count:
            raise DesignError("INVALID_REQUEST", "ipSAE PAE dimensions do not match the modeled residues.")
        pae = work / "pae.json"
        pae.write_bytes(json_bytes({"pae": payload["pae"]}))
        pae_cutoff, distance_cutoff = payload["pae_cutoff"], payload["distance_cutoff"]
        _run([sys.executable, "/opt/ipsae/ipsae.py", str(pae), str(pdb),
              str(pae_cutoff), str(distance_cutoff)], cwd=work)
        suffix = f"_{int(pae_cutoff):02d}_{int(distance_cutoff):02d}.txt"
        outputs = [path for path in work.glob("model_*.txt") if not path.name.endswith("_byres.txt")]
        if len(outputs) != 1 or not outputs[0].name.endswith(suffix):
            raise DesignError("OUTPUT_INVALID", "ipSAE did not publish its expected score table.")
        raw = outputs[0].read_bytes()
        return {"metrics": parse_ipsae(raw, expected_pae_cutoff=pae_cutoff,
                                        expected_distance_cutoff=distance_cutoff), "artifacts": [
            {"name": "ipsae.txt", "kind": "metric", "raw": raw},
            {"name": "ipsae-by-residue.txt", "kind": "metric",
             "raw": outputs[0].with_name(outputs[0].stem + "_byres.txt").read_bytes()}]}
    if tool == "mmseqs2":
        prefix, manifest, manifest_digest = _search_database(tool, payload["database"], databases)
        query = work / "query.fasta"
        query.write_text("".join(f">query_{index:04d}\n{sequence}\n"
                                 for index, sequence in enumerate(payload["sequences"], 1)), encoding="ascii")
        output, temporary = work / "hits.tsv", work / "tmp"
        completed = _run(["mmseqs", "easy-search", str(query), str(prefix), str(output), str(temporary),
                          "--threads", "1", "--max-seqs", str(payload["max_hits"]),
                          "--format-output", "query,target,evalue,bits,fident,qcov,tcov"], cwd=work)
        raw = output.read_bytes()
        return {"metrics": {"database": manifest["identity"], "database_release": manifest["release"],
                            "database_manifest_sha256": manifest_digest,
                            "hits": _parse_search_tsv(raw, foldseek=False, max_hits=payload["max_hits"])},
                "artifacts": [{"name": "mmseqs2-hits.tsv", "kind": "metric", "raw": raw},
                              {"name": "mmseqs2.log", "kind": "log", "raw": completed.stdout}]}
    if tool == "foldseek":
        prefix, manifest, manifest_digest = _search_database(tool, payload["database"], databases)
        output, temporary = work / "hits.tsv", work / "tmp"
        completed = _run(["foldseek", "easy-search", str(structures[0]), str(prefix), str(output),
                          str(temporary), "--threads", "1", "--max-seqs", str(payload["max_hits"]),
                          "--format-output",
                          "query,target,evalue,bits,fident,alntmscore,qtmscore,ttmscore"], cwd=work)
        raw = output.read_bytes()
        return {"metrics": {"database": manifest["identity"], "database_release": manifest["release"],
                            "database_manifest_sha256": manifest_digest,
                            "hits": _parse_search_tsv(raw, foldseek=True, max_hits=payload["max_hits"])},
                "artifacts": [{"name": "foldseek-hits.tsv", "kind": "metric", "raw": raw},
                              {"name": "foldseek.log", "kind": "log", "raw": completed.stdout}]}
    raise DesignError("TOOL_UNAVAILABLE", "Utility adapter is not implemented.")
