"""Modal deployment for the pinned Anthropic protein-tool kits."""

from __future__ import annotations

import os
from pathlib import Path

import modal
from vatlog import vat

from .kit_workers import KIT_TOOL_IDS
from .kit_source import validate_source_manifest
from .suite_executor import execute_suite, recover_suite
from .tool_catalog import ANTHROPIC_KIT_REVISION, KIT_APP, tool_spec
from .vat_adapter import scientific_result_adapter, tool_identity

APP_NAME = KIT_APP
app = modal.App(APP_NAME)
jobs = modal.Volume.from_name("biodesign-vat-tool-suite-jobs", create_if_missing=True)
assets = modal.Volume.from_name("biodesign-vat-weights", create_if_missing=True)
recover_image = (modal.Image.micromamba(python_version="3.12")
                 .pip_install("pydantic==2.12.5", "cryptography>=39", "rfc8785>=0.1", "pymerkle>=6.1")
                 .add_local_python_source("runner", "vatlog"))

_source_root = Path(os.environ.get("VAT_ANTHROPIC_KIT_ROOT", ".local/vendor/uplifting-biomolecular-modeling")).resolve()
_source_kits = sorted({tool_spec(tool).kit for tool in KIT_TOOL_IDS})
validate_source_manifest(_source_root, _source_root.parent / "uplifting-biomolecular-modeling.json",
                         _source_kits)


def _kit_image(kit: str, lock_name: str) -> modal.Image:
    """Use the exact reviewed upstream Dockerfile when its pinned source checkout is staged."""
    root = _source_root
    dockerfile = root / kit / "environment" / "Dockerfile"
    requirements = root / kit / "environment" / "requirements.lock"
    pins = root / kit / "stock" / "PINS.json"
    if not dockerfile.is_file() or not pins.is_file():
        raise RuntimeError(
            f"Pinned Anthropic kit source is missing for {kit}; run scripts/stage_anthropic_kit_source.py "
            "and set VAT_ANTHROPIC_KIT_ROOT before importing this deployment module."
        )
    lock = requirements if requirements.is_file() else pins
    return (modal.Image.from_dockerfile(dockerfile, context_dir=root, ignore=[".git/**"])
            .pip_install("pydantic==2.12.5", "cryptography>=39", "rfc8785>=0.1", "pymerkle>=6.1",
                         "gemmi==0.7.5")
            .add_local_file(lock, f"/opt/vat-locks/{lock_name}", copy=True)
            .env({"VAT_KIT_REVISION": ANTHROPIC_KIT_REVISION, "PYTHONHASHSEED": "0"})
            .add_local_python_source("runner", "vatlog"))


_kit_images = {spec.kit: _kit_image(spec.kit, spec.lock)
               for tool in KIT_TOOL_IDS for spec in (tool_spec(tool),)}


def _kit_function(tool: str, id: str, request: dict) -> dict:
    return execute_suite(tool, id, request, volume=jobs)


