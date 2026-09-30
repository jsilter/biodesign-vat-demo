"""Common manifest, execution and recovery codec for the Modal suite."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from importlib.metadata import distributions
from pathlib import Path
from uuid import UUID

from vatlog.canon import parse_json

from .design_inputs import DesignError
from .design_store import json_bytes
from .tool_catalog import KIT_APP, SUITE_APP, tool_spec
from .tool_contracts import validate_tool_request
from .kit_workers import KIT_TOOL_IDS

IMPLEMENTED_SUITE_TOOLS = frozenset({"proteinmpnn-soluble", "dssp", "ipsae", "dockq", "tmalign",
                                     "mmseqs2", "foldseek", "esm3", *KIT_TOOL_IDS})


def _domain_error_code(exc: BaseException) -> str:
    if not isinstance(exc, DesignError):
        return "EXECUTION_FAILED"
    if exc.code in {"ASSET_MISSING", "ASSET_MISMATCH"}:
        return "DEPENDENCY_MISMATCH"
    if exc.code == "INVALID_REQUEST":
        return "OUTPUT_INVALID"
    return exc.code if exc.code in {
        "EXECUTION_FAILED", "OUTPUT_INVALID", "TOOL_UNAVAILABLE", "DEPENDENCY_MISMATCH", "CANCELLED"
    } else "EXECUTION_FAILED"


def _hardware_name(profile: str) -> str:
    if profile == "CPU":
        import platform
        return platform.processor() or "CPU"
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except ImportError:
        pass
    return "CPU"


def execute_suite(tool: str, id: str, request: dict, *, jobs: Path = Path("/jobs"),
                  assets: Path = Path("/weights"), databases: Path = Path("/databases"), volume=None) -> dict:
    """Run one catalog endpoint while preserving start and terminal observations."""
    import modal

    id = str(UUID(id))
    spec = tool_spec(tool)
    if tool not in IMPLEMENTED_SUITE_TOOLS or spec.app_name not in {SUITE_APP, KIT_APP}:
        raise DesignError("TOOL_UNAVAILABLE", "Scientific suite endpoint is not implemented.")
    request = validate_tool_request(request, expected_tool=tool)
    work = jobs / id
    work.mkdir(parents=True, exist_ok=True)
    call_id = modal.current_function_call_id()
    started = {"id": id, "call_id": call_id, "tool": tool,
               "request_sha256": hashlib.sha256(json_bytes(request)).hexdigest(),
               "attempt": 1, "started": datetime.now(timezone.utc).isoformat()}
    if (work / "start.json").exists() and parse_json((work / "start.json").read_bytes()) != started:
        raise ValueError("Scientific job identity already exists; inspect platform redelivery.")
    (work / "request.json").write_bytes(json_bytes(request))
    (work / "start.json").write_bytes(json_bytes(started))
    if volume is not None:
        volume.commit()
    try:
        payload = request["payload"]
        if tool == "proteinmpnn-soluble":
            from .design_workers import run_proteinmpnn, validate_weight_lock
            lock = parse_json((assets / "deployment-lock.json").read_bytes())
            validate_weight_lock(assets, lock, tool)
            result = run_proteinmpnn(payload, assets, work, variant="soluble")
        elif tool == "esm3":
            from .design_workers import run_esm3, validate_weight_lock
            lock = parse_json((assets / "deployment-lock.json").read_bytes())
            validate_weight_lock(assets, lock, tool)
            result = run_esm3(payload, assets, work)
        elif tool in KIT_TOOL_IDS:
            from .kit_workers import run_kit
            result = run_kit(tool, payload, work, assets=assets)
        else:
            from .suite_workers import run_utility
            result = run_utility(tool, payload, work, databases=databases)
    except (DesignError, ValueError, RuntimeError, OSError, SystemExit) as exc:
        result = {"error": _domain_error_code(exc), "artifacts": []}
    lock_path = Path("/opt/vat-locks") / spec.lock
    lock_digest = hashlib.sha256(lock_path.read_bytes()).hexdigest() if lock_path.is_file() else None
    result["execution"] = {**started, "source_repository": spec.source,
                           "source_revision": spec.revision,
                           **({"upstream_repository": spec.upstream} if spec.upstream else {}),
                           "dependency_lock_sha256": lock_digest,
                           "gpu_profile": spec.hardware,
                           "hardware": _hardware_name(spec.hardware),
                           "modal_sdk_version": modal.__version__,
                           "installed_dependencies": {d.metadata["Name"]: d.version for d in distributions()
                                                      if d.metadata["Name"]},
                           "stopped": datetime.now(timezone.utc).isoformat()}
    metadata = {**result, "artifacts": []}
    for index, artifact in enumerate(result.get("artifacts", [])):
        raw = artifact["raw"]
        artifact.update(sha256=hashlib.sha256(raw).hexdigest(), bytes_len=len(raw))
        filename = f"artifact-{index}"
        (work / filename).write_bytes(raw)
        metadata["artifacts"].append({k: v for k, v in artifact.items() if k != "raw"} |
                                     {"file": filename})
    (work / "result.json").write_bytes(json_bytes(metadata))
    if volume is not None:
        volume.commit()
    return result


def recover_suite(id: str, *, jobs: Path = Path("/jobs")) -> dict:
    """Recover byte-identical artifacts without treating absence as no submission."""
    work = jobs / str(UUID(id))
    result = {}
    if (work / "start.json").exists():
        result["start"] = parse_json((work / "start.json").read_bytes())
    if (work / "result.json").exists():
        result["result"] = parse_json((work / "result.json").read_bytes())
        for artifact in result["result"]["artifacts"]:
            filename = artifact.pop("file")
            path = work / filename
            if Path(filename).name != filename or path.is_symlink():
                raise ValueError("Unsafe remote artifact manifest.")
            raw = path.read_bytes()
            if (len(raw) != artifact["bytes_len"]
                    or hashlib.sha256(raw).hexdigest() != artifact["sha256"]):
                raise ValueError("Remote artifact integrity mismatch.")
            artifact["raw"] = raw
    return result
