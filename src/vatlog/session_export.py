"""Seal a portable snapshot of recorded computations without rewriting their nodes."""
from __future__ import annotations

import platform
import time
from datetime import datetime, timezone
from pathlib import Path

from .bundle import build_bundle
from .canon import canonical_bytes
from .node import append_node
from .roles import Keyring
from .schema import (AssuranceTier, DesignParams, DesignerStatement, EnvSnapshot, HashLink,
                     RecordType, Timestamps, UserSession, VatRecord, GENESIS_PREV)
from .store import SessionLog


def publish_recorded(recorder, root: Path, keys: Keyring, session_id: str, *,
                     prompt: str, scope_id: str | None = None):
    """Export observed history; no network, execution, recovery or inferred relationships."""
    log = SessionLog(root, session_id)
    if log.records:
        raise ValueError("Publish to a fresh session; sealed histories are immutable")
    now = datetime.now(timezone.utc)
    env = EnvSnapshot(assurance_tier=AssuranceTier.SUBPROCESS_WRAPPER,
                      python=platform.python_version(), platform=platform.platform())
    user = UserSession(user_id=keys.designer.keyid, purpose_tag=prompt)
    statement = DesignerStatement(session_uuid=session_id, keyid=keys.designer.keyid,
                                  purpose_tag=prompt, issued_at=now)
    statement.signature = keys.designer.sign(statement.signing_payload())
    common = dict(session_uuid=session_id, seq_no=0, user_session=user, environment=env,
                  timestamps=Timestamps(stage_start=now, stage_stop=now, monotonic_ns=time.monotonic_ns()),
                  link=HashLink(sha_prev=GENESIS_PREV, leaf_index=0))
    log.append(VatRecord(record_type=RecordType.SESSION_OPEN, designer_statement=statement,
                        inputs=[log.blobs.artifact("prompt", "purpose.txt", prompt.encode())], **common))
    lifecycle = recorder.lifecycle(scope_id=scope_id)
    for index, event in enumerate(lifecycle):
        stamp = datetime.fromisoformat(event["observed_at"])
        fields = {**common, "timestamps": Timestamps(stage_start=stamp, stage_stop=stamp, monotonic_ns=0)}
        log.append(VatRecord(record_type=RecordType.AGENT_CALL,
            params=DesignParams(hyperparams={"capture_kind": "execution_lifecycle", **event}),
            results=[log.blobs.artifact("log", f"lifecycle-{index}.json", canonical_bytes(event))], **fields))
    for node in recorder.evidence_nodes(scope_id=scope_id):
        append_node(log, node, keys.trust_bundle(), user_session=user)
    log.seal(keys.recorder, recorder_identity=keys.recorder.keyid, environment=env)
    bundle = build_bundle(log)
    bundle.save(log.dir / "bundle.zip")
    keys.trust_bundle().save(log.dir / "trust.json")
    return bundle