@vat(descriptor=tool_identity("pxdesign", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(name="pxdesign", image=_kit_images["pxdesign"], gpu="H100", timeout=1800, retries=0,
              max_containers=1, block_network=True, volumes={"/jobs": jobs, "/weights": assets})
def pxdesign(id: str, request: dict) -> dict:
    return _kit_function("pxdesign", id, request)


@vat(descriptor=tool_identity("rfdiffusion3", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(name="rfdiffusion3", image=_kit_images["rfdiffusion3"], gpu="H100", timeout=1800, retries=0,
              max_containers=1, block_network=True, volumes={"/jobs": jobs, "/weights": assets})
def rfdiffusion3(id: str, request: dict) -> dict:
    return _kit_function("rfdiffusion3", id, request)


@vat(descriptor=tool_identity("genie3", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(name="genie3", image=_kit_images["genie3"], gpu="H100", timeout=1800, retries=0,
              max_containers=1, block_network=True, volumes={"/jobs": jobs, "/weights": assets})
def genie3(id: str, request: dict) -> dict:
    return _kit_function("genie3", id, request)


@vat(descriptor=tool_identity("rfdiffusion", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(name="rfdiffusion", image=_kit_images["rfdiffusion1"], gpu="H100", timeout=1800, retries=0,
              max_containers=1, block_network=True, volumes={"/jobs": jobs, "/weights": assets})
def rfdiffusion(id: str, request: dict) -> dict:
    return _kit_function("rfdiffusion", id, request)


@vat(descriptor=tool_identity("proteina-complexa", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(name="proteina-complexa", image=_kit_images["complexa"], gpu="H100", timeout=1800, retries=0,
              max_containers=1, block_network=True, volumes={"/jobs": jobs, "/weights": assets})
def proteina_complexa(id: str, request: dict) -> dict:
    return _kit_function("proteina-complexa", id, request)


@vat(descriptor=tool_identity("mosaic", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(name="mosaic", image=_kit_images["mosaic"], gpu="H100", timeout=1800, retries=0,
              max_containers=1, block_network=True, volumes={"/jobs": jobs, "/weights": assets})
def mosaic(id: str, request: dict) -> dict:
    return _kit_function("mosaic", id, request)


@vat(descriptor=tool_identity("caliby", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(name="caliby", image=_kit_images["caliby"], gpu="H100", timeout=1800, retries=0,
              max_containers=1, block_network=True, volumes={"/jobs": jobs, "/weights": assets})
def caliby(id: str, request: dict) -> dict:
    return _kit_function("caliby", id, request)


@vat(descriptor=tool_identity("soluble-caliby", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(name="soluble-caliby", image=_kit_images["caliby"], gpu="H100", timeout=1800, retries=0,
              max_containers=1, block_network=True, volumes={"/jobs": jobs, "/weights": assets})
def soluble_caliby(id: str, request: dict) -> dict:
    return _kit_function("soluble-caliby", id, request)


@vat(descriptor=tool_identity("esmfold2-full", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(name="esmfold2-full", image=_kit_images["esmfold2"], gpu="H100", timeout=1800, retries=0,
              max_containers=1, block_network=True, volumes={"/jobs": jobs, "/weights": assets})
def esmfold2_full(id: str, request: dict) -> dict:
    return _kit_function("esmfold2-full", id, request)


@vat(descriptor=tool_identity("protenix-v2", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(name="protenix-v2", image=_kit_images["protenix_v2"], gpu="H100", timeout=1800, retries=0,
              max_containers=1, block_network=True, volumes={"/jobs": jobs, "/weights": assets})
def protenix_v2(id: str, request: dict) -> dict:
    return _kit_function("protenix-v2", id, request)


@vat(descriptor=tool_identity("colabfold-afmultimer-v3", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(name="colabfold-afmultimer-v3", image=_kit_images["colabfold"], gpu="H100", timeout=1800,
              retries=0, max_containers=1, block_network=True, volumes={"/jobs": jobs, "/weights": assets})
def colabfold_afmultimer_v3(id: str, request: dict) -> dict:
    return _kit_function("colabfold-afmultimer-v3", id, request)


@vat(descriptor=tool_identity("af3-openfold3", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(name="af3-openfold3", image=_kit_images["af3_torch"], gpu="H100", timeout=1800, retries=0,
              max_containers=1, block_network=True, volumes={"/jobs": jobs, "/weights": assets})
def af3_openfold3(id: str, request: dict) -> dict:
    return _kit_function("af3-openfold3", id, request)


@vat(descriptor=tool_identity("chai1", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(name="chai1", image=_kit_images["chai1"], gpu="H100", timeout=1800, retries=0,
              max_containers=1, block_network=True, volumes={"/jobs": jobs, "/weights": assets})
def chai1(id: str, request: dict) -> dict:
    return _kit_function("chai1", id, request)


@vat(descriptor=tool_identity("boltz2", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(name="boltz2", image=_kit_images["boltz2"], gpu="H100", timeout=1800, retries=0,
              max_containers=1, block_network=True, volumes={"/jobs": jobs, "/weights": assets})
def boltz2(id: str, request: dict) -> dict:
    return _kit_function("boltz2", id, request)


@vat(descriptor=tool_identity("af-unmasked", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(name="af-unmasked", image=_kit_images["af2ig"], gpu="H100", timeout=1800, retries=0,
              max_containers=1, block_network=True, volumes={"/jobs": jobs, "/weights": assets})
def af_unmasked(id: str, request: dict) -> dict:
    return _kit_function("af-unmasked", id, request)


@vat(descriptor=tool_identity("openfold3", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(name="openfold3", image=_kit_images["openfold3"], gpu="H100", timeout=1800, retries=0,
              max_containers=1, block_network=True, volumes={"/jobs": jobs, "/weights": assets})
def openfold3(id: str, request: dict) -> dict:
    return _kit_function("openfold3", id, request)


@vat(descriptor=tool_identity("rosettafold3", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(name="rosettafold3", image=_kit_images["rosettafold3"], gpu="H100", timeout=1800, retries=0,
              max_containers=1, block_network=True, volumes={"/jobs": jobs, "/weights": assets})
def rosettafold3(id: str, request: dict) -> dict:
    return _kit_function("rosettafold3", id, request)


@vat(descriptor=tool_identity("opendde", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(name="opendde", image=_kit_images["opendde"], gpu="H100", timeout=1800, retries=0,
              max_containers=1, block_network=True, volumes={"/jobs": jobs, "/weights": assets})
def opendde(id: str, request: dict) -> dict:
    return _kit_function("opendde", id, request)


@app.function(image=recover_image, volumes={"/jobs": jobs})
def recover(id: str) -> dict:
    jobs.reload()
    return recover_suite(id)
