"""Scientific interpretation of ordinary returned values at the shared VAT boundary."""
from __future__ import annotations

import hashlib

from vatlog.capture import AdaptedResult, CaptureContext, VatOutputArtifact
from vatlog.node import DomainOutcome, FunctionIdentity

from .design_inputs import checked_result, sequence_bytes
from .design_store import json_bytes


def tool_identity(tool: str, app_name: str = "biodesign-vat-tools") -> FunctionIdentity:
    return FunctionIdentity(app_name=app_name, function_name=tool)


class ScientificResultAdapter:
    name = "scientific-result"
    version = "1"

    def __call__(self, value: dict, context: CaptureContext) -> AdaptedResult:
        result = checked_result(value)
        execution = result.get("execution")
        job_id, request = context.args
        if (not execution or execution.get("id") != job_id or job_id != context.execution_id
                or execution.get("call_id") != context.call_id
                or execution.get("tool") != context.function.function_name
                or type(execution.get("attempt")) is not int or execution.get("attempt") != 1
                or execution.get("request_sha256") != hashlib.sha256(json_bytes(request)).hexdigest()):
            raise ValueError("Scientific execution identity differs from the recorded call.")
        failed = bool(result.get("error"))
        artifacts = [VatOutputArtifact(a["name"], a["kind"], a["raw"],
                                      "intermediate" if failed or a["kind"] in {"aa_sequence", "fasta"} else "output")
                     for a in result["artifacts"]]
        if not failed:
            artifacts.extend(VatOutputArtifact(f"candidate-{index}.txt", "aa_sequence",
                                               sequence_bytes(candidate["sequence"]), "intermediate")
                             for index, candidate in enumerate(result.get("candidates", [])))
        return AdaptedResult(tuple(artifacts), DomainOutcome(
            status="failed" if failed else "succeeded", code=result.get("error")))


scientific_result_adapter = ScientificResultAdapter()
