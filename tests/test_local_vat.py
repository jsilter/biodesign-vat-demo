import asyncio

import pytest

from vatlog import VatRecorder, vat
from vatlog.execution import VatObservationPending
from vatlog.roles import Keyring
from vatlog.session_export import publish_recorded
from vatlog.provenance import bundle_provenance
from vatlog.bundle import Bundle
from vatlog.verify import verify_bundle


def test_real_recording_exports_and_reloads_exact_graph_with_no_cross_scope_leak(tmp_path):
    @vat
    def scale(value):
        return value * 3
    keys = Keyring.generate()
    with VatRecorder(tmp_path / 'journal', keys.recorder) as recorder:
        with recorder.scope('unrelated'):
            hidden = scale.with_recorder(recorder).call(99)
        with recorder.scope('selected'):
            a = scale.with_recorder(recorder).call(2)
            b = scale.with_recorder(recorder).call(a.ref())
        bundle = publish_recorded(recorder, tmp_path / 'export', keys, 'local-demo',
                                  prompt='Test', scope_id='selected')
        assert verify_bundle(bundle, keys.trust_bundle()).exit_code == 0
        report, nodes = bundle_provenance(bundle, keys.trust_bundle())
        assert report.outcome == 'VALID'
        assert {n.node_id for n in nodes} == {a.node_id, b.node_id}
        assert hidden.node_id not in str(bundle.model_dump())
        bundle.save(tmp_path / 'bundle.zip')
        assert bundle_provenance(Bundle.load(tmp_path / 'bundle.zip'), keys.trust_bundle())[0] == report


def test_interrupted_execution_is_not_replayed_and_remains_visible(tmp_path):
    calls = []
    @vat
    def dangerous(value):
        calls.append(value)
        return value
    keys = Keyring.generate()
    with VatRecorder(tmp_path / 'journal', keys.recorder) as recorder:
        prepared = dangerous.with_recorder(recorder).prepare(1)
        recorder.set_state(prepared.execution_id, 'submitting')
        recorder.accepted(prepared.execution_id, 'local:' + prepared.execution_id)
    with VatRecorder(tmp_path / 'journal', keys.recorder) as recorder:
        with pytest.raises(VatObservationPending):
            dangerous.with_recorder(recorder).resume(prepared.execution_id).run()
        assert calls == []
        bundle = publish_recorded(recorder, tmp_path / 'export', keys, 'interrupted', prompt='Test')
        assert verify_bundle(bundle, keys.trust_bundle()).exit_code == 0
        assert bundle_provenance(bundle, keys.trust_bundle())[0].outcome == 'UNRESOLVED'


def test_local_cancellation_preserves_signed_outcome_and_propagates(tmp_path):
    async def scenario(recorder):
        started = asyncio.Event()
        @vat
        async def waiting():
            started.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(waiting.with_recorder(recorder).call.aio())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        entry, = recorder.executions()
        assert entry['cancel_requested'] and entry['state'] == 'completed'
        assert recorder.completed(entry['execution_id']).statement().status.value == 'cancelled'
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        asyncio.run(scenario(recorder))
