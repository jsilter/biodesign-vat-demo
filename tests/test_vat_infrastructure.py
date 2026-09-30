"""Provider-independent VAT contracts: actual computation, storage and cryptography.

This module deliberately does not import Modal or scientific adapters.
"""
import base64
import hashlib
import json
import random
import subprocess
import sys
import zlib
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from vatlog.attest import pae
from vatlog.bundle import Bundle, build_bundle
from vatlog.canon import canonical_bytes, sha256_hex
from vatlog.execution import VatRecorder, VatStateError
from vatlog.node import (ExecutionStatement, FunctionIdentity, NodeError, NodeOutcome,
                         VatLimits, VatNode, decode_document, encode_document, verify_node)
from vatlog.result import VatResult
from vatlog.roles import Keyring, Role, TrustBundle, TrustEntry
from vatlog.schema import AssuranceTier, EnvSnapshot, Timestamps
from vatlog.verify import verify_bundle
from test_vat_node import open_session, signed_node


def record_computation(recorder, function, *args, **kwargs):
    """Exercise the provider-independent journal with locally computed inputs/outputs."""
    identity = FunctionIdentity(app_name="independent-test", function_name=function.__name__)
    call = encode_document({"args": list(args), "kwargs": kwargs}, "call.json")
    id = recorder.begin(identity, call)
    recorder.set_state(id, "submitting")
    recorder.accepted(id, "local-test-" + id)
    request, _, call_id, _ = recorder.request(id)
    value = function(*args, **kwargs)
    stopped = datetime.now(timezone.utc)
    recorder.observe(id, {"status": "succeeded", "value": value, "error": None,
                          "observed_at": stopped.isoformat(), "monotonic_ns": 1})
    returned = encode_document(value, "return.json")
    statement = ExecutionStatement(
        execution_id=id, function=identity, modal_call_id=call_id, status="succeeded",
        dispatched_at=request.dispatched_at, recorded_at=stopped,
        timestamps=Timestamps(stage_start=request.dispatched_at, stage_stop=stopped, monotonic_ns=1),
        inputs=request.inputs, results=[returned.artifact, *returned.byte_artifacts],
        environment=EnvSnapshot(assurance_tier=AssuranceTier.SUBPROCESS_WRAPPER,
                                python="test", platform="local test", modal_call_id=call_id))
    node = VatNode.sign(statement, {**call.blobs, **returned.blobs}, recorder.signer)
    recorder.complete(id, node)
    return VatResult(node, recorder.trust)


def compress_bytes(raw):
    return {"compressed": zlib.compress(raw), "sha256": hashlib.sha256(raw).hexdigest(), "length": len(raw)}


@pytest.mark.parametrize("corruption", [b"not json", b"{}", "noncanonical", "oversized"])
def test_corrupted_dispatch_request_is_rejected_before_use(tmp_path, corruption):
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        id = recorder.begin(FunctionIdentity(app_name="local", function_name="sum"),
                            encode_document({"args": [[1, 2]], "kwargs": {}}, "call.json"))
        if corruption == "noncanonical":
            raw = recorder.db.execute("SELECT request FROM executions WHERE id=?", (id,)).fetchone()[0]
            corruption = json.dumps(json.loads(raw), indent=2).encode()
        elif corruption == "oversized":
            corruption = b" " * (recorder.limits.max_node_bytes + 1)
        with recorder.db:
            recorder.db.execute("UPDATE executions SET request=? WHERE id=?", (corruption, id))
        with pytest.raises(VatStateError) as error:
            recorder.request(id)
        assert error.value.code == "JOURNAL_CORRUPT"


def test_missing_execution_cannot_be_read_cancelled_or_accepted(tmp_path):
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        id = str(uuid4())
        for operation in (lambda: recorder.request(id), lambda: recorder.request_cancel(id)):
            with pytest.raises(VatStateError) as error:
                operation()
            assert error.value.code == "EXECUTION_NOT_FOUND"
        with pytest.raises(VatStateError) as error:
            recorder.accepted(id, "fc-unknown")
        assert error.value.code == "REQUEST_CONFLICT"
        assert recorder.executions() == []


def matrix_product(left, right):
    return [[sum(a * b for a, b in zip(row, column)) for column in zip(*right)] for row in left]


