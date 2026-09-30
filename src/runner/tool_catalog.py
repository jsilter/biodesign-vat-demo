"""Immutable inventory and deployment identity for standalone scientific tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ToolFamily = Literal["generation", "sequence", "prediction", "utility"]
LicenceClass = Literal["open_source", "noncommercial", "mixed"]
Readiness = Literal["offline_ready", "deployed", "validated", "blocked"]

CORE_APP = "biodesign-vat-tools"
SUITE_APP = "biodesign-vat-tool-suite"
KIT_APP = "biodesign-vat-anthropic-kits"
ANTHROPIC_KIT_REVISION = "f4f62fa6592ae4938d49b1757bea0cfeff9f468e"
ANTHROPIC_KIT_SOURCE = "anthropics/uplifting-biomolecular-modeling"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One stable public tool identity and its bounded deployment requirements."""

    id: str
    name: str
    family: ToolFamily
    operation: str
    input_role: str
    source: str
    revision: str
    licence: LicenceClass
    app_name: str
    function_name: str
    lock: str
    assets: tuple[str, ...]
    hardware: str
    readiness: Readiness
    implemented: bool
    blocker: str | None = None
    kit: str | None = None
    upstream: str | None = None

    def __post_init__(self) -> None:
        if not self.id or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in self.id):
            raise ValueError(f"Invalid tool ID: {self.id!r}")
        if not self.function_name or self.function_name != self.id:
            raise ValueError(f"Function identity must equal stable tool ID: {self.id}")
        if self.readiness == "blocked" and not self.blocker:
            raise ValueError(f"Blocked tool lacks a reason: {self.id}")
        if self.readiness != "blocked" and self.blocker:
            raise ValueError(f"Ready tool carries a blocker: {self.id}")
        if not self.implemented and self.readiness != "blocked":
            raise ValueError(f"Inventory-only tool cannot be advertised ready: {self.id}")
        if self.kit and self.revision != ANTHROPIC_KIT_REVISION:
            raise ValueError(f"Anthropic kit is not pinned to the suite release: {self.id}")
        if self.kit and (self.source != ANTHROPIC_KIT_SOURCE or not self.upstream):
            raise ValueError(f"Anthropic kit lacks separate kit and upstream identities: {self.id}")

    def public(self) -> dict:
        """Return non-secret catalog metadata suitable for the loopback API."""
        return {
            "id": self.id,
            "name": self.name,
            "family": self.family,
            "operation": self.operation,
            "standalone": True,
            "input_role": self.input_role,
            "licence": self.licence,
            "readiness": self.readiness,
            "implemented": self.implemented,
            "required_assets": list(self.assets),
            **({"blocker": self.blocker} if self.blocker else {}),
        }


def _existing(id: str, name: str, family: ToolFamily, operation: str, role: str,
              source: str, revision: str, lock: str, assets: tuple[str, ...],
              hardware: str, readiness: Readiness) -> ToolSpec:
    return ToolSpec(id, name, family, operation, role, source, revision, "open_source",
                    CORE_APP, id, lock, assets, hardware, readiness, True)


def _kit(id: str, name: str, family: ToolFamily, operation: str, role: str, kit: str,
         source: str, licence: LicenceClass, lock: str, assets: tuple[str, ...],
         hardware: str) -> ToolSpec:
    return ToolSpec(id, name, family, operation, role, ANTHROPIC_KIT_SOURCE,
                    ANTHROPIC_KIT_REVISION, licence, KIT_APP, id, lock, assets, hardware,
                    "offline_ready", True, None, kit, source)


def _direct(id: str, name: str, family: ToolFamily, operation: str, role: str,
            source: str, revision: str, licence: LicenceClass, lock: str,
            assets: tuple[str, ...], hardware: str, blocker: str | None,
            *, implemented: bool | None = None) -> ToolSpec:
    implemented = blocker is None if implemented is None else implemented
    return ToolSpec(id, name, family, operation, role, source, revision, licence,
                    SUITE_APP, id, lock, assets, hardware,
                    "blocked" if blocker else "offline_ready", implemented, blocker)


