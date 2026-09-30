"""Build real local evidence for successful text-only model calls."""

from __future__ import annotations

import platform
import time
from datetime import datetime
from pathlib import Path

from vatlog.bundle import build_bundle
from vatlog.notary import issue_receipt
from vatlog.roles import Keyring
from vatlog.schema import (GENESIS_PREV, AssuranceTier, DesignParams, DesignerStatement,
                           EnvSnapshot, HashLink, RecordType, Timestamps, UserSession, VatRecord)
from vatlog.store import SessionLog
from vatlog.verify import verify_bundle
from .provider import SYSTEM
from .agent import Event

FILES = ("bundle.zip", "trust.json", "report.txt", "response.txt")


def _prepare_capture(root: Path, session_id: str, prompt: str, events: list[Event],
            text: str, started: datetime, scientific_artifacts: dict[int, list[tuple[str, str, bytes]]] | None = None,
            purpose: str = "Designer research; public read-only API and MCP tools",
            system: str = SYSTEM, keys: Keyring | None = None, execution_nodes: dict | None = None) -> Path:
    """Seal locally observed model/tool events and attached scientific outputs, not independent capture."""
    keys = keys or Keyring.generate()
    log = SessionLog(root, session_id)
    env = EnvSnapshot(assurance_tier=AssuranceTier.SUBPROCESS_WRAPPER,
                      python=platform.python_version(), platform=platform.platform())
    user = UserSession(user_id=keys.designer.keyid, purpose_tag=purpose)
    statement = DesignerStatement(session_uuid=session_id, keyid=keys.designer.keyid,
                                  purpose_tag=user.purpose_tag, issued_at=started)
    statement.signature = keys.designer.sign(statement.signing_payload())
    instructions = log.blobs.artifact("prompt", "system.txt", system.encode())
    prompt_ref = log.blobs.artifact("prompt", "prompt.txt", prompt.encode())
    common = dict(session_uuid=session_id, seq_no=0, user_session=user, environment=env,
                  timestamps=Timestamps(stage_start=started, stage_stop=started, monotonic_ns=time.monotonic_ns()),
                  link=HashLink(sha_prev=GENESIS_PREV, leaf_index=0))
    log.append(VatRecord(record_type=RecordType.SESSION_OPEN, designer_statement=statement,
                         inputs=[instructions, prompt_ref], **common))
    for index, event in enumerate(events):
        if execution_nodes and index in execution_nodes:
            result = execution_nodes[index]
            if not result.verify(keys.trust_bundle()).passed:
                raise ValueError("scientific execution node failed verification")
            result.append_to(log, user_session=user)
            # Scientific acceptance is a separate observed decision; it cannot rewrite execution.
            artifacts = [log.blobs.artifact(kind, name, raw)
                         for kind, name, raw in (scientific_artifacts or {}).get(index, [])]
            accepted = event.metadata.get("status") == "completed"
            decision = log.blobs.artifact("log", f"validation-{index}.json", event.response)
            log.append(VatRecord(record_type=RecordType.DESIGN_RUN,
                                 results=[decision, *(artifacts if accepted else [])],
                                 intermediates=[] if accepted else artifacts,
                                 params=DesignParams(hyperparams={**event.metadata,
                                     "vat_node_id": result.node_id,
                                     "capture_boundary": "scientific validation decision"}),
                                 **{**common, "timestamps": Timestamps(stage_start=event.started,
                                      stage_stop=event.stopped, monotonic_ns=time.monotonic_ns())}))
            continue
        request_name = "request.json" if index == 0 else f"request-{index}.json"
        response_name = "response.json" if index == 0 else f"response-{index}.json"
        results = [log.blobs.artifact("transcript", response_name, event.response)]
        if scientific_artifacts:
            results.extend(log.blobs.artifact(kind, name, raw) for kind, name, raw in scientific_artifacts.get(index, []))
        if index == len(events) - 1:
            results.append(log.blobs.artifact("transcript", "response.txt", text.encode()))
        fields = {**common, "timestamps": Timestamps(stage_start=event.started,
                  stage_stop=event.stopped, monotonic_ns=time.monotonic_ns())}
        log.append(VatRecord(record_type=RecordType.DESIGN_RUN if event.name in {"boltzgen", "proteinmpnn", "esmfold2", "esmc"} else RecordType.AGENT_CALL, inputs=[prompt_ref,
            log.blobs.artifact("prompt" if event.name == "openrouter" else "log", request_name, event.request)],
            results=results, params=DesignParams(hyperparams={**event.metadata,
            "capture_boundary": "local software-observed model/tool request and result bytes"}), **fields))
    log.seal(keys.recorder, recorder_identity=keys.recorder.keyid, environment=env)
    bundle = build_bundle(log)
    trust = keys.trust_bundle()
    bundle.notary_receipt = issue_receipt(bundle, keys.notary, trust)
    bundle.save(log.dir / "bundle.zip")
    trust.save(log.dir / "trust.json")
    (log.dir / "response.txt").write_text(text, encoding="utf-8")
    return log.dir


def capture(root: Path, session_id: str, prompt: str, events: list[Event],
            text: str, started: datetime, scientific_artifacts=None,
            purpose: str = "Designer research; public read-only API and MCP tools",
            system: str = SYSTEM, keys: Keyring | None = None, execution_nodes=None,
            publication_root: Path | None = None) -> Path:
    """Prepare once and recover exact notarized bytes across publication failures."""
    import tempfile
    from dataclasses import asdict
    from vatlog.canon import canonical_bytes, sha256_hex
    from .publication import PublicationStore
    # Hash raw event/artifact bytes explicitly rather than interpreting their content.
    def identity_value(value):
        if isinstance(value, bytes):
            return {"sha256": sha256_hex(value), "length": len(value)}
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {str(k): identity_value(v) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return [identity_value(v) for v in value]
        return value
    request = identity_value({"session": session_id, "prompt": prompt, "events": [asdict(e) for e in events],
        "text": text, "started": started, "artifacts": scientific_artifacts, "purpose": purpose,
        "system": system, "nodes": {str(k): v.node_id for k, v in (execution_nodes or {}).items()}})
    digest = sha256_hex(canonical_bytes(request))
    with PublicationStore(publication_root or root / ".publication", keys) as publisher:
        if not publisher.prepared(session_id, digest):
            with tempfile.TemporaryDirectory(prefix="vat-prepare-") as temporary:
                directory = _prepare_capture(Path(temporary), session_id, prompt, events, text, started,
                    scientific_artifacts, purpose, system, publisher.keys, execution_nodes)
                publisher.prepare(session_id, digest, (directory / "bundle.zip").read_bytes(), text.encode())
        return publisher.publish(session_id)
