import { expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import EvidenceGraph from './EvidenceGraph';
import { exampleGraph } from './graph-test-data';

function serve() {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
  const graph = exampleGraph();
  const evidence = { id: '00000000-0000-4000-8000-000000000001', graph };
  const api = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith('.zip')) return new Response(new Uint8Array([80, 75]));
    if (url.endsWith('.trust.json')) return Response.json({ entries: [] });
    if (init?.method === 'DELETE') return Response.json({ removed: true });
    if (url.endsWith('/experiment')) {
      const action = JSON.parse(String(init?.body));
      return Response.json({ ...evidence, graph: { ...graph, bundle_report: { ...graph.bundle_report, outcome: action.kind === 'narrative' ? 'OK' : 'INCOMPLETE' } }, description: 'Applied one action' });
    }
    return Response.json(evidence);
  });
  vi.stubGlobal('fetch', api);
  return { api, evidence };
}

it('loads evidence, inspects arbitrary steps, verifies one action, compares and resets', async () => {
  const { api } = serve(); const user = userEvent.setup(); render(<EvidenceGraph />);
  await user.click(screen.getByRole('button', { name: 'clean' }));
  await screen.findByText('VALID');
  await user.click(screen.getByRole('button', { name: /Unfamiliar producer/ }));
  expect(screen.getByRole('region', { name: 'Selected evidence' }).textContent).toContain('Signature: passed');
  await user.click(screen.getByRole('button', { name: 'Falsification' }));
  await user.selectOptions(screen.getByLabelText('Record'), '2');
  await user.click(screen.getByRole('button', { name: 'Apply and verify' }));
  await screen.findByText('INCOMPLETE');
  expect(api.mock.calls.find(([url]) => url.endsWith('/experiment'))?.[1]?.body).toBe('{"kind":"withhold","leaf":2}');
  await user.click(screen.getByRole('button', { name: 'Compare with original' }));
  expect(screen.getByRole('region', { name: 'Original disclosure · same commitment' }).textContent).toContain('OK');
  await user.click(screen.getByRole('button', { name: 'Reset' }));
  expect(screen.queryByText('INCOMPLETE')).toBeNull();
  expect(screen.getByRole('region', { name: 'Loaded disclosure' }).textContent).toContain('VALID');
});

it('discards an old verification response after a file selection changes', async () => {
  const { api, evidence } = serve();
  let finish!: (value: Response) => void;
  const pending = new Promise<Response>(resolve => { finish = resolve; });
  const ordinary = api.getMockImplementation()!;
  api.mockImplementation((url, init) => url === '/api/evidence' ? pending : ordinary(url, init));
  const user = userEvent.setup(); render(<EvidenceGraph />);
  await user.click(screen.getByRole('button', { name: 'clean' }));
  await waitFor(() => expect(api.mock.calls.some(([url]) => url === '/api/evidence')).toBe(true));
  await user.upload(screen.getByLabelText('Evidence ZIP'), new File(['new'], 'new.zip', { type: 'application/zip' }));
  finish(Response.json(evidence));
  await waitFor(() => expect(api.mock.calls.some(([url, init]) => url.endsWith(evidence.id) && init?.method === 'DELETE')).toBe(true));
  expect(screen.queryByText('VALID')).toBeNull();
  expect(screen.queryByText('Checking evidence…')).toBeNull();
});

it('shows a safe failure and never labels an unsuccessful request verified', async () => {
  serve(); vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 503 })));
  const user = userEvent.setup(); render(<EvidenceGraph />);
  await user.click(screen.getByRole('button', { name: 'partial' }));
  expect((await screen.findByRole('alert')).textContent).toContain('unavailable');
  expect(screen.queryByText('VALID')).toBeNull();
});

it('sends the verifier policy for public Sigstore evidence and shows what verified', async () => {
  const { api, evidence } = serve(); const user = userEvent.setup();
  const anchored = { ...evidence, graph: { ...evidence.graph, public_evidence: { claimed_log_index: '1234567890' },
    bundle_report: { ...evidence.graph.bundle_report, notarization: 'simulated (local-null-screen/1)', public_transparency: 'Sigstore Rekor inclusion verified; RFC 3161 publication time verified',
      public_signing_identity: 'name@example-project.iam.gserviceaccount.com (issuer https://accounts.google.com)', rekor_log: 'c0d2 / 1234567890', rfc3161_timestamp: '2026-01-01T00:00:00+00:00 (publication time, not execution time)' } } };
  const ordinary = api.getMockImplementation()!;
  api.mockImplementation(async (url, init) => url === '/api/evidence' ? Response.json(anchored) : url.endsWith('/experiment') ? Response.json({ ...anchored, description: 'Removed the public Sigstore evidence', graph: { ...anchored.graph, bundle_report: { ...anchored.graph.bundle_report, outcome: 'NOT_INCLUDED', public_transparency: 'absent' } } }) : ordinary(url, init));
  render(<EvidenceGraph />);
  await user.upload(screen.getByLabelText('Evidence ZIP'), new File(['PK'], 'anchored.zip', { type: 'application/zip' }));
  await user.upload(screen.getByLabelText('Public trust JSON'), new File(['{"entries": []}'], 'trust.json', { type: 'application/json' }));
  await user.type(screen.getByLabelText('Expected signing identity'), 'name@example-project.iam.gserviceaccount.com');
  await user.upload(screen.getByLabelText('Sigstore trusted root JSON'), new File(['{"tlogs": []}'], 'trusted_root.json', { type: 'application/json' }));
  await user.click(screen.getByLabelText('Require public transparency'));
  await user.click(screen.getByRole('button', { name: 'Open evidence' }));
  await screen.findByText('Rekor log ID / index');
  const body = JSON.parse(String(api.mock.calls.find(([url]) => url === '/api/evidence')?.[1]?.body));
  expect(body.public).toEqual({ require_public_transparency: true, sigstore_identity: 'name@example-project.iam.gserviceaccount.com', sigstore_issuer: null, sigstore_trusted_root: '{"tlogs": []}' });
  expect(screen.getByRole('region', { name: 'Loaded disclosure' }).textContent).toContain('publication time, not execution time');
  await user.click(screen.getByRole('button', { name: 'Falsification' }));
  await user.selectOptions(screen.getByLabelText('Action'), 'strip_public_evidence');
  expect(screen.queryByLabelText('Record')).toBeNull();
  await user.click(screen.getByRole('button', { name: 'Apply and verify' }));
  await screen.findByText('NOT_INCLUDED');
  expect(api.mock.calls.find(([url]) => url.endsWith('/experiment'))?.[1]?.body).toBe('{"kind":"strip_public_evidence"}');
  expect(screen.queryByText('Rekor log ID / index')).toBeNull();
});

it('omits the public policy when no expectation is entered', async () => {
  const { api } = serve(); const user = userEvent.setup(); render(<EvidenceGraph />);
  await user.upload(screen.getByLabelText('Evidence ZIP'), new File(['PK'], 'plain.zip', { type: 'application/zip' }));
  await user.upload(screen.getByLabelText('Public trust JSON'), new File(['{"entries": []}'], 'trust.json', { type: 'application/json' }));
  await user.click(screen.getByRole('button', { name: 'Open evidence' }));
  await screen.findByText('VALID');
  expect(JSON.parse(String(api.mock.calls.find(([url]) => url === '/api/evidence')?.[1]?.body))).not.toHaveProperty('public');
});