TOOLS = (
    _kit("pxdesign", "PXDesign", "generation", "design", "target structure → candidate binders",
         "pxdesign", "bytedance/PXDesign", "open_source", "pxdesign.txt", ("pxdesign",), "H100"),
    _kit("rfdiffusion3", "RFdiffusion3", "generation", "design", "target structure → candidate binders",
         "rfdiffusion3", "RosettaCommons/foundry", "open_source", "rfdiffusion3.txt", ("rfdiffusion3",), "H100"),
    _kit("genie3", "Genie 3", "generation", "design", "constraints → candidate structures",
         "genie3", "aqlaboratory/genie3", "open_source", "genie3.txt", ("genie3",), "H100"),
    _direct("freebindcraft", "FreeBindCraft", "generation", "design", "target structure → candidate binders",
            "cytokineking/FreeBindCraft", "8b8d4c4627da06f084f46b7007450048fe3ca22f", "open_source",
            "freebindcraft.txt", ("freebindcraft",), "H100",
            "Upstream resolves mutable dependencies and downloads weights during installation; no immutable runtime or asset manifest is published."),
    _existing("boltzgen", "BoltzGen", "generation", "design", "target structure → candidate binders",
              "HannesStark/boltzgen", "a3149cf18eeb58648d1abbb27539bd73f746cdda", "boltz.txt",
              ("boltzgen", "mols"), "H100", "offline_ready"),
    _kit("rfdiffusion", "RFdiffusion", "generation", "design", "target structure → candidate binders",
         "rfdiffusion1", "RosettaCommons/RFdiffusion", "open_source", "rfdiffusion.txt", ("rfdiffusion",), "H100"),
    _kit("proteina-complexa", "Proteina-Complexa", "generation", "design", "target complex → candidate complexes",
         "complexa", "NVIDIA-BioNeMo/Proteina-Complexa", "mixed", "complexa.txt", ("complexa",), "H100"),
    _direct("foldcraft", "FoldCraft", "generation", "design", "target structure → candidate binders",
            "KhondamirRustamov/FoldCraft", "535ad10a9643c8ae64c52d17d603b4ac96ea305f", "open_source",
            "foldcraft.txt", ("foldcraft",), "H100", "Upstream publishes no inference dependency lock or checkpoint manifest."),
    _direct("boltzdesign1", "BoltzDesign1", "generation", "design", "target structure → candidate binders",
            "yehlincho/BoltzDesign1", "31aeedd7f187ba88dd9e92514070aee13381cb93", "open_source",
            "boltzdesign1.txt", ("boltzdesign1",), "H100", "Upstream setup resolves unpinned dependencies and weights without an immutable asset manifest."),
    _direct("protein-hunter", "Protein Hunter", "generation", "design", "target structure → candidate binders",
            "yehlincho/Protein-Hunter", "a3e048e96c93953f022d15a4d35cd744b7e39e41", "open_source",
            "protein-hunter.txt", ("protein-hunter",), "H100", "Upstream supplies no reproducibly locked predictor stack or weight manifest."),
    _kit("mosaic", "Mosaic", "generation", "design", "target structure → candidate binders",
         "mosaic", "escalante-bio/mosaic", "open_source", "mosaic.txt", ("mosaic",), "H100"),
    _direct("halludesign", "HalluDesign", "generation", "design", "target structure → candidate binders",
            "MinchaoFang/HalluDesign", "6a5d62c99a565531e58afaf2453d520ecfda1c1a", "open_source",
            "halludesign.txt", ("halludesign", "esmfold2-full"), "H100", "The published container depends on gated native AlphaFold3 and provides no pinned open predictor stack."),

    _direct("proteinmpnn-soluble", "ProteinMPNN soluble v_48_020", "sequence", "inverse_fold",
            "protein backbone → designed sequences", "dauparas/ProteinMPNN",
            "8907e6671bfbfc92303b5f79c4b5e6ce47cdef57", "open_source", "proteinmpnn.txt",
            ("proteinmpnn-soluble",), "L4", None),
    _existing("proteinmpnn", "ProteinMPNN v_48_020", "sequence", "inverse_fold",
              "protein backbone → designed sequences", "dauparas/ProteinMPNN",
              "8907e6671bfbfc92303b5f79c4b5e6ce47cdef57", "proteinmpnn.txt", ("proteinmpnn",),
              "L4", "offline_ready"),
    _kit("soluble-caliby", "SolubleCaliby", "sequence", "inverse_fold", "protein backbone ensemble → designed sequences",
         "caliby", "ProteinDesignLab/caliby", "open_source", "caliby.txt", ("soluble-caliby",), "H100"),
    _kit("caliby", "Caliby", "sequence", "inverse_fold", "protein backbone ensemble → designed sequences",
         "caliby", "ProteinDesignLab/caliby", "open_source", "caliby.txt", ("caliby",), "H100"),
    _existing("esmc", "ESMC-300M", "sequence", "score", "sequence → masked plausibility score",
              "Biohub/esm", "bf343ba264b650dff7a073643725f9aaa1fdbe8d", "esm.txt", ("esmc",),
              "L4", "offline_ready"),
    _direct("esm3", "ESM3 small open", "sequence", "generate", "sequence/structure tracks → generated track",
            "Biohub/esm", "43b4548b86762edfa747b07d5f440aad3c33acee", "open_source", "esm3.txt",
            ("esm3-sm-open-v1",), "L4", None),

    _kit("esmfold2-full", "ESMFold2 Full", "prediction", "predict", "sequence/MSA → predicted structure",
         "esmfold2", "biohub/esm", "open_source", "esmfold2-full.txt", ("esmfold2-full",), "H100"),
    _existing("esmfold2", "ESMFold2-Fast", "prediction", "predict", "sequence → predicted structure",
              "biohub/ESMFold2-Fast", "93a69ea81c2c1fa593292b385c62d5f5b19893f9", "esm.txt",
              ("esmfold2",), "A100-40GB", "validated"),
    _kit("protenix-v2", "Protenix v2", "prediction", "predict", "complex/MSA → predicted structure",
         "protenix_v2", "bytedance/Protenix", "open_source", "protenix-v2.txt", ("protenix-v2",), "H100"),
    _kit("colabfold-afmultimer-v3", "ColabFold AlphaFold-Multimer v3", "prediction", "predict",
         "complex/MSA → predicted structure", "colabfold", "sokrypton/ColabFold", "open_source",
         "colabfold.txt", ("af2-multimer-v3",), "H100"),
    _kit("af3-openfold3", "AlphaFold3 architecture with OpenFold3 weights", "prediction", "predict",
         "complex features → predicted structure", "af3_torch", "sokrypton/alphafold3", "open_source",
         "af3-openfold3.txt", ("openfold3-openbind0",), "H100"),
    _direct("alphafold3", "AlphaFold 3", "prediction", "predict", "complex features → predicted structure",
            "google-deepmind/alphafold3", "c0f97eda2f1f482fd94d3a38bece18c7069b4a5c", "noncommercial",
            "alphafold3.txt", ("alphafold3-parameters", "alphafold-databases", "ccd"), "H100",
            "Owner-supplied organization-specific parameter grant and staged parameters are required."),
    _kit("chai1", "Chai-1", "prediction", "predict", "complex/MSA → predicted structure",
         "chai1", "chaidiscovery/chai-lab", "open_source", "chai1.txt", ("chai1",), "H100"),
    _direct("boltz1", "Boltz-1", "prediction", "predict", "complex/MSA → predicted structure",
            "jwohlwend/boltz", "cb04aeccdd480fd4db707f0bbafde538397fa2ac", "open_source", "boltz1.txt", ("boltz1", "ccd"), "H100",
            "Superseded by the implemented Boltz-2 predictor: the pinned Anthropic kit carries Boltz-2 assets only and no reviewed Boltz-1 checkpoint manifest."),
    _kit("boltz2", "Boltz-2", "prediction", "predict", "complex/MSA → predicted structure",
         "boltz2", "jwohlwend/boltz", "open_source", "boltz2.txt", ("boltz2",), "H100"),
    _kit("af-unmasked", "AF_unmasked", "prediction", "predict", "template/sequence features → diagnostic structure",
         "af2ig", "sokrypton/ColabDesign", "open_source", "af-unmasked.txt", ("af2",), "H100"),
    _kit("openfold3", "OpenFold3", "prediction", "predict", "complex features → predicted structure",
         "openfold3", "aqlaboratory/openfold-3", "open_source", "openfold3.txt", ("openfold3",), "H100"),
    _kit("rosettafold3", "RoseTTAFold3", "prediction", "predict", "complex/MSA → predicted structure",
         "rosettafold3", "RosettaCommons/RoseTTAFold3", "open_source", "rosettafold3.txt", ("rosettafold3",), "H100"),
    _kit("opendde", "OpenDDE", "prediction", "predict", "complex/MSA → predicted structure",
         "opendde", "aurekaresearch/OpenDDE", "open_source", "opendde.txt", ("opendde",), "H100"),

    _direct("dssp", "DSSP", "utility", "annotate", "structure → residue secondary structure",
            "PDB-REDO/dssp", "ca067979028bccbdafa6ed70d08ded4b1414c6ff", "open_source", "suite-cpu.txt", (), "CPU",
            None),
    _direct("ipsae", "ipSAE", "utility", "score", "structure and PAE → directional interface scores",
            "DunbrackLab/IPSAE", "6174cf9e71cb1bd660cc805856a18c4871a6dec3", "open_source", "suite-cpu.txt", (), "CPU",
            None),
    _direct("dockq", "DockQ", "utility", "score", "model/reference complexes → DockQ score",
            "wallnerlab/DockQ", "d9cbb1940bb0f42db3257f7da3b0e96f162b94d9", "open_source", "suite-cpu.txt", (), "CPU",
            None),
    _direct("mmseqs2", "MMseqs2", "utility", "search", "sequences → similarity hits",
            "soedinglab/MMseqs2", "18-8cc5c", "open_source", "mmseqs2.txt", ("mmseqs2-database",), "CPU",
            "A release-identified private search database is required.", implemented=True),
    _direct("tmalign", "TM-align", "utility", "align", "two structures → structural alignment",
            "zhanggroup/TM-align", "sha256:c68bb4ddc4f6162b2aa53fafb53ed8007eb3f24c21e62ced18a93604742fb013", "open_source", "suite-cpu.txt", (), "CPU",
            None),
    _direct("foldseek", "Foldseek", "utility", "search", "structures → structural similarity hits",
            "steineggerlab/foldseek", "10-941cd33", "open_source", "foldseek.txt", ("foldseek-database",), "CPU",
            "A release-identified private search database is required.", implemented=True),
    _direct("rosetta-relax", "Rosetta relax", "utility", "relax", "structure → relaxed structure and scores",
            "RosettaCommons/rosetta", "2025.12", "noncommercial", "rosetta.txt", ("rosetta",), "CPU",
            "Owner-supplied non-commercial Rosetta distribution and licence manifest are required."),
    _direct("pyrosetta-interface", "PyRosetta interface analysis", "utility", "score",
            "complex → bounded interface metrics", "RosettaCommons/rosetta", "2025.12", "noncommercial",
            "pyrosetta.txt", ("pyrosetta",), "CPU",
            "Owner-supplied non-commercial PyRosetta distribution and concurrent Rosetta licence are required."),
)

TOOL_BY_ID = {tool.id: tool for tool in TOOLS}
if len(TOOL_BY_ID) != len(TOOLS) or len({(t.app_name, t.function_name) for t in TOOLS}) != len(TOOLS):
    raise RuntimeError("Scientific tool catalog contains duplicate identities.")
if len(TOOLS) != 39:
    raise RuntimeError(f"Scientific tool catalog must contain 39 entries, found {len(TOOLS)}.")

CORE_TOOLS = frozenset({"boltzgen", "proteinmpnn", "esmfold2", "esmc"})


def tool_spec(tool_id: str) -> ToolSpec:
    try:
        return TOOL_BY_ID[tool_id]
    except KeyError as exc:
        raise ValueError("Unknown scientific tool.") from exc


def public_catalog() -> list[dict]:
    return [tool.public() for tool in TOOLS]
