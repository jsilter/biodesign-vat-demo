"""Reference capture uses real computations, signatures and independently checked values."""
import asyncio
import base64
from uuid import uuid4

import pytest

from vatlog import vat, VatRecorder
from vatlog.provenance import InputBinding, OutputSelector, pointer_tokens, select, validate_destinations
from vatlog.node import NodeError, decode_document
from vatlog.roles import Keyring
from vatlog.provenance import verify_provenance
from vatlog.node import VatNode


@vat
def source(value):
    return {"a/b": {"~key": value}, "raw": b"\x00\xff"}


@vat
def combine(values):
    return sum(values)


def test_fork_join_records_actual_producers_and_argument_locations(tmp_path):
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        left = source.with_recorder(recorder).call(7)
        right = source.with_recorder(recorder).call(7)
        assert left.node_id != right.node_id
        result = combine.with_recorder(recorder).call([left.ref('/a~1b/~0key'), right.ref('/a~1b/~0key')])
        assert result.value == 14
        bindings = result.node.statement().input_bindings
        assert [b.destination for b in bindings] == ['/args/0/0', '/args/0/1']
        assert [b.producer_node_id for b in bindings] == [left.node_id, right.node_id]
        assert bindings[0].sha256 == bindings[1].sha256
        statement = result.node.statement()
        call = decode_document(statement.inputs[0], statement.inputs[1:], result.node.raw_blobs())
        assert call == {"args": [[7, 7]], "kwargs": {}}
        assert statement.transport == "local" and statement.modal_call_id is None
        assert len(recorder.evidence_nodes()) == 3
        report = verify_provenance(recorder.evidence_nodes(), recorder.trust)
        assert report.outcome == 'VALID'
        assert len(report.checks) == 2
    assert report.unspecified_inputs == 2  # Only the two literal source arguments.


def test_utf8_artifact_reference_commits_original_bytes_and_decoded_input(tmp_path):
    from vatlog.capture import AdaptedResult, VatOutputArtifact
    from vatlog.local import LocalFunction

    class TextAdapter:
        name, version = "text-artifact", "1"

        def __call__(self, value, context):
            return AdaptedResult((VatOutputArtifact("structure.cif", "coordinates", b"data_test\n#\n"),))

    def produce():
        return {"ok": True}

    def consume(text):
        return text.startswith("data_")

    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        produced = LocalFunction(produce, result_adapter=TextAdapter()).with_recorder(recorder).call()
        consumed = LocalFunction(consume).with_recorder(recorder).call(
            produced.artifact_ref("structure.cif", encoding="utf-8"))
        assert consumed.value is True
        binding, = consumed.node.statement().input_bindings
        assert binding.selector.encoding == "utf-8"
        assert binding.artifact_sha256 == __import__("hashlib").sha256(b"data_test\n#\n").hexdigest()
        assert binding.artifact_bytes_len == len(b"data_test\n#\n")
        assert verify_provenance(recorder.evidence_nodes(), recorder.trust).outcome == "VALID"


@pytest.mark.parametrize('pointer', ['a', '/~', '/~2', '/a~3b'])
def test_invalid_pointer_escape_is_rejected(pointer):
    with pytest.raises(ValueError):
        pointer_tokens(pointer)


@pytest.mark.parametrize('pointer', ['/01', '/-1', '/-', '/2', '/999999999999999'])
def test_array_selector_never_accepts_noncanonical_or_missing_indices(pointer):
    with pytest.raises(ValueError):
        select([1, 2], pointer)


def test_overlapping_bindings_cannot_claim_two_sources_for_one_input():
    def binding(path):
        return InputBinding(destination=path, producer_node_id='a' * 64,
                            selector=OutputSelector(), sha256='b' * 64, bytes_len=1)
    for path in ['/args/0', '/args/0/key']:
        with pytest.raises(ValueError, match='overlap'):
            validate_destinations([binding('/args/0'), binding(path)])
    validate_destinations([binding('/args/0'), binding('/args/01')])


