"""Representative real-format parsing for the CPU utility workers."""

import json
import hashlib
from types import SimpleNamespace

import pytest

from runner.design_inputs import DesignError
from runner.design_store import json_bytes
from runner.suite_workers import (parse_dockq, parse_dssp, parse_ipsae, parse_tmalign,
                                  build_search_database_manifest, run_utility)


def dssp_row(index=1, residue="1", chain="A", amino_acid="A", code="H"):
    line = [" "] * 80
    line[0:5] = f"{index:5d}"
    line[5:11] = f"{residue:>6}"
    line[11], line[13], line[16] = chain, amino_acid, code
    return "".join(line)


def test_dssp_parser_preserves_residue_mapping_and_rejects_duplicates():
    header = "HEADER\n  #  RESIDUE AA STRUCTURE\n"
    raw = (header + dssp_row() + "\n" + dssp_row(2, "2", "A", "C", " ") + "\n").encode()
    assert parse_dssp(raw) == [
        {"index": 1, "chain": "A", "residue": "1", "amino_acid": "A", "secondary_structure": "H"},
        {"index": 2, "chain": "A", "residue": "2", "amino_acid": "C", "secondary_structure": " "},
    ]
    with pytest.raises(DesignError):
        parse_dssp((header + dssp_row() + "\n" + dssp_row(2) + "\n").encode())


def test_tmalign_parser_retains_both_normalizations_and_checks_alignment_width():
    raw = (b"""Length of Chain_1: 10 residues
Length of Chain_2: 12 residues
Aligned length= 8, RMSD= 1.25, Seq_ID=n_identical/n_aligned= 0.375
TM-score= 0.70000 (if normalized by length of Chain_1, i.e., LN=10, d0=1.0)
TM-score= 0.60000 (if normalized by length of Chain_2, i.e., LN=12, d0=1.2)
(\":\" denotes residue pairs of d <  5.0 Angstrom, \".\" denotes other aligned residues)
ACDEFGHI--
""" + b"::::....  \n" + b"""
ACDDFGHIKL
""")
    metrics = parse_tmalign(raw)
    assert metrics["tm_score_normalized_chain_1"] == .7
    assert metrics["tm_score_normalized_chain_2"] == .6
    assert metrics["aligned_length"] == 8 and metrics["rmsd_angstrom"] == 1.25
    with pytest.raises(DesignError):
        parse_tmalign(raw.replace(b"0.60000", b"nan"))


def test_dockq_parser_validates_requested_mapping_and_metric_ranges():
    raw = json.dumps({"GlobalDockQ": .653, "best_mapping": {"A": "B", "B": "A"},
        "best_result": {"A-B": {"DockQ": .994, "iRMSD": 0.0, "LRMSD": 0.0,
                                  "fnat": .983, "fnonnat": .008, "F1": .987, "clashes": 0}}}).encode()
    result = parse_dockq(raw, {"A": "B", "B": "A"})
    assert result["global_dockq"] == .653 and result["interfaces"][0]["metrics"]["DockQ"] == .994
    with pytest.raises(DesignError):
        parse_dockq(raw, {"A": "A", "B": "B"})
    with pytest.raises(DesignError):
        parse_dockq(raw.replace(b"0.994", b"1.994"), {"A": "B", "B": "A"})


def ipsae_row(chain1, chain2, score):
    return (f"{chain1} {chain2} 10 15 asym {score:.6f} {score:.6f} {score:.6f} 0.500 "
            f"{score:.6f} 0.4000 0.3000 0.2000 5 20 10 1.00 2.00 1.50 10 10 5 5 model")


def test_ipsae_parser_keeps_directions_and_derives_minimum_independently():
    raw = ("heading\n" + ipsae_row("A", "B", .8) + "\n" + ipsae_row("B", "A", .6) + "\n").encode()
    metrics = parse_ipsae(raw)
    assert [row["ipsae"] for row in metrics["directional"]] == [.8, .6]
    assert metrics["pairs"] == [{"chains": ["A", "B"], "minimum_ipsae": .6,
                                  "forward_ipsae": .8, "reverse_ipsae": .6}]
    with pytest.raises(DesignError):
        parse_ipsae(("heading\n" + ipsae_row("A", "B", .8) + "\n").encode())
    with pytest.raises(DesignError, match="cutoffs"):
        parse_ipsae(raw, expected_pae_cutoff=10, expected_distance_cutoff=20)


