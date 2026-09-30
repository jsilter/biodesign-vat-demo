"""The tool catalog has one immutable source of deployment identities."""

from runner.tool_catalog import (ANTHROPIC_KIT_REVISION, ANTHROPIC_KIT_SOURCE, CORE_TOOLS,
                                 KIT_APP, TOOLS, TOOL_BY_ID, public_catalog, tool_spec)
from runner.kit_workers import KIT_TOOL_IDS
from runner.suite_executor import IMPLEMENTED_SUITE_TOOLS


def test_catalog_covers_exact_tool_set_without_identity_aliases():
    expected = {
        "pxdesign", "rfdiffusion3", "genie3", "freebindcraft", "boltzgen", "rfdiffusion",
        "proteina-complexa", "foldcraft", "boltzdesign1", "protein-hunter", "mosaic",
        "halludesign", "proteinmpnn-soluble", "proteinmpnn", "soluble-caliby", "caliby",
        "esmc", "esm3", "esmfold2-full", "esmfold2", "protenix-v2",
        "colabfold-afmultimer-v3", "af3-openfold3", "alphafold3", "chai1", "boltz1",
        "boltz2", "af-unmasked", "openfold3", "rosettafold3", "opendde", "dssp", "ipsae",
        "dockq", "mmseqs2", "tmalign", "foldseek", "rosetta-relax", "pyrosetta-interface",
    }
    assert len(TOOLS) == len(TOOL_BY_ID) == 39
    assert set(TOOL_BY_ID) == expected
    assert len({(tool.app_name, tool.function_name) for tool in TOOLS}) == 39
    assert all(tool.function_name == tool.id for tool in TOOLS)


def test_catalog_keeps_core_scope_narrow_and_reports_readiness_honestly():
    assert CORE_TOOLS == {"boltzgen", "proteinmpnn", "esmfold2", "esmc"}
    assert tool_spec("esmfold2").readiness == "validated"
    assert tool_spec("esm3").readiness == "offline_ready"
    assert all(tool_spec(name).readiness in {"offline_ready", "validated"} for name in CORE_TOOLS)
    assert all(tool.blocker for tool in TOOLS if tool.readiness == "blocked")
    assert all(tool.revision == ANTHROPIC_KIT_REVISION for tool in TOOLS if tool.kit)
    assert all(tool.source == ANTHROPIC_KIT_SOURCE and tool.upstream
               for tool in TOOLS if tool.app_name == KIT_APP)
    assert {tool.id for tool in TOOLS if tool.implemented} == CORE_TOOLS | IMPLEMENTED_SUITE_TOOLS
    assert all(tool_spec(tool).implemented for tool in KIT_TOOL_IDS)


def test_public_catalog_contains_no_source_locations_or_private_asset_paths():
    catalog = public_catalog()
    assert [entry["id"] for entry in catalog] == [tool.id for tool in TOOLS]
    assert all(entry["standalone"] is True and entry["input_role"] for entry in catalog)
    assert sum(entry["implemented"] for entry in catalog) == 30
    assert all("source" not in entry and "revision" not in entry and "app_name" not in entry
               for entry in catalog)
    assert next(item for item in catalog if item["id"] == "alphafold3")["licence"] == "noncommercial"
