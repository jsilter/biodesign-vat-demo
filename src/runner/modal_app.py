"""SDK-only Modal workers. Deploy after reviewing the staged deployment lock."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import modal
from vatlog import vat
from .vat_adapter import scientific_result_adapter, tool_identity

from .design_workers import (ESM_REVISION, BOLTZ_REVISION, PROTEINMPNN_REVISION,
                             PROTEINMPNN_REPOSITORY,
                             run_boltzgen, run_esmc, run_fold, run_proteinmpnn,
                             collect_boltz_artifacts, collect_proteinmpnn_artifacts)
from .design_store import json_bytes

app = modal.App("biodesign-vat-tools")
FOLD_GPU = "A100-40GB"
volume = modal.Volume.from_name("biodesign-vat-jobs", create_if_missing=True)
weights = modal.Volume.from_name("biodesign-vat-weights", create_if_missing=True)
locks = Path("/opt/vat-locks") if Path("/opt/vat-locks").is_dir() else Path(__file__).with_name("locks")
base = modal.Image.debian_slim(python_version="3.12").apt_install("git").pip_install(
    "gemmi==0.7.5", "pydantic==2.12.5", "cryptography>=39", "rfc8785>=0.1",
    "pymerkle>=6.1").add_local_dir(str(locks), "/opt/vat-locks", copy=True)
esm_image = base.pip_install_from_requirements(str(locks / "esm.txt")).pip_install(
    f"esm @ git+https://github.com/Biohub/esm.git@{ESM_REVISION}", extra_options="--no-deps")
boltz_image = base.pip_install_from_requirements(str(locks / "boltz.txt")).pip_install(
    f"boltzgen @ git+https://github.com/HannesStark/boltzgen.git@{BOLTZ_REVISION}", extra_options="--no-deps")
proteinmpnn_image = base.pip_install_from_requirements(str(locks / "proteinmpnn.txt")).run_commands(
    f"git init /opt/proteinmpnn && git -C /opt/proteinmpnn remote add origin {PROTEINMPNN_REPOSITORY} "
    f"&& git -C /opt/proteinmpnn fetch --depth 1 origin {PROTEINMPNN_REVISION} "
    "&& git -C /opt/proteinmpnn checkout --detach FETCH_HEAD")
esm_image = esm_image.env({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
boltz_image = boltz_image.env({"HF_HUB_OFFLINE": "1"})
proteinmpnn_image = proteinmpnn_image.env({"PYTHONPATH": "/opt/proteinmpnn"})
# Images become deployment candidates, not GPU-validated releases, merely by building.
esm_image = esm_image.add_local_python_source("runner", "vatlog")
boltz_image = boltz_image.add_local_python_source("runner", "vatlog")
proteinmpnn_image = proteinmpnn_image.add_local_python_source("runner", "vatlog")
recover_image = base.add_local_python_source("runner", "vatlog")


def execute(tool: str, id: str, request: dict) -> dict:
    """Publish start identity before computation and retain a result manifest afterward."""
    from .design_inputs import DesignError

    id = str(UUID(id))
    work = Path("/jobs") / id
    work.mkdir(parents=True, exist_ok=True)
    call_id = modal.current_function_call_id()
    request_digest = hashlib.sha256(json_bytes(request)).hexdigest()
    started = {"id": id, "call_id": call_id, "tool": tool, "request_sha256": request_digest,
               "attempt": 1, "started": datetime.now(timezone.utc).isoformat()}
    # Detect platform redelivery instead of silently overwriting execution identity.
    if (work / "start.json").exists():
        existing = json.loads((work / "start.json").read_text())
        if existing != started:
            raise ValueError("Scientific job identity already exists; inspect platform redelivery.")
    (work / "request.json").write_bytes(json_bytes(request))
    (work / "start.json").write_bytes(json_bytes(started))
    volume.commit()
    cache = Path("/weights")
    lock = {}
    try:
        lock = json.loads((cache / "deployment-lock.json").read_text())
        from .design_workers import validate_weight_lock
        validate_weight_lock(cache, lock, tool)
        (work / "progress.json").write_bytes(json_bytes({"message": f"Running {tool}; staged weights verified."}))
        volume.commit()
        worker_request = request
        if request.get("version") == "protein-tool/1":
            from .tool_contracts import worker_request as to_worker_request
            worker_request = to_worker_request(request, expected_tool=tool)
        if tool == "boltzgen":
            result = run_boltzgen(worker_request, cache, work)
        elif tool == "esmfold2":
            result = run_fold(worker_request, cache)
        elif tool == "esmc":
            result = run_esmc(worker_request, cache)
        elif tool == "proteinmpnn":
            result = run_proteinmpnn(worker_request, cache, work)
        else:
            raise DesignError("TOOL_UNAVAILABLE", "Unknown scientific tool.")
    except (DesignError, ValueError, RuntimeError, OSError, SystemExit) as exc:
        partial = (collect_boltz_artifacts(work) if tool == "boltzgen" else
                   collect_proteinmpnn_artifacts(work / "proteinmpnn-output") if tool == "proteinmpnn" else [])
        result = {"error": exc.code if isinstance(exc, DesignError) else "EXECUTION_FAILED", "artifacts": partial}
    revisions = {"boltzgen": BOLTZ_REVISION, "proteinmpnn": PROTEINMPNN_REVISION,
                 "esmfold2": ESM_REVISION, "esmc": ESM_REVISION}
    locks_by_tool = {"boltzgen": "boltz.txt", "proteinmpnn": "proteinmpnn.txt",
                     "esmfold2": "esm.txt", "esmc": "esm.txt"}
    result["execution"] = {**started, "source_revision": revisions[tool],
                           "deployment": lock.get("identity"),
                           "stopped": datetime.now(timezone.utc).isoformat(),
                           "dependency_lock_sha256": hashlib.sha256((Path("/opt/vat-locks") / locks_by_tool[tool]).read_bytes()).hexdigest()}
    if tool == "proteinmpnn":
        result["execution"]["source_repository"] = PROTEINMPNN_REPOSITORY
    import torch
    result["execution"]["hardware"] = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU"
    from importlib.metadata import distributions
    result["execution"]["installed_dependencies"] = {d.metadata["Name"]: d.version for d in distributions() if d.metadata["Name"]}
    prefixes = ("boltzgen/", "mols/") if tool == "boltzgen" else (tool + "/",)
    result["execution"]["weight_files"] = {name: digest for name, digest in lock.get("files", {}).items() if name.startswith(prefixes)}
    result["execution"]["weight_repositories"] = lock.get("repositories", {})
    result["execution"]["chemistry"] = lock.get("chemistry")
    result["execution"]["gpu_profile"] = (FOLD_GPU if tool == "esmfold2" else
                                          "L4" if tool in {"esmc", "proteinmpnn"} else "H100")
    result["execution"]["modal_sdk_version"] = modal.__version__
    metadata = {**result, "artifacts": []}
    for index, artifact in enumerate(result.get("artifacts", [])):
        raw = artifact["raw"]
        # The returned and recoverable values must be byte-for-byte codec-equivalent.
        artifact.update(sha256=hashlib.sha256(raw).hexdigest(), bytes_len=len(raw))
        filename = f"artifact-{index}"
        (work / filename).write_bytes(raw)
        metadata["artifacts"].append({k: v for k, v in artifact.items() if k != "raw"} |
                                     {"file": filename, "sha256": hashlib.sha256(raw).hexdigest(), "bytes_len": len(raw)})
    (work / "result.json").write_bytes(json_bytes(metadata))
    volume.commit()
    return result


@vat(descriptor=tool_identity("boltzgen"), result_adapter=scientific_result_adapter)
@app.function(image=boltz_image, gpu="H100", timeout=1800, retries=0, max_containers=1,
              volumes={"/jobs": volume, "/weights": weights})
def boltzgen(id: str, request: dict) -> dict:
    return execute("boltzgen", id, request)


@vat(descriptor=tool_identity("esmfold2"), result_adapter=scientific_result_adapter)
@app.function(image=esm_image, gpu=FOLD_GPU, timeout=1800, retries=0, max_containers=1,
              volumes={"/jobs": volume, "/weights": weights})
def esmfold2(id: str, request: dict) -> dict:
    return execute("esmfold2", id, request)


@vat(descriptor=tool_identity("esmc"), result_adapter=scientific_result_adapter)
@app.function(image=esm_image, gpu="L4", timeout=1800, retries=0, max_containers=1,
              volumes={"/jobs": volume, "/weights": weights})
def esmc(id: str, request: dict) -> dict:
    return execute("esmc", id, request)


@vat(descriptor=tool_identity("proteinmpnn"), result_adapter=scientific_result_adapter)
@app.function(image=proteinmpnn_image, gpu="L4", timeout=1800, retries=0, max_containers=1,
              volumes={"/jobs": volume, "/weights": weights})
def proteinmpnn(id: str, request: dict) -> dict:
    return execute("proteinmpnn", id, request)


@app.function(image=recover_image, volumes={"/jobs": volume})
def recover(id: str) -> dict:
    """Read known start/result identities; absence is never proof of no submission."""
    work = Path("/jobs") / str(UUID(id))
    volume.reload()
    result = {}
    if (work / "start.json").exists():
        result["start"] = json.loads((work / "start.json").read_text())
    if (work / "result.json").exists():
        result["result"] = json.loads((work / "result.json").read_text())
        for artifact in result["result"]["artifacts"]:
            name = artifact.pop("file")
            path = work / name
            if Path(name).name != name or path.is_symlink():
                raise ValueError("Unsafe remote artifact manifest.")
            raw = path.read_bytes()
            if len(raw) != artifact["bytes_len"] or hashlib.sha256(raw).hexdigest() != artifact["sha256"]:
                raise ValueError("Remote artifact integrity mismatch.")
            artifact["raw"] = raw
    if (work / "progress.json").exists():
        result["progress"] = json.loads((work / "progress.json").read_text())
    if "result" not in result and result.get("start", {}).get("tool") in {"boltzgen", "proteinmpnn"}:
        result["partial_artifacts"] = (collect_boltz_artifacts(work) if result["start"]["tool"] == "boltzgen"
                                       else collect_proteinmpnn_artifacts(work / "proteinmpnn-output"))
    return result