def test_untrusted_source_rejected_before_function_or_execution_preparation(tmp_path):
    with VatRecorder(tmp_path / 'one', Keyring.generate().recorder) as first:
        upstream = source.with_recorder(first).call(3)
    with VatRecorder(tmp_path / 'two', Keyring.generate().recorder) as other:
        with pytest.raises(NodeError, match='producer'):
            combine.with_recorder(other).call([upstream.ref('/a~1b/~0key')])
        assert other.executions() == []


def test_prepared_snapshot_is_frozen_and_repeated_submission_does_not_execute_twice(tmp_path):
    calls = []
    @vat
    def identity(value):
        calls.append(value)
        return value
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        mutable = [1, 2]
        identifier = str(uuid4())
        bound = identity.with_recorder(recorder)
        prepared = bound.prepare_call((mutable,), {}, execution_id=identifier)
        mutable.append(9)
        result = prepared.run()
        assert result.value == [1, 2]
        assert prepared.run().node_id == result.node_id
        assert bound.prepare_call(([1, 2],), {}, execution_id=identifier).run().node_id == result.node_id
        assert calls == [[1, 2]]
        with pytest.raises(NodeError, match='another prepared'):
            bound.prepare_call(([1, 9],), {}, execution_id=identifier)


def test_reference_classification_and_nested_bytes_are_preserved(tmp_path):
    @vat
    def length(value):
        return len(value)
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        upstream = source.with_recorder(recorder).call(1)
        raw = next(a for a in upstream.artifacts if a.name.startswith('return-bytes-'))
        result = length.with_recorder(recorder).call(upstream.artifact_ref(raw.name))
        binding, = result.node.statement().input_bindings
        assert result.value == 2 and binding.artifact_sha256 == raw.sha256
        assert binding.artifact_bytes_len == 2
        assert length.with_recorder(recorder).call(upstream.ref('/raw')).value == 2


def test_async_functions_keep_control_and_data_relationships_separate(tmp_path):
    @vat
    async def double(value):
        await asyncio.sleep(0)
        return value * 2
    async def run(recorder):
        upstream = await double.with_recorder(recorder).call.aio(5)
        next_call = double.with_recorder(recorder).prepare_call((upstream.ref(),), {},
                                                              control_references=[upstream.ref()])
        result = await next_call.run.aio()
        assert result.value == 20
        assert result.node.statement().control_references[0].producer_node_id == upstream.node_id
        assert len(result.node.statement().input_bindings) == 1
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        asyncio.run(run(recorder))


def test_valid_signature_does_not_excuse_a_false_input_binding(tmp_path):
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        upstream = source.with_recorder(recorder).call(7)
        other = source.with_recorder(recorder).call(99)
        result = combine.with_recorder(recorder).call([upstream.ref('/a~1b/~0key')])
        statement = result.node.statement()
        statement.input_bindings[0] = statement.input_bindings[0].model_copy(update={'producer_node_id': other.node_id})
        lie = VatNode.sign(statement, result.node.raw_blobs(), recorder.signer)
        from vatlog.node import verify_node
        assert verify_node(lie, recorder.trust).passed
        report = verify_provenance([upstream.node, other.node, lie], recorder.trust)
        assert report.outcome == 'INVALID'
        assert report.checks[0]['reason'] == 'BINDING_MISMATCH'


def test_missing_producer_is_unresolved_not_an_invented_or_corrupt_execution(tmp_path):
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        upstream = source.with_recorder(recorder).call(7)
        result = combine.with_recorder(recorder).call([upstream.ref('/a~1b/~0key')])
        report = verify_provenance([result.node], recorder.trust)
        assert report.outcome == 'UNRESOLVED'
        assert report.checks[0]['producer_node_id'] == upstream.node_id
        assert report.checks[0]['reason'] == 'PRODUCER_UNAVAILABLE'