@pytest.mark.parametrize("function,args,expected", [
    (sum, ([3, -2, 11],), 12),
    (sorted, ([9, 1, 8, -4],), [-4, 1, 8, 9]),
    (matrix_product, ([[1, 2], [3, 4]], [[5, 6], [7, 8]]), [[19, 22], [43, 50]]),
    (str.casefold, ("Straße",), "strasse"),
])
def test_general_computation_survives_restart_and_independent_bundle_verification(tmp_path, function, args, expected):
    ring = Keyring.generate()
    with VatRecorder(tmp_path / "journal", ring.recorder) as recorder:
        result = record_computation(recorder, function, *args)
        assert result.value == expected
        raw = result.node.canonical_bytes()
        id = result.execution_id
    with VatRecorder(tmp_path / "journal", ring.recorder) as restarted:
        loaded = VatResult(restarted.completed(id), restarted.trust)
        assert loaded.node.canonical_bytes() == raw
        assert loaded.value == expected
        log, user = open_session(tmp_path / "session", ring, str(uuid4()))
        first = loaded.append_to(log, user_session=user)
        assert loaded.append_to(log, user_session=user).seq_no == first.seq_no
        log.seal(ring.recorder, recorder_identity="independent local recorder", environment=first.environment)
        path = tmp_path / "audit.zip"
        build_bundle(log).save(path)
        bundle = Bundle.load(path)
        assert verify_bundle(bundle, ring.trust_bundle()).exit_code == 0
        embedded, = [a for record in bundle.records for a in record.intermediates if a.kind == "vat_node"]
        assert bundle.blobs[embedded.sha256] == raw
        assert verify_node(VatNode.model_validate_json(raw), ring.trust_bundle()).passed


def test_binary_computation_preserves_exact_input_and_output_bytes(tmp_path):
    raw = bytes(range(256)) * 3 + b"\x00\xff\r\n"
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        result = record_computation(recorder, compress_bytes, raw)
        assert zlib.decompress(result.value["compressed"]) == raw
        assert result.value["sha256"] == hashlib.sha256(raw).hexdigest()
        statement, blobs = result.node.statement(), result.node.raw_blobs()
        inputs = decode_document(statement.inputs[0], statement.inputs[1:], blobs)
        assert inputs == {"args": [raw], "kwargs": {}}
        assert next(a for a in result.artifacts if a.name.startswith("return-bytes-")).read_bytes() == result.value["compressed"]


def test_provider_independent_core_works_with_modal_import_blocked():
    code = '''
import sys
sys.modules["modal"] = None
from vatlog.node import encode_document, decode_document
from vatlog import VatRecorder, VatResult
document = encode_document({"answer": sum(range(11)), "bytes": b"\\x00\\xff"}, "return.json")
assert decode_document(document.artifact, list(document.byte_artifacts), document.blobs) == {"answer": 55, "bytes": b"\\x00\\xff"}
'''
    completed = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr


def test_seeded_nested_codec_values_preserve_types_bytes_and_order_independence():
    rng = random.Random(601)
    atoms = [None, True, False, 0, -(2**53 - 1), 2**53 - 1, 1.5, "é", "e\u0301", "\x00", b"", b"\x00\xff"]
    def value(depth):
        if depth == 0 or rng.randrange(3) == 0:
            return rng.choice(atoms)
        if rng.randrange(2):
            return [value(depth - 1) for _ in range(rng.randrange(4))]
        return {f"key-{i}": value(depth - 1) for i in range(rng.randrange(4))}
    for _ in range(200):
        original = value(5)
        document = encode_document(original, "return.json")
        actual = decode_document(document.artifact, list(document.byte_artifacts), document.blobs)
        assert actual == original and type(actual) is type(original)
        assert encode_document(actual, "return.json").blobs == document.blobs
        if isinstance(original, dict):
            reordered = dict(reversed(list(original.items())))
            assert encode_document(reordered, "return.json").artifact.sha256 == document.artifact.sha256


@pytest.mark.parametrize("mutation", ["missing_blob", "extra_blob", "unsafe_name", "duplicate_name", "backward_time", "reserved_lineage"])
def test_even_recorder_signed_invalid_statements_are_rejected(mutation):
    ring, node = signed_node()
    payload = node.statement().model_dump(mode="json")
    if mutation == "missing_blob":
        node.blobs.pop(next(iter(node.blobs)))
    elif mutation == "extra_blob":
        node.blobs[sha256_hex(b"undeclared")] = base64.b64encode(b"undeclared").decode()
    elif mutation == "unsafe_name":
        payload["results"][-1]["name"] = "../outside"
    elif mutation == "duplicate_name":
        payload["results"][-1]["name"] = "return.json"
    elif mutation == "backward_time":
        payload["recorded_at"] = "2000-01-01T00:00:00Z"
    else:
        payload["input_bindings"] = [{"node_id": "0" * 64}]
    raw = canonical_bytes(payload)
    node.payload = base64.b64encode(raw).decode()
    node.signatures[0].sig = ring.recorder.sign(pae(node.payload_type, raw))
    assert verify_node(node, ring.trust_bundle()).outcome is NodeOutcome.TAMPERED
    with pytest.raises(NodeError):
        VatResult(node, ring.trust_bundle())


