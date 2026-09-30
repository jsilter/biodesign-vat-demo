import { execFileSync } from 'node:child_process';
import { expect, it, vi } from 'vitest';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from './App';
import { fixtureCatalog, serveCatalog } from './test-data';

async function open(data = fixtureCatalog()) {
  serveCatalog(data);
  const user = userEvent.setup();
  const rendered = render(<App />);
  await screen.findByRole('tab', { name: /designer/ });
  return { user, ...rendered };
}

it('shows local transparency evidence and its observation limits', async () => {
  const { user } = await open();
  await user.click(screen.getByRole('tab', { name: /notary/ }));
  expect(screen.getByText(/All four local signers/)).toBeTruthy();
  expect(screen.getByText(/Log entry 0 · checkpoint size 1/)).toBeTruthy();
  expect(screen.getByText(/previously saved checkpoint/)).toBeTruthy();
});

it('keeps the live 1. Enter your question across role and fixture navigation', async () => {
  const { user } = await open();
  await user.type(screen.getByLabelText('1. Enter your question'), 'Persistent request');
  await user.click(screen.getByRole('tab', { name: /recorder/ }));
  expect(screen.queryByRole('region', { name: 'Live Designer workflow' })).toBeNull();
  await user.click(screen.getByRole('button', { name: /Conflicting objectives/ }));
  await user.click(screen.getByRole('tab', { name: /designer/ }));
  expect((screen.getByLabelText('1. Enter your question') as HTMLTextAreaElement).value).toBe('Persistent request');
});

it('switches between research and protein design without losing either request', async () => {
  const { user } = await open();
  await user.type(screen.getByLabelText('1. Enter your question'), 'Keep the research request');
  await user.click(screen.getByRole('button', { name: 'Open protein design' }));
  expect(screen.queryByRole('region', { name: 'Live Designer workflow' })).toBeNull();
  await user.type(screen.getByLabelText('Describe your objective'), 'Keep the design objective');
  await user.click(screen.getByRole('button', { name: 'Return to research' }));
  expect((screen.getByLabelText('1. Enter your question') as HTMLTextAreaElement).value).toBe('Keep the research request');
  await user.click(screen.getByRole('button', { name: 'Open protein design' }));
  expect((screen.getByLabelText('Describe your objective') as HTMLTextAreaElement).value).toBe('Keep the design objective');
});

it.each([
  ['Complete session', 'OK', 5, 0], ['Altered record', 'TAMPERED', 0, 4],
  ['Conflicting objectives', 'OK', 5, 0], ['Selective disclosure', 'INCOMPLETE', 4, 0],
])('shows real checks for %s', async (name, outcome, passes, notRun) => {
  const { user } = await open();
  await user.click(screen.getByRole('button', { name: new RegExp(String(name)) }));
  await user.click(screen.getByRole('tab', { name: /verifier/ }));
  const panel = screen.getByRole('tabpanel');
  expect(within(panel).getByText(new RegExp(`${outcome} · exit`))).toBeTruthy();
  expect(panel.querySelectorAll('.checks .passed')).toHaveLength(Number(passes));
  expect(panel.querySelectorAll('.checks .not-run')).toHaveLength(Number(notRun));
  expect(panel.querySelectorAll('.checks li')).toHaveLength(5);
  expect(within(panel).getByText('Project extension')).toBeTruthy();
  if (name === 'Conflicting objectives') expect(within(panel).getByText(/curated reviewer finding/)).toBeTruthy();
});