def test_cpu_workers_construct_fixed_commands_and_publish_raw_outputs(tmp_path, monkeypatch):
    from runner import suite_workers

    commands = []
    def fake_run(command, *, cwd, timeout=300):
        commands.append(command)
        if command[0] == "mkdssp":
            (cwd / "assignments.dssp").write_bytes(
                ("  #  RESIDUE AA STRUCTURE\n" + dssp_row() + "\n").encode())
            return SimpleNamespace(stdout=b"", stderr=b"", returncode=0)
        raise AssertionError(command)
    monkeypatch.setattr(suite_workers, "_run", fake_run)
    result = run_utility("dssp", {"structures": ["data_test\n#\n"]}, tmp_path)
    assert commands == [["mkdssp", "--output-format=dssp", str(tmp_path / "structure-1.cif"),
                         str(tmp_path / "assignments.dssp")]]
    assert result["metrics"]["assignments"][0]["chain"] == "A"
    assert result["artifacts"][0]["raw"].startswith(b"  #")


def stage_database(root, tool="mmseqs2", identity="uniref-test"):
    directory = root / identity
    directory.mkdir()
    files = {"database": b"index", "database.dbtype": b"\x00\x00\x00\x00"}
    for name, raw in files.items():
        (directory / name).write_bytes(raw)
    manifest = {"schema": "protein-search-database/1", "tool": tool, "identity": identity,
                "release": "fixture-2026-09", "prefix": "database", "files": [
                    {"path": name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes_len": len(raw)}
                    for name, raw in files.items()]}
    (directory / "manifest.json").write_bytes(json_bytes(manifest))
    return manifest


def test_search_database_stager_hashes_exact_flat_files(tmp_path):
    (tmp_path / "database").write_bytes(b"index")
    (tmp_path / "database.dbtype").write_bytes(b"type")
    manifest = build_search_database_manifest(
        "mmseqs2", "uniref90-2026-03", "UniRef90 2026_03", "database", tmp_path)
    assert manifest["identity"] == "uniref90-2026-03"
    assert [entry["path"] for entry in manifest["files"]] == ["database", "database.dbtype"]
    (tmp_path / "nested").mkdir()
    with pytest.raises(DesignError, match="flat regular files"):
        build_search_database_manifest(
            "mmseqs2", "uniref90-2026-03", "UniRef90 2026_03", "database", tmp_path)


def test_mmseqs_worker_authenticates_database_constructs_fixed_command_and_parses_hits(tmp_path, monkeypatch):
    from runner import suite_workers

    databases, work = tmp_path / "databases", tmp_path / "work"
    databases.mkdir(); work.mkdir()
    manifest = stage_database(databases)
    seen = []

    def fake_run(command, *, cwd, timeout=300):
        seen.append(command)
        (cwd / "hits.tsv").write_bytes(b"query_0001\tP12345\t1e-20\t90.5\t0.75\t0.8\t0.9\n")
        return SimpleNamespace(stdout=b"searched\n", stderr=b"", returncode=0)

    monkeypatch.setattr(suite_workers, "_run", fake_run)
    result = run_utility("mmseqs2", {"database": "uniref-test", "sequences": ["ACDE"],
                                      "max_hits": 2}, work, databases=databases)
    assert seen[0][0:2] == ["mmseqs", "easy-search"]
    assert "--threads" in seen[0] and seen[0][seen[0].index("--threads") + 1] == "1"
    assert result["metrics"]["database_release"] == manifest["release"]
    assert result["metrics"]["hits"][0]["target"] == "P12345"
    assert result["metrics"]["hits"][0]["fident"] == .75
    (databases / "uniref-test" / "database").write_bytes(b"tampered")
    with pytest.raises(DesignError, match="integrity"):
        run_utility("mmseqs2", {"database": "uniref-test", "sequences": ["ACDE"],
                                  "max_hits": 2}, work, databases=databases)


def test_foldseek_worker_parses_structural_scores_and_rejects_nonfinite_output(tmp_path, monkeypatch):
    from runner import suite_workers

    databases, work = tmp_path / "databases", tmp_path / "work"
    databases.mkdir(); work.mkdir()
    stage_database(databases, tool="foldseek", identity="pdb-test")

    def fake_run(command, *, cwd, timeout=300):
        (cwd / "hits.tsv").write_bytes(b"structure-1\t1abc_A\t1e-8\t70\t0.4\t0.6\t0.7\t0.5\n")
        return SimpleNamespace(stdout=b"searched\n", stderr=b"", returncode=0)

    monkeypatch.setattr(suite_workers, "_run", fake_run)
    result = run_utility("foldseek", {"database": "pdb-test", "structures": ["data_x\n#\n"],
                                      "max_hits": 1}, work, databases=databases)
    assert result["metrics"]["hits"][0]["qtmscore"] == .7

    def nonfinite(command, *, cwd, timeout=300):
        (cwd / "hits.tsv").write_bytes(b"structure-1\t1abc_A\t1e-8\t70\t0.4\tnan\t0.7\t0.5\n")
        return SimpleNamespace(stdout=b"", stderr=b"", returncode=0)

    monkeypatch.setattr(suite_workers, "_run", nonfinite)
    with pytest.raises(DesignError):
        run_utility("foldseek", {"database": "pdb-test", "structures": ["data_x\n#\n"],
                                   "max_hits": 1}, work, databases=databases)