@pytest.mark.parametrize("role", [Role.DESIGNER, Role.NOTARY])
def test_valid_cryptographic_signature_without_recorder_role_is_not_trusted(role):
    ring, node = signed_node()
    wrong_role = TrustBundle(entries=[TrustEntry(
        keyid=ring.recorder.keyid, role=role,
        public_key=TrustEntry.from_signer(ring.recorder).public_key)])
    assert verify_node(node, wrong_role).outcome is NodeOutcome.BAD_SIGNATURE
    with pytest.raises(NodeError):
        VatResult(node, wrong_role)


@pytest.mark.parametrize("source", ["prepared", "submitting", "submission_uncertain", "running", "observation_pending", "completed"])
@pytest.mark.parametrize("destination", ["prepared", "submitting", "submission_uncertain", "running", "observation_pending", "completed"])
def test_journal_transition_matrix_never_reopens_completed_or_uncertain_work(tmp_path, source, destination):
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        if source == "completed":
            id = record_computation(recorder, sum, [1, 2]).execution_id
        else:
            id = recorder.begin(FunctionIdentity(app_name="test", function_name="sum"),
                                encode_document({"args": [[1, 2]], "kwargs": {}}, "call.json"))
            if source != "prepared":
                recorder.set_state(id, "submitting")
            if source == "submission_uncertain":
                recorder.set_state(id, source)
            elif source in {"running", "observation_pending"}:
                recorder.accepted(id, "local-test")
                if source == "observation_pending":
                    recorder.set_state(id, source)
        allowed = {("prepared", "submitting"), ("submitting", "submission_uncertain"),
                   ("running", "observation_pending"), ("observation_pending", "running")}
        if source == destination or (source, destination) in allowed:
            recorder.set_state(id, destination)
            assert recorder.request(id)[1] == destination
        else:
            with pytest.raises(VatStateError) as error:
                recorder.set_state(id, destination)
            assert error.value.code == "STATE_CONFLICT"
            assert recorder.request(id)[1] == source


def test_corrupt_persisted_observation_is_not_returned_after_restart(tmp_path):
    ring = Keyring.generate()
    with VatRecorder(tmp_path, ring.recorder) as recorder:
        id = record_computation(recorder, sum, [1, 2]).execution_id
        original = recorder.db.execute("SELECT data FROM observations WHERE id=?", (id,)).fetchone()[0]
        data = json.loads(original)
        digest = data["artifact"]["sha256"]
        data["blobs"][digest] = base64.b64encode(b"changed").decode()
        with recorder.db:
            recorder.db.execute("UPDATE observations SET data=? WHERE id=?", (canonical_bytes(data), id))
    with VatRecorder(tmp_path, ring.recorder) as recorder:
        with pytest.raises(VatStateError, match="validation"):
            recorder.observation(id)


def test_atomic_export_failure_keeps_previous_verified_file(tmp_path, monkeypatch):
    import vatlog.node as node_module
    ring, node = signed_node()
    path = tmp_path / "node.json"
    node.save(path)
    original = path.read_bytes()
    def unavailable(*_args):
        raise OSError("simulated failed rename")
    monkeypatch.setattr(node_module.os, "replace", unavailable)
    with pytest.raises(OSError):
        node.save(path)
    assert path.read_bytes() == original
    assert verify_node(VatNode.load(path), ring.trust_bundle()).passed
    assert sorted(p.name for p in tmp_path.iterdir()) == ["node.json"]


@pytest.mark.parametrize("limits", [VatLimits(max_blob_bytes=2), VatLimits(max_total_blob_bytes=2),
                                    VatLimits(max_artifacts=1)])
def test_decoder_enforces_caller_blob_and_artifact_budgets(limits):
    document = encode_document([b"abc", b"def"], "return.json")
    with pytest.raises(NodeError) as error:
        decode_document(document.artifact, list(document.byte_artifacts), document.blobs, limits=limits)
    assert error.value.code == "LIMIT_EXCEEDED"


