"""Repository-owned launcher and result adapter for approved generated source."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from vatlog.capture import AdaptedResult, VatOutputArtifact
from vatlog.node import DomainOutcome

from .design_inputs import DesignError
from .dynamic_contracts import DynamicProposal, MAX_OUTPUT_BYTES, validate_dynamic_output, validate_dynamic_proposal

LAUNCHER_VERSION = "vat-dynamic-launcher/1"
IMAGE_DECLARATION = {"family": "debian-slim", "python": "3.12", "packages": []}

BOOTSTRAP = r'''
import json, pathlib, sys
payload = json.load(sys.stdin)
namespace = {"__name__": "approved_dynamic_job"}
exec(compile(payload["source"], "approved_job.py", "exec"), namespace, namespace)
function = namespace.get(payload["entrypoint"])
if not callable(function):
    raise RuntimeError("approved entrypoint is not callable")
output = function(payload["input"], payload["output_dir"])
print(json.dumps(output, allow_nan=False, separators=(",", ":")))
'''


def launch_dynamic(proposal_value: object) -> dict:
    """Run validated source in a child process and collect only regular output files."""
    proposal = validate_dynamic_proposal(proposal_value)
    with tempfile.TemporaryDirectory(prefix="vat-dynamic-") as root:
        output = Path(root) / "outputs"
        output.mkdir(mode=0o700)
        payload = {"source": proposal.source, "entrypoint": proposal.entrypoint,
                   "input": proposal.input, "output_dir": str(output)}
        environment = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"}
        try:
            completed = subprocess.run(
                [sys.executable, "-I", "-c", BOOTSTRAP], input=json.dumps(payload, allow_nan=False).encode(),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=root, env=environment,
                timeout=proposal.resources.timeout_seconds, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DesignError("EXECUTION_FAILED", "Generated job exceeded its approved timeout.") from exc
        if completed.returncode != 0 or len(completed.stdout) > MAX_OUTPUT_BYTES:
            raise DesignError("EXECUTION_FAILED", "Generated job did not return a valid result.")
        try:
            result = json.loads(completed.stdout)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise DesignError("OUTPUT_INVALID", "Generated job returned malformed JSON.") from exc
        if {path.name for path in Path(root).iterdir()} != {"outputs"}:
            raise DesignError("OUTPUT_INVALID", "Generated job wrote outside its output directory.")
        artifacts = []
        for path in sorted(output.iterdir()):
            if path.is_symlink() or not path.is_file() or path.parent != output:
                raise DesignError("OUTPUT_INVALID", "Generated output must be a regular direct child file.")
            if path.stat().st_size > MAX_OUTPUT_BYTES:
                raise DesignError("OUTPUT_INVALID", "Generated artifact exceeds its byte limit.")
            raw = path.read_bytes()
            artifacts.append({"name": path.name, "raw": raw})
        return validate_dynamic_output({"version": "vat-dynamic-output/1",
                                        "result": result, "artifacts": artifacts})


class DynamicResultAdapter:
    name = "vat-dynamic-result"
    version = "1"

    def __call__(self, value, context):
        try:
            output = validate_dynamic_output(value)
            proposal = DynamicProposal.model_validate(context.args[1])
        except (DesignError, ValueError) as exc:
            raise ValueError("invalid dynamic result") from exc
        declarations = (
            VatOutputArtifact("approved-source.py", "log", proposal.source.encode()),
            VatOutputArtifact("dependency-lock.json", "log", b'{"packages":[]}'),
            VatOutputArtifact("image.json", "log",
                              json.dumps(IMAGE_DECLARATION, sort_keys=True, separators=(",", ":")).encode()),
            VatOutputArtifact("launcher.txt", "log", LAUNCHER_VERSION.encode()),
            *(VatOutputArtifact(item["name"], "log", item["raw"])
              for item in output["artifacts"]),
        )
        return AdaptedResult(tuple(declarations), DomainOutcome(
            status="succeeded", code="DYNAMIC_OUTPUT_VALID"))


dynamic_result_adapter = DynamicResultAdapter()