it('preserves role but resets record, evidence and download paths between scenarios', async () => {
  const data = fixtureCatalog();
  const { user } = await open(data);
  await user.click(screen.getByRole('button', { name: /Conflicting objectives/ }));
  await user.click(screen.getByRole('button', { name: 'Record 6: disclosed, inspect' }));
  expect(screen.getByRole('region', { name: 'Record 6 details' })).toBeTruthy();
  await user.click(screen.getByRole('tab', { name: /designer/ }));
  await user.click(screen.getByRole('tab', { name: /recorder/ }));
  expect(screen.getByRole('region', { name: 'Record 6 details' })).toBeTruthy();
  await user.click(screen.getByRole('button', { name: /Selective disclosure/ }));
  expect(screen.getByRole('tab', { name: /recorder/ }).getAttribute('aria-selected')).toBe('true');
  expect(screen.queryByRole('region', { name: 'Record 6 details' })).toBeNull();
  expect(screen.getByRole('region', { name: 'Record 5 details' })).toBeTruthy();
  expect(screen.queryByText(data.scenarios[2].bundle.session_uuid)).toBeNull();
  expect(screen.getByRole('link', { name: /Download bundle/ }).getAttribute('href')).toBe(`${import.meta.env.BASE_URL}demo/partial.zip`);
  expect(screen.getByRole('link', { name: /Public trust/ }).getAttribute('href')).toBe(`${import.meta.env.BASE_URL}demo/partial.trust.json`);
  expect(screen.getByRole('link', { name: /Sequence/ }).getAttribute('href')).toBe(`${import.meta.env.BASE_URL}demo/partial.sequence.txt`);
  expect((screen.getByRole('button', { name: 'Record 0: not disclosed, type unknown' }) as HTMLButtonElement).disabled).toBe(true);
  await user.click(screen.getByRole('tab', { name: /designer/ }));
  expect(screen.getByText('Unknown: opening leaf not disclosed')).toBeTruthy();
  expect(screen.getByText('unverified')).toBeTruthy();
  await user.click(screen.getByRole('button', { name: /Complete session/ }));
  await user.click(screen.getByRole('tab', { name: /recorder/ }));
  expect(screen.getByRole('region', { name: 'Record 0 details' })).toBeTruthy();
});

it('preserves the Python report line breaks and literal evidence text', async () => {
  const data = fixtureCatalog();
  data.scenarios[0].report_text += '\n<script>literal evidence</script>';
  const { user } = await open(data);
  await user.click(screen.getByRole('tab', { name: /verifier/ }));
  const summary = screen.getByText('Inspect full Python verification report');
  await user.click(summary);
  const report = summary.parentElement!.querySelector('pre')!;
  expect(report.textContent).toBe(data.scenarios[0].report_text);
  expect(report.textContent!.split('\n').length).toBeGreaterThan(5);
  expect(report.querySelector('script')).toBeNull();
});

it('never treats the original receipt claim as current tampered-bundle success', async () => {
  const { user } = await open();
  await user.click(screen.getByRole('button', { name: /Altered record/ }));
  await user.click(screen.getByRole('tab', { name: /notary/ }));
  expect(screen.getByText('Not run')).toBeTruthy();
  expect(screen.getByText(/passed · full original chain/)).toBeTruthy();
  expect(screen.queryByText('Verified under supplied local trust')).toBeNull();
});

it('does not blame a valid original receipt when the recorder signature is corrupted', async () => {
  const data = fixtureCatalog();
  const report = execFileSync('../.venv/bin/python', ['-c', `
import json
from dataclasses import asdict
from pathlib import Path
from vatlog.bundle import Bundle
from vatlog.roles import TrustBundle
from vatlog.verify import verify_bundle
b = Bundle.load(Path('public/demo/clean.zip'))
b.signed_tree_head.signature = 'AA=='
r = verify_bundle(b, TrustBundle.load(Path('public/demo/clean.trust.json')), submitted_sequence=Path('public/demo/clean.sequence.txt').read_text(), require_transparency=True)
print(json.dumps({**asdict(r), 'exit_code': r.exit_code}))
`], { env: { ...process.env, PYTHONPATH: '../src' }, encoding: 'utf8' });
  data.scenarios[0].bundle.signed_tree_head.signature = 'AA==';
  data.scenarios[0].report = JSON.parse(report);
  const { user } = await open(data);
  await user.click(screen.getByRole('tab', { name: /notary/ }));
  expect(screen.getByText('Receipt verification not established')).toBeTruthy();
  expect(screen.queryByText('Receipt failed')).toBeNull();
});

it('renders hostile-looking evidence literally and gates shared-digest previews by kind', async () => {
  const data = fixtureCatalog();
  const s = data.scenarios[2];
  const opening = s.bundle.records[0];
  const ref = opening.inputs[0];
  const text = '<img src=x onerror="alert(1)"><script>attack()</script>';
  s.artifacts[ref.sha256].text = text;
  opening.user_session.purpose_tag = text;
  opening.inputs.push({ ...ref, kind: 'log', name: 'unsupported alias' });
  const { user } = await open(data);
  await user.click(screen.getByRole('button', { name: /Conflicting objectives/ }));
  await user.click(screen.getByRole('button', { name: 'Record 0: disclosed, inspect' }));
  const inspector = screen.getByRole('region', { name: 'Record 0 details' });
  const artifacts = inspector.querySelectorAll('.artifact');
  expect(artifacts[0].querySelector('pre')?.textContent).toBe(text);
  expect(artifacts[1].querySelector('pre')).toBeNull();
  expect(inspector.querySelectorAll('img,script')).toHaveLength(0);
  expect([...document.querySelectorAll('a')].every(a => !a.href.includes('alert'))).toBe(true);
});

