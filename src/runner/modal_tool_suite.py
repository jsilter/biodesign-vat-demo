"""Cataloged Modal deployment for the standalone protein-tool suite."""

from __future__ import annotations

from pathlib import Path

import modal
from vatlog import vat

from .suite_executor import execute_suite, recover_suite
from .vat_adapter import scientific_result_adapter, tool_identity

APP_NAME = "biodesign-vat-tool-suite"
app = modal.App(APP_NAME)
jobs = modal.Volume.from_name("biodesign-vat-tool-suite-jobs", create_if_missing=True)
assets = modal.Volume.from_name("biodesign-vat-weights", create_if_missing=True)
databases = modal.Volume.from_name("biodesign-vat-protein-databases", create_if_missing=True)
locks = Path("/opt/vat-locks") if Path("/opt/vat-locks").is_dir() else Path(__file__).with_name("locks")

base = (modal.Image.micromamba(python_version="3.12")
        .apt_install("curl", "g++", "git")
        .pip_install("pydantic==2.12.5", "cryptography>=39", "rfc8785>=0.1", "pymerkle>=6.1")
        .add_local_dir(str(locks), "/opt/vat-locks", copy=True))

cpu_image = (base
    .micromamba_install("dssp=4.6.1", channels=["conda-forge"])
    .pip_install_from_requirements(str(locks / "suite-cpu.txt"))
    .run_commands(
        "mkdir -p /opt/tmalign && curl -fsSL https://zhanggroup.org/TM-align/TMalign.cpp -o /opt/tmalign/TMalign.cpp && "
        "echo 'c68bb4ddc4f6162b2aa53fafb53ed8007eb3f24c21e62ced18a93604742fb013  /opt/tmalign/TMalign.cpp' | sha256sum -c - && "
        "g++ -O3 -ffast-math -o /opt/tmalign/TMalign /opt/tmalign/TMalign.cpp",
        "git init /opt/ipsae && git -C /opt/ipsae remote add origin https://github.com/DunbrackLab/IPSAE.git && "
        "git -C /opt/ipsae fetch --depth 1 origin 6174cf9e71cb1bd660cc805856a18c4871a6dec3 && "
        "git -C /opt/ipsae checkout --detach FETCH_HEAD")
    .env({"PYTHONHASHSEED": "0"})
    .add_local_python_source("runner", "vatlog"))

proteinmpnn_image = (base
    .pip_install_from_requirements(str(locks / "proteinmpnn.txt"))
    .run_commands(
        "git init /opt/proteinmpnn && git -C /opt/proteinmpnn remote add origin https://github.com/dauparas/ProteinMPNN.git && "
        "git -C /opt/proteinmpnn fetch --depth 1 origin 8907e6671bfbfc92303b5f79c4b5e6ce47cdef57 && "
        "git -C /opt/proteinmpnn checkout --detach FETCH_HEAD")
    .env({"PYTHONPATH": "/opt/proteinmpnn", "PYTHONHASHSEED": "0"})
    .add_local_python_source("runner", "vatlog"))

esm3_image = (base
    .pip_install_from_requirements(str(locks / "esm3.txt"))
    .pip_install(
        "esm @ git+https://github.com/Biohub/esm.git@43b4548b86762edfa747b07d5f440aad3c33acee",
        extra_options="--no-deps")
    .env({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONHASHSEED": "0"})
    .add_local_python_source("runner", "vatlog"))

mmseqs2_image = (base
    .micromamba_install("mmseqs2=18.8cc5c", channels=["conda-forge", "bioconda"])
    .env({"PYTHONHASHSEED": "0"})
    .add_local_python_source("runner", "vatlog"))

foldseek_image = (base
    .micromamba_install("foldseek=10.941cd33", channels=["conda-forge", "bioconda"])
    .env({"PYTHONHASHSEED": "0"})
    .add_local_python_source("runner", "vatlog"))

recover_image = base.add_local_python_source("runner", "vatlog")


@vat(descriptor=tool_identity("proteinmpnn-soluble", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(name="proteinmpnn-soluble", image=proteinmpnn_image, gpu="L4", timeout=1800,
              retries=0, max_containers=1, block_network=True,
              volumes={"/jobs": jobs, "/weights": assets})
def proteinmpnn_soluble(id: str, request: dict) -> dict:
    return execute_suite("proteinmpnn-soluble", id, request, volume=jobs)


@vat(descriptor=tool_identity("esm3", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(name="esm3", image=esm3_image, gpu="L4", timeout=1800, retries=0,
              max_containers=1, block_network=True,
              volumes={"/jobs": jobs, "/weights": assets})
def esm3(id: str, request: dict) -> dict:
    return execute_suite("esm3", id, request, volume=jobs)


@vat(descriptor=tool_identity("dssp", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(image=cpu_image, timeout=600, retries=0, max_containers=2, block_network=True,
              volumes={"/jobs": jobs})
def dssp(id: str, request: dict) -> dict:
    return execute_suite("dssp", id, request, volume=jobs)


@vat(descriptor=tool_identity("ipsae", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(image=cpu_image, timeout=600, retries=0, max_containers=2, block_network=True,
              volumes={"/jobs": jobs})
def ipsae(id: str, request: dict) -> dict:
    return execute_suite("ipsae", id, request, volume=jobs)


@vat(descriptor=tool_identity("dockq", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(image=cpu_image, timeout=600, retries=0, max_containers=2, block_network=True,
              volumes={"/jobs": jobs})
def dockq(id: str, request: dict) -> dict:
    return execute_suite("dockq", id, request, volume=jobs)


@vat(descriptor=tool_identity("tmalign", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(image=cpu_image, timeout=600, retries=0, max_containers=2, block_network=True,
              volumes={"/jobs": jobs})
def tmalign(id: str, request: dict) -> dict:
    return execute_suite("tmalign", id, request, volume=jobs)


@vat(descriptor=tool_identity("mmseqs2", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(image=mmseqs2_image, timeout=1800, retries=0, max_containers=1, block_network=True,
              volumes={"/jobs": jobs, "/databases": databases})
def mmseqs2(id: str, request: dict) -> dict:
    return execute_suite("mmseqs2", id, request, volume=jobs)


@vat(descriptor=tool_identity("foldseek", APP_NAME), result_adapter=scientific_result_adapter)
@app.function(image=foldseek_image, timeout=1800, retries=0, max_containers=1, block_network=True,
              volumes={"/jobs": jobs, "/databases": databases})
def foldseek(id: str, request: dict) -> dict:
    return execute_suite("foldseek", id, request, volume=jobs)


@app.function(image=recover_image, volumes={"/jobs": jobs})
def recover(id: str) -> dict:
    jobs.reload()
    return recover_suite(id)
