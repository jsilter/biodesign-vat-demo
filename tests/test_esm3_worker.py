"""ESM3's public checkpoint stays local, deterministic and bound to validated artifacts."""

import os
import sys
from types import SimpleNamespace

import pytest

from runner.design_inputs import DesignError
from runner.design_workers import run_esm3
from runner.tool_contracts import tool_request


def _install_fake_esm(monkeypatch, generated):
    loads = []

    class Model:
        def eval(self):
            return self

        def generate(self, protein, config):
            return generated(protein, config)

    class ESM3:
        @staticmethod
        def from_pretrained(name, device):
            loads.append((name, str(device)))
            return Model()

    class ESMProtein:
        def __init__(self, sequence, *, coordinates=None, plddt=None, pdb=""):
            self.sequence = sequence
            self.coordinates = coordinates
            self.plddt = plddt
            self._pdb = pdb

        def to_pdb_string(self):
            return self._pdb

    class ESMProteinError(Exception):
        pass

    class GenerationConfig:
        def __init__(self, **values):
            self.__dict__.update(values)

    monkeypatch.setitem(sys.modules, "esm.models.esm3", SimpleNamespace(ESM3=ESM3))
    monkeypatch.setitem(sys.modules, "esm.sdk.api", SimpleNamespace(
        ESMProtein=ESMProtein, ESMProteinError=ESMProteinError,
        GenerationConfig=GenerationConfig))
    return ESMProtein, loads


def test_esm3_sequence_generation_retains_duplicate_samples_with_distinct_seeds(tmp_path, monkeypatch):
    seen = []
    protein_type = None

    def generate(protein, config):
        seen.append((protein.sequence, config.__dict__))
        return protein_type(protein.sequence.replace("_", "C"))

    protein_type, loads = _install_fake_esm(monkeypatch, generate)
    (tmp_path / "weights/esm3/data/weights").mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    previous = os.environ.pop("INFRA_PROVIDER", None)
    try:
        result = run_esm3({"sequence": "A_", "task": "generate_sequence", "count": 2,
                           "seed": 7, "temperature": 0.7}, tmp_path / "weights", work)
        assert "INFRA_PROVIDER" not in os.environ
    finally:
        if previous is not None:
            os.environ["INFRA_PROVIDER"] = previous
    assert loads == [("esm3-sm-open-v1", "cuda")]
    assert seen == [("A_", {"track": "sequence", "num_steps": 2, "temperature": 0.7})] * 2
    assert [sample["seed"] for sample in result["metrics"]["samples"]] == [7, 8]
    assert [artifact["name"] for artifact in result["artifacts"]] == ["sample-1.fasta", "sample-2.fasta"]
    assert [artifact["raw"] for artifact in result["artifacts"]] == [b">esm3-sample-1\nAC\n",
                                                                       b">esm3-sample-2\nAC\n"]


def test_esm3_structure_generation_validates_sequence_and_reports_raw_confidence(tmp_path, monkeypatch):
    torch = pytest.importorskip("torch")
    pdb = ("ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00 60.00           C\n"
           "ATOM      2  CA  CYS A   2       1.000   0.000   0.000  1.00 80.00           C\n"
           "TER\nEND\n")
    protein_type = None

    def generate(protein, config):
        return protein_type(protein.sequence, coordinates=torch.zeros((2, 37, 3)),
                            plddt=torch.tensor([0.6, 0.8]), pdb=pdb)

    protein_type, _ = _install_fake_esm(monkeypatch, generate)
    (tmp_path / "weights/esm3/data/weights").mkdir(parents=True)
    (tmp_path / "work").mkdir()
    result = run_esm3({"sequence": "AC", "task": "generate_structure", "count": 1,
                       "seed": 3, "temperature": 1.0}, tmp_path / "weights", tmp_path / "work")
    assert result["metrics"]["samples"] == [{"sample": 1, "seed": 3,
                                               "mean_plddt_raw_0_1": pytest.approx(0.7)}]
    assert result["artifacts"] == [{"name": "sample-1.pdb", "kind": "coordinates",
                                     "raw": pdb.encode()}]


@pytest.mark.parametrize("payload", [
    {"sequence": "AC", "task": "score", "count": 1, "seed": 1, "temperature": 1.0},
    {"sequence": "AC", "task": "generate_sequence", "count": 1, "seed": 1, "temperature": 1.0},
    {"sequence": "A_", "task": "generate_structure", "count": 1, "seed": 1, "temperature": 1.0},
])
def test_esm3_contract_rejects_undefined_or_incoherent_operations(payload):
    with pytest.raises(DesignError) as error:
        tool_request("esm3", payload)
    assert error.value.code == "INVALID_REQUEST"
