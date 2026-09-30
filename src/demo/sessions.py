"""Builders for the three demo sessions.

Shared by the test suite and by the fixture generator so the thing the tests assert and the thing
the demo shows are the same code path.

  clean     opening + 3 runs, purpose matches the recorded objectives, all 4 revealed.
  doctored  opening + 3 agent calls + 3 design runs. Purpose tag says "therapeutic lead";
            the objectives include an immune-evasion
            term. Every hash is valid and every signature verifies. This is the case RAND's
            schema is *for*: the record is honest and the discrepancy is only visible to
            someone who reads it.
  partial   9 runs logged honestly, bundle reveals 1. Verify checks 1-4 pass and check 5
            reports 1 of 10 (including the session opening).
"""

from __future__ import annotations

import json
import platform
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from vatlog.attest import DSSEEnvelope
from vatlog.head import SignedTreeHead
from vatlog.keys import generate_key
from vatlog.roles import Keyring, LocalSigner, Role, signer_for
from vatlog.schema import (
    AssuranceTier,
    DataSource,
    DesignParams,
    DesignerStatement,
    EnvSnapshot,
    HashLink,
    ObjectiveTerm,
    RecordType,
    Timestamps,
    UserSession,
    VatRecord,
)
from vatlog.store import SessionLog

BACKBONE = "1QYS"
BASE_TIME = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)

# Deterministic stand-in sequences. The real runner replaces these with ProteinMPNN output;
# the fixtures must not need a GPU.
DESIGNS = [
    "MKVEEAIRRLEELLKKAKEEGNEELLRIAEELLKRAEELLKKGNPELAKKVLELAKKLLELAK",
    "MSVEEALRKLEELLRKAKEEGNPELLRIAEELLKRAKELLEKGNEELAKKVLELAKELLEKAK",
    "MTVEEAIRKLEELLKKAEEEGNPELLRIAEELLKRAEELLKKGNPELAKRVLELAKKLLEKAK",
    "MKVEEALRRLEELLRKAKEEGNEELLRIAEELLKRAKELLKKGNEELAKKVLELAKELLELAK",
    "MQVEEAIRKLEELLKKAKEEGNPELLRIAEELLKRAEELLEKGNPELAKKVLELAKKLLEKAK",
    "MRVEEALRKLEELLKKAEEEGNEELLRIAEELLKRAKELLKKGNEELAKRVLELAKELLELAK",
    "MNVEEAIRRLEELLRKAKEEGNPELLRIAEELLKRAEELLKKGNPELAKKVLELAKKLLEKAK",
    "MDVEEALRKLEELLKKAKEEGNEELLRIAEELLKRAKELLEKGNEELAKKVLELAKELLEKAK",
    "MEVEEAIRKLEELLRKAEEEGNPELLRIAEELLKRAEELLKKGNPELAKRVLELAKKLLELAK",
]

THERAPEUTIC_OBJECTIVES = [
    ObjectiveTerm(name="mpnn_nll", weight=1.0, direction="min"),
    ObjectiveTerm(name="predicted_solubility", weight=0.3, direction="max"),
    ObjectiveTerm(name="off_target_binding", weight=0.5, direction="min"),
]

# The divergence. A purpose tag of "therapeutic lead" does not explain optimizing against
# MHC-II presentation and serum antibody recognition; RAND's stated value for the design
# parameters group is "detect covert optimization for toxicity or immune evasion".
DOCTORED_OBJECTIVES = [
    ObjectiveTerm(name="mpnn_nll", weight=1.0, direction="min"),
    ObjectiveTerm(name="mhc_ii_presentation", weight=0.8, direction="min"),
    ObjectiveTerm(name="serum_antibody_recognition", weight=0.7, direction="min"),
]


@dataclass(frozen=True)
class CallArtifacts:
    """Opaque bytes for one call, or a producer-aggregated per-run transcript."""

    prompt: bytes | None = None
    transcript: bytes | None = None
    reasoning: bytes | None = None


def _fixture_calls(n_runs: int, objectives: list[ObjectiveTerm]) -> list[CallArtifacts]:
    """Synthetic timestamped tool transcripts, not outputs from a live model."""
    return [CallArtifacts(
        prompt=f"Record demonstration candidate {i} using the declared fixture objectives.".encode(),
        transcript=json.dumps([{
            "timestamp": (BASE_TIME + timedelta(minutes=3 * i)).isoformat(),
            "role": "assistant",
            "tool_call": {"name": "design_fixture", "arguments": {
                "candidate": i, "objective_names": [o.name for o in objectives],
            }},
            "tool_result": "synthetic demonstration candidate recorded",
        }]).encode(),
    ) for i in range(n_runs)]


def _environment() -> EnvSnapshot:
    return EnvSnapshot(
        assurance_tier=AssuranceTier.SERVERLESS_HOSTED,
        python=sys.version.split()[0],
        platform=platform.platform(),
        cuda=None,
        gpu_model=None,
        modal_call_id="fc-demo-0000000000",
    )