it.each(['withheld', 'unpreviewable'])('distinguishes %s instructions from absent or unknown', async state => {
  const data = fixtureCatalog();
  const s = data.scenarios[2];
  const ref = s.bundle.records[0].inputs[0];
  s.artifacts[ref.sha256] = { available: state !== 'withheld', text: null };
  const { user } = await open(data);
  await user.click(screen.getByRole('button', { name: /Conflicting objectives/ }));
  expect(screen.getByText(state === 'withheld' ? 'Bytes withheld (all or some)' : 'Bytes available')).toBeTruthy();
  expect(screen.getByText('No text preview.')).toBeTruthy();
});

it('supports keyboard tab navigation, selection and associated panels', async () => {
  const { user } = await open();
  const designer = screen.getByRole('tab', { name: /designer/ });
  designer.focus();
  await user.keyboard('{ArrowRight}');
  expect(document.activeElement).toBe(screen.getByRole('tab', { name: /recorder/ }));
  await user.keyboard('{End}');
  expect(document.activeElement).toBe(screen.getByRole('tab', { name: /verifier/ }));
  expect(screen.getByRole('tabpanel').getAttribute('aria-labelledby')).toBe('tab-verifier');
  await user.keyboard('{ArrowRight}');
  expect(document.activeElement).toBe(designer);
  await user.keyboard('{ArrowLeft}{Home}{Tab}');
  expect(document.activeElement).toBe(screen.getByRole('tabpanel'));
});

it.each(['missing', 'json', 'shape'])('shows an error, not success, for %s catalog', async kind => {
  vi.stubGlobal('fetch', vi.fn(async () => kind === 'missing' ? new Response('', { status: 404 }) : new Response(kind === 'json' ? '{' : '{}')));
  render(<App />);
  expect(await screen.findByRole('alert')).toBeTruthy();
  expect(screen.queryByRole('tabpanel')).toBeNull();
  expect(screen.queryByRole('link', { name: /Download bundle/ })).toBeNull();
});

it('retries failures and then replaces the error with fresh evidence', async () => {
  const fetch = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(new Response('{'))
    .mockImplementation(async (path: string) => new Response(JSON.stringify(path === '/api/models'
      ? { models: [{ id: 'openai/test', name: 'OpenAI test' }], configured: false } : fixtureCatalog())));
  vi.stubGlobal('fetch', fetch);
  const user = userEvent.setup(); render(<App />);
  await screen.findByRole('alert');
  await user.click(screen.getByRole('button', { name: 'Retry loading' }));
  await screen.findByRole('alert');
  await user.click(screen.getByRole('button', { name: 'Retry loading' }));
  await screen.findByRole('tabpanel');
  expect(screen.queryByRole('alert')).toBeNull();
});

it('aborts an outstanding request on unmount', async () => {
  let signal: AbortSignal | undefined;
  vi.stubGlobal('fetch', vi.fn((_url, options) => { signal = options.signal; return new Promise(() => {}); }));
  const { unmount } = render(<App />);
  await waitFor(() => expect(signal).toBeDefined());
  unmount(); expect(signal?.aborted).toBe(true);
});

it('keeps loaded evidence when the presenter returns from the research tab', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
  const { user } = await open();
  const catalog = fixtureCatalog();
  const { exampleGraph } = await import('./graph-test-data');
  const evidence = { id: '00000000-0000-4000-8000-000000000002', graph: exampleGraph() };
  const api = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith('.zip')) return new Response(new Uint8Array([80, 75]));
    if (url.endsWith('.trust.json')) return Response.json({ entries: [] });
    if (url === '/api/evidence') return Response.json(evidence);
    if (init?.method === 'DELETE') return Response.json({ removed: true });
    return new Response(JSON.stringify(url === '/api/models' ? { models: [], configured: false } : catalog));
  });
  vi.stubGlobal('fetch', api);
  await user.click(screen.getByRole('button', { name: 'Evidence graph' }));
  await user.click(await screen.findByRole('button', { name: 'clean' }));
  await screen.findByText('VALID');
  await user.click(screen.getByRole('button', { name: 'Research & examples' }));
  expect(screen.getByText('VALID').closest('[hidden]')).not.toBeNull();
  await user.click(screen.getByRole('button', { name: 'Evidence graph' }));
  expect(screen.getByText('VALID').closest('[hidden]')).toBeNull();
  expect(api.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(false);
});
