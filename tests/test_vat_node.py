"""Basic standalone node cryptography, canonical values and session embedding."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from vatlog.attest import pae
from vatlog.bundle import build_bundle
from vatlog.canon import canonical_bytes, sha256_hex
from vatlog.node import (ASSURANCE_PROFILE, NODE_PAYLOAD_TYPE, EncodedDocument,
                         ExecutionStatement, FunctionIdentity, NodeError, NodeOutcome,
                         NodeStatus, SafeException, VatLimits, VatNode, append_node,
                         decode_document, encode_document, node_result_value, verify_node)
from vatlog.roles import Keyring
from vatlog.schema import (AssuranceTier, DesignerStatement, EnvSnapshot, GENESIS_PREV,
                           HashLink, RecordType, Timestamps, UserSession, VatRecord)
from vatlog.store import SessionLog
from vatlog.verify import Outcome, verify_bundle

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def merge(*documents: EncodedDocument) -> tuple[list, dict[str, bytes]]:
    artifacts, blobs = [], {}
    for document in documents:
        artifacts.extend([document.artifact, *document.byte_artifacts])
        blobs.update(document.blobs)
    return artifacts, blobs


def statement_and_blobs(*, status=NodeStatus.SUCCEEDED, result=None):
    call = encode_document({"args": [12, b"input"], "kwargs": {"mode": "safe"}}, "call.json")
    inputs, blobs = merge(call)
    results = []
    exception = None
    if status is NodeStatus.SUCCEEDED:
        returned = encode_document({"answer": 144, "raw": result or b"output"}, "return.json")
        results, result_blobs = merge(returned)
        blobs.update(result_blobs)
    else:
        exception = SafeException(code="FUNCTION_ERROR", module="builtins", type="ValueError", message="safe failure")
    timestamps = Timestamps(stage_start=NOW + timedelta(seconds=1),
                            stage_stop=NOW + timedelta(seconds=2), monotonic_ns=123)
    environment = EnvSnapshot(assurance_tier=AssuranceTier.SUBPROCESS_WRAPPER,
                              python="3.11", platform="test", modal_call_id="fc-1")
    statement = ExecutionStatement(
        assurance_profile=ASSURANCE_PROFILE, execution_id=uuid4(), attempt=1, status=status,
        function=FunctionIdentity(app_name="example", function_name="square", module="example",
                                  qualname="square", source_sha256="ab" * 32),
        modal_call_id="fc-1", modal_input_id="in-1", dispatched_at=NOW,
        recorded_at=NOW + timedelta(seconds=3), timestamps=timestamps,
        inputs=inputs, results=results, exception=exception, environment=environment,
    )
    return statement, blobs


def signed_node(ring=None, *, status=NodeStatus.SUCCEEDED):
    ring = ring or Keyring.generate()
    statement, blobs = statement_and_blobs(status=status)
    return ring, VatNode.sign(statement, blobs, ring.recorder)


def test_real_signature_and_blob_verification_round_trip(tmp_path):
    ring, node = signed_node()
    report = verify_node(node, ring.trust_bundle())
    assert report.outcome is NodeOutcome.OK
    assert report.node_id == sha256_hex(node.canonical_bytes()) == node.node_id
    assert node_result_value(node) == {"answer": 144, "raw": b"output"}
    signature = base64.b64decode(node.signatures[0].sig, validate=True)
    payload = base64.b64decode(node.payload, validate=True)
    ring.recorder.key.public_key().verify(signature, pae(NODE_PAYLOAD_TYPE, payload))
    path = tmp_path / "node.json"
    node.save(path)
    assert path.stat().st_mode & 0o077 == 0
    loaded = VatNode.load(path)
    assert loaded.canonical_bytes() == node.canonical_bytes()
    assert verify_node(loaded, ring.trust_bundle()).passed


def test_wrong_key_and_signature_mutation_are_bad_signatures():
    ring, node = signed_node()
    assert verify_node(node, Keyring.generate().trust_bundle()).outcome is NodeOutcome.BAD_SIGNATURE
    changed = node.model_copy(deep=True)
    changed.signatures[0].sig = base64.b64encode(b"x" * 64).decode()
    assert verify_node(changed, ring.trust_bundle()).outcome is NodeOutcome.BAD_SIGNATURE


@pytest.mark.parametrize("mutation", ["blob", "extra_blob", "length", "noncanonical_payload"])
def test_signed_node_rejects_content_and_manifest_tampering(mutation):
    ring, node = signed_node()
    changed = node.model_copy(deep=True)
    if mutation == "blob":
        digest = next(iter(changed.blobs))
        changed.blobs[digest] = base64.b64encode(b"changed").decode()
    elif mutation == "extra_blob":
        changed.blobs["00" * 32] = base64.b64encode(b"extra").decode()
    elif mutation == "length":
        statement = changed.statement()
        statement.inputs[0].bytes_len += 1
        raw = canonical_bytes(statement.model_dump(mode="json"))
        changed.payload = base64.b64encode(raw).decode()
        changed.signatures[0].sig = ring.recorder.sign(pae(NODE_PAYLOAD_TYPE, raw))
    else:
        statement = changed.statement().model_dump(mode="json")
        raw = json.dumps(statement, indent=2).encode()
        changed.payload = base64.b64encode(raw).decode()
        changed.signatures[0].sig = ring.recorder.sign(pae(NODE_PAYLOAD_TYPE, raw))
    assert verify_node(changed, ring.trust_bundle()).outcome is NodeOutcome.TAMPERED


def test_value_codec_round_trips_supported_values_and_extracts_bytes():
    value = {"none": None, "bool": True, "int": 2**53 - 1, "float": 1.25,
             "text": "β", "array": (1, b"bytes")}
    document = encode_document(value, "call.json")
    decoded = decode_document(document.artifact, list(document.byte_artifacts), document.blobs)
    assert decoded == {**value, "array": [1, b"bytes"]}
    assert canonical_bytes(document.encoded) == document.blobs[document.artifact.sha256]


def test_value_codec_deduplicates_identical_byte_values():
    document = encode_document([b"same", b"same"], "call.json")
    assert len(document.byte_artifacts) == 1
    assert decode_document(document.artifact, list(document.byte_artifacts), document.blobs) == [b"same", b"same"]


@pytest.mark.parametrize("factory", [
    lambda: float("nan"), lambda: float("inf"), lambda: 2**53, lambda: {1: "value"},
    lambda: {"$vat-forged": "x"}, lambda: {1, 2}, lambda: object(),
])
def test_value_codec_rejects_unsafe_or_noncanonical_types(factory):
    with pytest.raises(NodeError):
        encode_document(factory(), "call.json")


def test_value_codec_rejects_cycles_depth_count_and_size():
    cycle = []
    cycle.append(cycle)
    with pytest.raises(NodeError, match="Cyclic"):
        encode_document(cycle, "call.json")
    with pytest.raises(NodeError, match="depth or item"):
        encode_document([[[1]]], "call.json", limits=VatLimits(max_depth=1))
    with pytest.raises(NodeError, match="depth or item"):
        encode_document([1, 2], "call.json", limits=VatLimits(max_items=2))
    with pytest.raises(NodeError, match="per-blob"):
        encode_document(b"123", "call.json", limits=VatLimits(max_blob_bytes=2))
    with pytest.raises(NodeError, match="exceed"):
        encode_document("long", "call.json", limits=VatLimits(max_value_bytes=2))


def test_signing_enforces_portable_node_size_limit():
    ring = Keyring.generate()
    statement, blobs = statement_and_blobs()
    with pytest.raises(NodeError, match="Canonical node"):
        VatNode.sign(statement, blobs, ring.recorder, limits=VatLimits(max_node_bytes=10))


def test_decoder_rejects_tag_smuggling_and_unreferenced_byte_artifacts():
    document = encode_document(b"value", "call.json")
    forged = {"$vat": "bytes/1", "sha256": document.byte_artifacts[0].sha256,
              "bytes_len": 5, "extra": True}
    raw = canonical_bytes(forged)
    main = document.artifact.model_copy(update={"sha256": sha256_hex(raw), "bytes_len": len(raw)})
    blobs = {**document.blobs, main.sha256: raw}
    blobs.pop(document.artifact.sha256)
    with pytest.raises(NodeError, match="Malformed"):
        decode_document(main, list(document.byte_artifacts), blobs)
    plain = encode_document("no bytes", "call.json")
    with pytest.raises(NodeError, match="unreferenced"):
        decode_document(plain.artifact, list(document.byte_artifacts),
                        {**plain.blobs, **{a.sha256: document.blobs[a.sha256] for a in document.byte_artifacts}})


def test_node_loader_rejects_noncanonical_duplicate_and_symlink_files(tmp_path):
    _, node = signed_node()
    noncanonical = tmp_path / "pretty.json"
    noncanonical.write_text(json.dumps(node.model_dump(mode="json"), indent=2))
    with pytest.raises(NodeError, match="canonical"):
        VatNode.load(noncanonical)
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"node_format":"vat-node/1","node_format":"vat-node/1"}')
    with pytest.raises(ValueError, match="duplicate"):
        VatNode.load(duplicate)
    link = tmp_path / "link.json"
    link.symlink_to(noncanonical)
    with pytest.raises(NodeError, match="unsafe"):
        VatNode.load(link)


def open_session(root: Path, ring: Keyring, name: str) -> tuple[SessionLog, UserSession]:
    log = SessionLog(root, name)
    user = UserSession(user_id=ring.designer.keyid, purpose_tag="basic VAT test")
    statement = DesignerStatement(session_uuid=name, keyid=ring.designer.keyid,
                                  purpose_tag=user.purpose_tag, issued_at=NOW)
    statement.signature = ring.designer.sign(statement.signing_payload())
    environment = EnvSnapshot(assurance_tier=AssuranceTier.SUBPROCESS_WRAPPER,
                              python="3.11", platform="test")
    log.append(VatRecord(record_type=RecordType.SESSION_OPEN, session_uuid=name, seq_no=0,
                         user_session=user, designer_statement=statement,
                         timestamps=Timestamps(stage_start=NOW, stage_stop=NOW, monotonic_ns=1),
                         environment=environment,
                         link=HashLink(sha_prev=GENESIS_PREV, leaf_index=0)))
    return log, user


def test_same_node_stands_alone_and_appends_idempotently_to_multiple_sessions(tmp_path):
    ring, node = signed_node()
    assert verify_node(node, ring.trust_bundle()).passed
    first, user = open_session(tmp_path / "first", ring, "session-one")
    record = append_node(first, node, ring.trust_bundle(), user_session=user)
    assert record.record_type is RecordType.FUNCTION_CALL
    assert record.params.hyperparams["vat_node_id"] == node.node_id
    assert append_node(first, node, ring.trust_bundle(), user_session=user).seq_no == record.seq_no
    assert len(first.records) == 2
    first.seal(ring.recorder, recorder_identity="local basic recorder",
               environment=node.statement().environment, timestamp=NOW + timedelta(seconds=4))
    bundle = build_bundle(first)
    assert verify_bundle(bundle, ring.trust_bundle()).outcome is Outcome.OK
    embedded = next(artifact for artifact in record.intermediates if artifact.kind == "vat_node")
    assert bundle.blobs[embedded.sha256] == node.canonical_bytes()

    second, user = open_session(tmp_path / "second", ring, "session-two")
    other = append_node(second, node, ring.trust_bundle(), user_session=user)
    assert next(a for a in other.intermediates if a.kind == "vat_node").sha256 == embedded.sha256


def test_session_bundle_detects_mutated_embedded_node_and_conflicting_execution(tmp_path):
    ring, node = signed_node()
    log, user = open_session(tmp_path, ring, "session")
    record = append_node(log, node, ring.trust_bundle(), user_session=user)
    log.seal(ring.recorder, recorder_identity="local basic recorder",
             environment=node.statement().environment, timestamp=NOW + timedelta(seconds=4))
    bundle = build_bundle(log)
    embedded = next(artifact for artifact in record.intermediates if artifact.kind == "vat_node")
    bundle.blobs[embedded.sha256] += b"changed"
    assert verify_bundle(bundle, ring.trust_bundle()).outcome is Outcome.TAMPERED

    conflicting_statement, blobs = statement_and_blobs(result=b"different")
    conflicting_statement.execution_id = node.statement().execution_id
    conflicting = VatNode.sign(conflicting_statement, blobs, ring.recorder)
    reopened, user = open_session(tmp_path / "conflict", ring, "conflict")
    append_node(reopened, node, ring.trust_bundle(), user_session=user)
    with pytest.raises(NodeError, match="already bound"):
        append_node(reopened, conflicting, ring.trust_bundle(), user_session=user)


def test_failed_node_has_no_return_value_but_remains_verifiable():
    ring, node = signed_node(status=NodeStatus.FUNCTION_ERROR)
    assert verify_node(node, ring.trust_bundle()).passed
    with pytest.raises(NodeError, match="do not have"):
        node_result_value(node)