def test_prov_export_keeps_equal_outputs_as_distinct_entities(tmp_path):
    from vatlog.prov_export import export_prov
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        a = source.with_recorder(recorder).call(1)
        b = source.with_recorder(recorder).call(1)
        combine.with_recorder(recorder).call([a.ref('/a~1b/~0key'), b.ref('/a~1b/~0key')])
        exported = export_prov(recorder.evidence_nodes())
        activity = next(n for n in exported['@graph'] if n.get('prov:label') == 'combine')
        assert len({entity['@id'] for entity in activity['prov:used']}) == 2
        assert exported == export_prov(list(reversed(recorder.evidence_nodes())))


def test_missing_node_bytes_are_unresolved_but_changed_bytes_are_invalid(tmp_path):
    from vatlog.node import verify_node_signature
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        node = source.with_recorder(recorder).call(1).node
        absent = node.model_copy(deep=True)
        del absent.blobs[next(iter(absent.blobs))]
        assert verify_node_signature(absent, recorder.trust).passed
        assert verify_provenance([absent], recorder.trust).outcome == 'UNRESOLVED'
        corrupt = node.model_copy(deep=True)
        corrupt.blobs[next(iter(corrupt.blobs))] = base64.b64encode(b'changed').decode()
        assert verify_node_signature(corrupt, recorder.trust).passed
        assert verify_provenance([corrupt], recorder.trust).outcome == 'INVALID'


def test_signed_malformed_call_is_invalid_provenance_not_a_verifier_crash(tmp_path):
    from vatlog.node import encode_document, verify_node
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        node = source.with_recorder(recorder).call(1).node
        statement = node.statement()
        document = encode_document({'args': 1, 'kwargs': []}, 'call.json')
        blobs = node.raw_blobs()
        for artifact in statement.inputs:
            blobs.pop(artifact.sha256)
        statement.inputs = [document.artifact, *document.byte_artifacts]
        blobs.update(document.blobs)
        malformed = VatNode.sign(statement, blobs, recorder.signer)
        assert verify_node(malformed, recorder.trust).passed
        report = verify_provenance([malformed], recorder.trust)
        assert report.outcome == 'INVALID'
        assert report.checks[0]['reason'] == 'INVALID_CALL_SHAPE'


def test_intermediate_reference_requires_opt_in_and_keeps_its_classification(tmp_path):
    from vatlog.capture import AdaptedResult, VatOutputArtifact
    class Adapter:
        name, version = 'candidate', '1'
        def __call__(self, value, context):
            return AdaptedResult((VatOutputArtifact('candidate.bin', 'log', b'candidate', 'intermediate'),))
    @vat(result_adapter=Adapter())
    def candidate():
        return None
    @vat
    def count(raw):
        return len(raw)
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        proposed = candidate.with_recorder(recorder).call()
        with pytest.raises(NodeError, match='opt-in'):
            proposed.artifact_ref('candidate.bin')
        result = count.with_recorder(recorder).call(proposed.artifact_ref('candidate.bin', allow_intermediate=True))
        assert result.value == 9
        assert result.node.statement().input_bindings[0].selector.classification == 'intermediate'
        assert verify_provenance(recorder.evidence_nodes(), recorder.trust).outcome == 'VALID'


def test_signed_execution_id_reuse_with_different_contents_is_rejected(tmp_path):
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        a = source.with_recorder(recorder).call(1).node
        b = source.with_recorder(recorder).call(2).node
        statement = b.statement()
        statement.execution_id = a.statement().execution_id
        conflicting = VatNode.sign(statement, b.raw_blobs(), recorder.signer)
        report = verify_provenance([a, conflicting], recorder.trust)
        assert report.outcome == 'INVALID'
        assert any(c['reason'] == 'EXECUTION_CONFLICT' for c in report.checks)