def _event_record(
    *,
    record_type: RecordType,
    index: int,
    purpose_tag: str,
    user_id: str,
    log: SessionLog,
) -> VatRecord:
    started = BASE_TIME + timedelta(minutes=3 * index)
    return VatRecord(
        record_type=record_type,
        session_uuid=log.session_uuid,
        seq_no=0,  # assigned by append
        user_session=UserSession(
            user_id=user_id,
            oidc_sub="https://idp.example.edu/u/researcher",
            purpose_tag=purpose_tag,
            geo_coarse="US-MA",
        ),
        timestamps=Timestamps(
            stage_start=started,
            stage_stop=started + timedelta(seconds=42),
            monotonic_ns=time.monotonic_ns(),
        ),
        environment=_environment(),
        link=HashLink(sha_prev="00" * 32, leaf_index=0),  # assigned by append
    )


def _attach_call_artifacts(
    record: VatRecord, log: SessionLog, *, prompt: bytes | None,
    transcript: bytes | None, reasoning: bytes | None,
) -> None:
    for kind, data, group in (
        ("prompt", prompt, record.inputs),
        ("transcript", transcript, record.results),
        ("reasoning", reasoning, record.results),
    ):
        if data is not None:
            group.append(log.blobs.artifact(kind, f"{kind}.txt", data))


def _run_record(
    *, index: int, sequence: str, purpose_tag: str, objectives: list[ObjectiveTerm],
    user_id: str, log: SessionLog, prompt: bytes | None = None,
    transcript: bytes | None = None, reasoning: bytes | None = None,
) -> VatRecord:
    record = _event_record(record_type=RecordType.DESIGN_RUN, index=index,
                           purpose_tag=purpose_tag, user_id=user_id, log=log)
    record.inputs = [log.blobs.artifact("pdb_id", "backbone.txt", BACKBONE.encode())]
    record.params = DesignParams(
        hyperparams={"temperature": 0.1, "num_seq_per_target": 8, "model": "ProteinMPNN-v_48_020"},
        objectives=objectives, seeds=[1000 + index],
    )
    record.external = [DataSource(name="PDB", release_or_date="2026-09-01")]
    record.results = [log.blobs.artifact("aa_sequence", f"design_{index}.fasta", sequence.encode())]
    _attach_call_artifacts(record, log, prompt=prompt, transcript=transcript, reasoning=reasoning)
    return record


def agent_call_record(
    *, index: int, purpose_tag: str, user_id: str, log: SessionLog,
    prompt: bytes | None = None, transcript: bytes | None = None, reasoning: bytes | None = None,
) -> VatRecord:
    """Build an independently disclosable call leaf; the caller appends it to the session."""
    record = _event_record(record_type=RecordType.AGENT_CALL, index=index,
                           purpose_tag=purpose_tag, user_id=user_id, log=log)
    record.params = DesignParams(hyperparams={"model": "synthetic-demo-agent"})
    _attach_call_artifacts(record, log, prompt=prompt, transcript=transcript, reasoning=reasoning)
    return record


