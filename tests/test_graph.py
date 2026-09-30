import base64
from dataclasses import asdict

import httpx
import pytest

from runner.api import create_app
from runner.graph import project, falsify
from vatlog import VatRecorder, vat
from vatlog.bundle import Bundle
from vatlog.provenance import bundle_provenance
from vatlog.roles import Keyring
from vatlog.session_export import publish_recorded
from vatlog.verify import verify_bundle


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def evidence(tmp_path):
    @vat
    def unfamiliar_function(value):
        return value + 1
    keys = Keyring.generate()
    with VatRecorder(tmp_path / 'journal', keys.recorder) as recorder:
        a = unfamiliar_function.with_recorder(recorder).call(1)
        b = unfamiliar_function.with_recorder(recorder).call(a.ref())
        bundle = publish_recorded(recorder, tmp_path / 'exports', keys, 'graph-test', prompt='test')
    return bundle, keys.trust_bundle(), a, b


def test_graph_contains_only_recorded_relationships_and_independent_checks(evidence):
    bundle, trust, a, b = evidence
    graph = project(bundle, trust)
    assert graph['bundle_report'] == asdict(verify_bundle(bundle, trust))
    assert graph['provenance_report'] == asdict(bundle_provenance(bundle, trust)[0])
    executions = {n['id']: n for n in graph['nodes'] if n['kind'] == 'execution'}
    assert set(executions) == {a.node_id, b.node_id}
    assert all(n['signature'] == 'passed' for n in executions.values())
    assert len(executions[a.node_id]['leaves']) > 1
    assert len(graph['edges']) == 2
    assert graph['edges'][0]['source'] == a.node_id
    assert graph['edges'][1]['target'] == b.node_id
    assert graph['edges'][1]['label'] == '/args/0'
    assert all(n['membership'] == 'passed' for n in graph['records'])


def test_partial_graph_cannot_invent_hidden_labels_or_attach_producer_to_gap(evidence):
    bundle, trust, a, _ = evidence
    graph = project(bundle, trust)
    node = next(n for n in graph['nodes'] if n['id'] == a.node_id)
    from demo.disclosure import withhold_records
    partial = withhold_records(bundle, set(node['leaves']))
    view = project(partial, trust)
    missing = next(n for n in view['nodes'] if n['id'] == a.node_id)
    assert missing['kind'] == 'missing' and missing['leaves'] == []
    assert 'statement' not in missing
    assert view['missing_count'] == len(node['leaves'])
    assert view['provenance_report']['outcome'] == 'UNRESOLVED'
    assert view['bundle_report']['outcome'] == 'INCOMPLETE'


@pytest.mark.parametrize('kind', ['parameter', 'artifact', 'remove_binding', 'redirect_binding', 'result', 'withhold', 'narrative'])
def test_each_falsification_preserves_original_and_commitments(evidence, kind, tmp_path):
    bundle, trust, _, b = evidence
    original = bundle.model_dump()
    leaf = next(r for r in bundle.records if r.params.hyperparams.get('vat_node_id') == b.node_id)
    action = {'kind': kind, 'leaf': leaf.link.leaf_index, 'value': 'a changed account',
              'digest': leaf.intermediates[0].sha256, 'producer': '0' * 64}
    changed, description = falsify(bundle, action)
    assert description
    assert bundle.model_dump() == original
    assert changed.signed_tree_head == bundle.signed_tree_head
    assert changed.attestation == bundle.attestation
    if kind == 'narrative':
        assert changed.model_dump() == original
    else:
        assert verify_bundle(changed, trust).exit_code != 0
    changed.save(tmp_path / 'changed.zip')
    reloaded = Bundle.load(tmp_path / 'changed.zip')
    assert project(reloaded, trust)['bundle_report'] == project(changed, trust)['bundle_report']


