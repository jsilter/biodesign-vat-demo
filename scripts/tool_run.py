#!/usr/bin/env python3
"""Run one cataloged Modal protein tool through the common VatResult boundary."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv

from runner.design_inputs import DesignError
from runner.design_store import DesignStore, json_bytes
from runner.modal_client import ModalTools
from runner.tool_catalog import TOOLS, tool_spec
from runner.tool_contracts import validate_tool_request
from vatlog.node import NodeStatus
from vatlog.canon import parse_json


async def run(tool: str, request: dict, output: Path) -> int:
    """Submit once, collect without a polling deadline, and save signed result evidence."""
    spec = tool_spec(tool)
    if not spec.implemented:
        raise DesignError("TOOL_UNAVAILABLE", spec.blocker or "Scientific tool is inventory-only.")
    if spec.readiness == "blocked":
        raise DesignError("TOOL_UNAVAILABLE", spec.blocker or "Scientific tool is unavailable.")
    envelope = validate_tool_request(request, expected_tool=tool)
    output.mkdir(parents=True, exist_ok=False)
    store = DesignStore(output / "journal")
    transport = ModalTools()
    transport.bind_store(store)
    try:
        result = await transport.run.aio(tool, envelope)
        result.save(output / "vat-node.json")
        artifacts = []
        for artifact in result.artifacts:
            destination = output / artifact.name
            if destination.parent != output or destination.exists():
                raise DesignError("OUTPUT_INVALID", "Result contains an unsafe or duplicate artifact name.")
            destination.write_bytes(artifact.read_bytes())
            artifacts.append({"name": artifact.name, "kind": artifact.kind,
                              "classification": artifact.classification,
                              "sha256": artifact.sha256, "bytes_len": artifact.bytes_len})
        value = result.value if result.status is NodeStatus.SUCCEEDED else None
        if isinstance(value, dict) and isinstance(value.get("artifacts"), list):
            value = {**value, "artifacts": [
                {key: item for key, item in artifact.items() if key != "raw"}
                for artifact in value["artifacts"]]}
        report = {"tool": tool, "execution_id": result.execution_id, "node_id": result.node_id,
                  "status": result.status.value, "domain_outcome": (
                      result.domain_outcome.model_dump(mode="json") if result.domain_outcome else None),
                  "error": result.error.model_dump(mode="json") if result.error else None,
                  "value": value,
                  "artifacts": artifacts}
        (output / "result.json").write_bytes(json_bytes(report))
        print(json.dumps(report, indent=2, allow_nan=False))
        accepted = (result.status is NodeStatus.SUCCEEDED
                    and (result.domain_outcome is None or result.domain_outcome.status == "succeeded"))
        return 0 if accepted else 1
    finally:
        await transport.drain()
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=[tool.id for tool in TOOLS])
    parser.add_argument("--request", type=Path, required=True,
                        help="Local JSON file containing the versioned common request envelope")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirm-paid-work", action="store_true",
                        help="Confirm the owner authorized this billed Modal call")
    args = parser.parse_args()
    if not args.confirm_paid_work:
        parser.error("Explicitly pass --confirm-paid-work after obtaining owner spend authorization.")
    try:
        request = parse_json(args.request.read_bytes())
    except (OSError, ValueError) as exc:
        parser.error(f"Request JSON is unavailable or malformed: {exc}")
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False, interpolate=False)
    try:
        return asyncio.run(run(args.tool, request, args.output))
    except DesignError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