def test_decoder_accepts_exact_budgets_and_deduplicates_repeated_bytes():
    value = [b"x" * 1024, b"x" * 1024, b"y" * 1024]
    document = encode_document(value, "return.json")
    total = sum(map(len, document.blobs.values()))
    limits = VatLimits(max_blob_bytes=1024, max_total_blob_bytes=total, max_artifacts=3)
    assert decode_document(document.artifact, list(document.byte_artifacts), document.blobs, limits=limits) == value
    for smaller in (VatLimits(max_blob_bytes=1023), VatLimits(max_total_blob_bytes=total - 1), VatLimits(max_artifacts=2)):
        with pytest.raises(NodeError) as error:
            decode_document(document.artifact, list(document.byte_artifacts), document.blobs, limits=smaller)
        assert error.value.code == "LIMIT_EXCEEDED"


@pytest.mark.parametrize("mutate", ["invalid_tag_version", "boolean_length", "negative_length", "wrong_digest", "undeclared_digest"])
def test_tagged_byte_references_cannot_bypass_integrity_validation(mutate):
    document = encode_document(b"binary", "return.json")
    tag = json.loads(document.blobs[document.artifact.sha256])
    if mutate == "invalid_tag_version":
        tag["$vat"] = "bytes/2"
    elif mutate == "boolean_length":
        tag["bytes_len"] = True
    elif mutate == "negative_length":
        tag["bytes_len"] = -1
    else:
        tag["sha256"] = "0" * 64
    raw = canonical_bytes(tag)
    main = document.artifact.model_copy(update={"sha256": sha256_hex(raw), "bytes_len": len(raw)})
    blobs = {**document.blobs, main.sha256: raw}
    if mutate == "undeclared_digest":
        blobs[tag["sha256"]] = b"binary"
    with pytest.raises(NodeError):
        decode_document(main, list(document.byte_artifacts), blobs)


@pytest.mark.parametrize("call_id", ["", "x" * 257, 42, None])
def test_invalid_acceptance_identity_does_not_advance_journal(tmp_path, call_id):
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        id = recorder.begin(FunctionIdentity(app_name="test", function_name="sum"),
                            encode_document({"args": [[1, 2]], "kwargs": {}}, "call.json"))
        recorder.set_state(id, "submitting")
        with pytest.raises(VatStateError):
            recorder.accepted(id, call_id)
        assert recorder.request(id)[1:3] == ("submitting", None)
        assert recorder.completed(id) is None


def test_acceptance_recovery_is_idempotent_but_never_changes_known_identity(tmp_path):
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        id = recorder.begin(FunctionIdentity(app_name="test", function_name="sum"),
                            encode_document({"args": [[1, 2]], "kwargs": {}}, "call.json"))
        with pytest.raises(VatStateError):
            recorder.recovered_acceptance(id, "fc-unsubmitted")
        recorder.set_state(id, "submitting")
        recorder.set_state(id, "submission_uncertain")
        recorder.recovered_acceptance(id, "fc-accepted")
        recorder.recovered_acceptance(id, "fc-accepted")
        with pytest.raises(VatStateError) as error:
            recorder.recovered_acceptance(id, "fc-different")
        assert error.value.code == "REQUEST_CONFLICT"
        assert recorder.request(id)[1:3] == ("running", "fc-accepted")


@pytest.mark.parametrize("target", ["database", "execution_directory", "node_file"])
def test_recorder_rejects_symlinked_storage_without_modifying_target(tmp_path, target):
    root = tmp_path / "journal"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"untouched")
    ring = Keyring.generate()
    if target == "database":
        (root / "state.sqlite").symlink_to(sentinel)
        with pytest.raises(VatStateError, match="symlink"):
            VatRecorder(root, ring.recorder)
    else:
        with VatRecorder(root, ring.recorder) as recorder:
            result = record_computation(recorder, sum, [1, 2])
            path = root / result.execution_id
            if target == "execution_directory":
                path.rename(root / "original-execution")
                path.symlink_to(outside, target_is_directory=True)
            else:
                (path / "node.json").rename(path / "original-node.json")
                (path / "node.json").symlink_to(sentinel)
            with pytest.raises(VatStateError, match="symlink"):
                recorder.completed(result.execution_id)
    assert sentinel.read_bytes() == b"untouched"