@pytest.mark.anyio
async def test_upload_verify_experiment_reset_download_and_origin_boundary(evidence, tmp_path):
    bundle, trust, _, b = evidence
    path = tmp_path / 'bundle.zip'
    bundle.save(path)
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
        response = await client.post('/api/evidence', json={'archive': base64.b64encode(path.read_bytes()).decode(), 'trust': trust.model_dump(mode='json')})
        assert response.status_code == 200, response.text
        data = response.json()
        identifier = data['id']
        assert data['graph']['provenance_report']['outcome'] == 'VALID'
        leaf = next(r for r in bundle.records if r.params.hyperparams.get('vat_node_id') == b.node_id).link.leaf_index
        altered = await client.post(f'/api/evidence/{identifier}/experiment', json={'kind': 'withhold', 'leaf': leaf})
        assert altered.json()['graph']['bundle_report']['outcome'] == 'INCOMPLETE'
        reset = await client.get(f'/api/evidence/{identifier}')
        assert reset.json() == data
        record = next(r for r in bundle.records if r.link.leaf_index == leaf)
        digest = record.intermediates[0].sha256
        preview = await client.post(f'/api/evidence/{identifier}/artifact', json={'digest': digest})
        assert preview.json()['integrity'] == 'passed'
        altered_preview = await client.post(f'/api/evidence/{identifier}/artifact', json={'digest': digest, 'action': {'kind': 'artifact', 'leaf': leaf, 'digest': digest}})
        assert altered_preview.json()['integrity'] == 'failed'
        assert altered_preview.json()['bytes_len'] > preview.json()['bytes_len']
        hidden_preview = await client.post(f'/api/evidence/{identifier}/artifact', json={'digest': digest, 'action': {'kind': 'withhold', 'leaf': leaf}})
        assert hidden_preview.status_code == 404
        download = await client.post(f'/api/evidence/{identifier}/download', json={'kind': 'withhold', 'leaf': leaf})
        path.write_bytes(download.content)
        assert verify_bundle(Bundle.load(path), trust).outcome.value == 'INCOMPLETE'
        assert (await client.get(f'/api/evidence/{identifier}', headers={'Origin': 'https://evil.example'})).status_code == 403
        assert (await client.get('/api/evidence/etc%2Fpasswd')).status_code in {404, 422}
        assert (await client.post('/api/evidence', json={'archive': 'invalid', 'trust': {}})).status_code == 422
        assert (await client.delete(f'/api/evidence/{identifier}')).status_code == 200
        assert (await client.get(f'/api/evidence/{identifier}')).status_code == 404


def test_invalid_record_does_not_hide_independent_signatures_and_memberships(evidence):
    bundle, trust, a, _ = evidence
    changed, _ = falsify(bundle, {'kind': 'parameter', 'leaf': 1})
    graph = project(changed, trust)
    assert graph['bundle_report']['outcome'] == 'TAMPERED'
    assert graph['commitment'] == 'passed'
    assert next(n for n in graph['nodes'] if n['id'] == a.node_id)['signature'] == 'passed'
    assert next(r for r in graph['records'] if r['index'] == 1)['membership'] == 'failed'
    assert next(r for r in graph['records'] if r['index'] == 2)['membership'] == 'passed'


def test_reordering_disclosed_records_does_not_change_lifecycle_result(evidence):
    bundle, trust, _, _ = evidence
    expected = bundle_provenance(bundle, trust)[0]
    bundle.records.reverse()
    assert bundle_provenance(bundle, trust)[0] == expected


def test_bad_lifecycle_claim_returns_failure_instead_of_crashing(evidence):
    bundle, trust, _, _ = evidence
    bundle.records[1].params.hyperparams['execution_id'] = ['not', 'an', 'identity']
    report, _ = bundle_provenance(bundle, trust)
    assert report.outcome == 'INVALID'
    assert any(c['reason'] == 'INVALID_LIFECYCLE' for c in report.checks)


def test_duplicate_positions_never_receive_a_passed_membership(evidence):
    bundle, trust, _, _ = evidence
    bundle.records.append(bundle.records[1].model_copy(deep=True))
    graph = project(bundle, trust)
    assert all(r['membership'] == 'failed' for r in graph['records'] if r['index'] == 1)


def test_missing_terminal_node_cannot_be_excused_by_completed_lifecycle(evidence):
    from demo.disclosure import withhold_records
    bundle, trust, a, _ = evidence
    leaf = next(r.link.leaf_index for r in bundle.records if r.params.hyperparams.get('vat_node_id') == a.node_id)
    partial = withhold_records(bundle, {leaf})
    assert bundle_provenance(partial, trust)[0].outcome == 'UNRESOLVED'


def test_cli_report_exit_codes_and_prov_projection_match_real_bundle(evidence, tmp_path, capsys):
    import json
    from vatlog.cli import main
    from vatlog.prov_export import export_prov
    from demo.disclosure import withhold_records
    bundle, trust, a, _ = evidence
    path, trust_path = tmp_path / 'bundle.zip', tmp_path / 'trust.json'
    trust.save(trust_path)
    bundle.save(path)
    args = ['provenance', str(path), '--trust', str(trust_path), '--prov']
    assert main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['outcome'] == 'VALID'
    assert output['prov'] == export_prov(bundle_provenance(bundle, trust)[1])
    leaf = next(r.link.leaf_index for r in bundle.records if r.params.hyperparams.get('vat_node_id') == a.node_id)
    withhold_records(bundle, {leaf}).save(path)
    assert main(args) == 3
    assert json.loads(capsys.readouterr().out)['outcome'] == 'UNRESOLVED'


def test_published_schema_matches_execution_statement():
    import json
    from pathlib import Path
    from vatlog.node import ExecutionStatement
    schema = json.loads((Path(__file__).parents[1] / 'docs/schemas/vat-prov-1.schema.json').read_text())
    assert schema == ExecutionStatement.model_json_schema()