def build_session(
    root: Path,
    *,
    session_uuid: str,
    n_runs: int,
    purpose_tag: str,
    objectives: list[ObjectiveTerm],
    key: Ed25519PrivateKey | LocalSigner | None = None,
    keyring: Keyring | None = None,
    designer_statement: DesignerStatement | None = None,
    instructions: bytes | None = None,
    calls: list[CallArtifacts] | None = None,
    granularity: Literal["design_run", "agent_call"] | None = None,
) -> tuple[SessionLog, Ed25519PrivateKey, SignedTreeHead, DSSEEnvelope]:
    """Build and seal a session; return ``(log, recorder_key, head, attestation)``.

    ``calls`` supplies one entry per design run. Select ``design_run`` to attach aggregated
    call bytes to each run, or ``agent_call`` for a separate leaf before each run. Custom
    producers can append any number of ``agent_call_record`` leaves before sealing.
    Instructions and call bytes must be redacted, if needed, before they enter this builder.
    """
    if n_runs < 0:
        raise ValueError("n_runs must be nonnegative")
    if calls is not None and (len(calls) != n_runs or granularity not in ("design_run", "agent_call")):
        raise ValueError("calls requires one entry per run and an explicit record granularity")
    if granularity is not None and granularity not in ("design_run", "agent_call"):
        raise ValueError("unknown record granularity")
    if keyring is not None and key is not None:
        raise ValueError("supply keyring or recorder key, not both")
    if designer_statement is not None and keyring is None and key is None:
        raise ValueError("a supplied designer statement requires a recorder key")
    if designer_statement is None:
        keyring = keyring or Keyring(
            LocalSigner(Role.DESIGNER, generate_key()),
            signer_for(key or generate_key(), Role.RECORDER),
            LocalSigner(Role.NOTARY, generate_key()),
            LocalSigner(Role.TRANSPARENCY_LOG, generate_key()),
        )
        designer_statement = DesignerStatement(
            session_uuid=session_uuid, keyid=keyring.designer.keyid,
            purpose_tag=purpose_tag, issued_at=BASE_TIME,
        )
        designer_statement.signature = keyring.designer.sign(designer_statement.signing_payload())
    if designer_statement.session_uuid != session_uuid or designer_statement.purpose_tag != purpose_tag:
        raise ValueError("designer statement must bind the requested session and purpose")
    recorder = keyring.recorder if keyring is not None else signer_for(key, Role.RECORDER)
    key = recorder.key
    user_id = designer_statement.keyid
    log = SessionLog(root, session_uuid)
    log.append(VatRecord(
        record_type=RecordType.SESSION_OPEN, session_uuid=session_uuid, seq_no=0,
        user_session=UserSession(user_id=user_id, purpose_tag=purpose_tag),
        designer_statement=designer_statement,
        inputs=[] if instructions is None else [log.blobs.artifact("prompt", "instructions.txt", instructions)],
        timestamps=Timestamps(stage_start=BASE_TIME, stage_stop=BASE_TIME, monotonic_ns=time.monotonic_ns()),
        environment=_environment(), link=HashLink(sha_prev="00" * 32, leaf_index=0),
    ))
    for i in range(n_runs):
        call = calls[i] if calls is not None else CallArtifacts()
        if granularity == "agent_call":
            log.append(agent_call_record(
                index=2 * i, purpose_tag=purpose_tag, user_id=user_id, log=log,
                prompt=call.prompt, transcript=call.transcript, reasoning=call.reasoning,
            ))
            call = CallArtifacts()
        log.append(
            _run_record(
                index=2 * i + 1 if granularity == "agent_call" else i,
                sequence=DESIGNS[i % len(DESIGNS)],
                purpose_tag=purpose_tag,
                objectives=objectives,
                user_id=user_id,
                log=log,
                prompt=call.prompt, transcript=call.transcript, reasoning=call.reasoning,
            )
        )
    head, att = log.seal(
        recorder,
        recorder_identity="biodesign-vat recorder 0.1 (modal)",
        environment=_environment(),
        timestamp=BASE_TIME + timedelta(minutes=3 * n_runs * (2 if granularity == "agent_call" else 1)),
    )
    return log, key, head, att


def build_clean(
    root: Path, key: Ed25519PrivateKey | LocalSigner | None = None, *, keyring: Keyring | None = None,
    instructions: bytes | None = None, calls: list[CallArtifacts] | None = None,
    granularity: Literal["design_run", "agent_call"] | None = None,
) -> tuple[SessionLog, Ed25519PrivateKey, SignedTreeHead, DSSEEnvelope]:
    """Build an opening and three honestly recorded therapeutic design runs."""
    return build_session(
        root,
        session_uuid="clean-0001",
        n_runs=3,
        purpose_tag="therapeutic lead",
        objectives=THERAPEUTIC_OBJECTIVES,
        key=key,
        keyring=keyring,
        instructions=instructions, calls=calls, granularity=granularity,
    )


def build_doctored(
    root: Path, key: Ed25519PrivateKey | LocalSigner | None = None, *, keyring: Keyring | None = None,
    instructions: bytes | None = None, calls: list[CallArtifacts] | None = None,
    granularity: Literal["design_run", "agent_call"] = "agent_call",
) -> tuple[SessionLog, Ed25519PrivateKey, SignedTreeHead, DSSEEnvelope]:
    """Build an honest log with objectives that diverge from the stated purpose."""
    return build_session(
        root,
        session_uuid="doctored-0001",
        n_runs=3,
        purpose_tag="therapeutic lead",
        objectives=DOCTORED_OBJECTIVES,
        key=key,
        keyring=keyring,
        instructions=instructions if instructions is not None else b"Record the therapeutic-lead demonstration; preserve the declared objectives.",
        calls=calls if calls is not None else _fixture_calls(3, DOCTORED_OBJECTIVES),
        granularity=granularity,
    )


def build_partial(
    root: Path, key: Ed25519PrivateKey | LocalSigner | None = None, *, keyring: Keyring | None = None,
    instructions: bytes | None = None, calls: list[CallArtifacts] | None = None,
    granularity: Literal["design_run", "agent_call"] = "design_run",
) -> tuple[SessionLog, Ed25519PrivateKey, SignedTreeHead, DSSEEnvelope]:
    """Build an opening and nine runs for the selective-disclosure demonstration."""
    return build_session(
        root,
        session_uuid="partial-0001",
        n_runs=9,
        purpose_tag="therapeutic lead",
        objectives=THERAPEUTIC_OBJECTIVES,
        key=key,
        keyring=keyring,
        instructions=instructions if instructions is not None else b"Record each demonstration design run and its supplied transcript.",
        calls=calls if calls is not None else _fixture_calls(9, THERAPEUTIC_OBJECTIVES),
        granularity=granularity,
    )
