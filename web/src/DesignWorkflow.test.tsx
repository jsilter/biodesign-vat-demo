import { expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DesignWorkflow } from './DesignWorkflow';

const id = '12345678-1234-4234-8234-123456789012';
const state = { id, status: 'running', logs: [{ at: 'now', message: 'Submitting boltzgen' }], text: '', record_count: 1, artifacts: [], candidates: [] };
function serve(configured = true, result: unknown = state) {
  const calls: { path: string; body: unknown }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (path: string, options?: RequestInit) => {
    calls.push({ path, body: options?.body ? JSON.parse(String(options.body)) : undefined });
    if (path === '/api/design/config') return Response.json({ configured, tools: [
      { id: 'boltzgen', name: 'BoltzGen', standalone: true, implemented: true, configured, input_role: 'target → candidates', readiness: 'offline_ready', licence: 'open_source' },
      { id: 'proteinmpnn', name: 'ProteinMPNN v_48_020', standalone: true, implemented: true, configured, input_role: 'backbone → sequences', readiness: 'offline_ready', licence: 'open_source' },
      { id: 'esmfold2', name: 'ESMFold2-Fast', standalone: true, implemented: true, configured, input_role: 'sequence → structure', readiness: 'validated', licence: 'open_source' },
      { id: 'esmc', name: 'ESMC-300M', standalone: true, implemented: true, configured, input_role: 'sequence → score', readiness: 'offline_ready', licence: 'open_source' },
    ] });
    if (path === '/api/models') return Response.json({ configured: true, models: [{ id: 'anthropic/test', name: 'Anthropic test' }] });
    if (path === '/api/design/prepare') return Response.json({ sequence: 'AC', limits: 'Modeled residues only',
      mapping: [{ index: 1, original_chain: 'A', original_residue: '1', residue_name: 'ALA' },
        { index: 2, original_chain: 'A', original_residue: '2', residue_name: 'CYS' }] });
    if (path.endsWith('/cancel')) return Response.json({ ...state, status: 'cancelled' });
    return Response.json(result);
  }));
  return calls;
}

async function fill(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByRole('option', { name: 'Anthropic test' });
  await user.type(screen.getByLabelText('Describe your objective'), 'A benign generic target');
  const file = new File(['generic structure bytes'], 'target.pdb', { type: 'text/plain' });
  Object.defineProperty(file, 'text', { value: async () => 'generic structure bytes' });
  await user.upload(screen.getByLabelText(/Target structure/), file);
  await screen.findByRole('heading', { name: 'Target: 2 modeled residues' });
}

it('requires configured workers and never submits an unavailable design', async () => {
  const calls = serve(false); const user = userEvent.setup(); render(<DesignWorkflow />);
  await fill(user);
  expect((screen.getByRole('button', { name: 'Go' }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText(/Design requires deployed, validated Modal workers/)).toBeTruthy();
  expect(calls.filter(call => call.path === '/api/designs')).toHaveLength(0);
});

it('shows all scientific tools as standalone without adding ProteinMPNN to Go', async () => {
  const calls = serve(); const user = userEvent.setup(); render(<DesignWorkflow />);
  await screen.findByText('4 scientific tools cataloged');
  await user.click(screen.getByText('4 scientific tools cataloged'));
  expect(screen.getByText('ProteinMPNN v_48_020')).toBeTruthy();
  expect(screen.getByText(/ProteinMPNN is standalone first/)).toBeTruthy();
  await fill(user); await user.click(screen.getByRole('button', { name: 'Go' }));
  expect(calls.find(call => call.path === '/api/designs')).toBeTruthy();
});

it('submits uploaded non-GFP inputs and disables repeated submission while running', async () => {
  const calls = serve(); const user = userEvent.setup(); render(<DesignWorkflow />);
  await fill(user); await user.click(screen.getByRole('button', { name: 'Go' }));
  await screen.findByText(/1 observed records/);
  expect(calls.find(call => call.path === '/api/designs')?.body).toMatchObject({
    prompt: 'A benign generic target', structure: 'generic structure bytes', format: 'pdb', chain: 'A',
    settings: { count: 4, min_length: 60, max_length: 100, seed: 0 },
  });
  expect((screen.getByRole('button', { name: 'Go' }) as HTMLButtonElement).disabled).toBe(true);
  await user.click(screen.getByRole('button', { name: 'Go' }));
  expect(calls.filter(call => call.path === '/api/designs')).toHaveLength(1);
});

it('requires cancellation confirmation and propagates the confirmed request', async () => {
  const calls = serve(); const user = userEvent.setup(); render(<DesignWorkflow />);
  await fill(user); await user.click(screen.getByRole('button', { name: 'Go' }));
  await screen.findByText(/1 observed records/);
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  await user.click(screen.getByRole('button', { name: 'Cancel design' }));
  expect(calls.some(call => call.path.endsWith('/cancel'))).toBe(false);
  confirm.mockReturnValue(true);
  await user.click(screen.getByRole('button', { name: 'Cancel design' }));
  await screen.findByText(/Design cancelled/);
  expect(calls.filter(call => call.path.endsWith('/cancel'))).toHaveLength(1);
  confirm.mockRestore();
});

it('shows partial outcomes and missing metrics without fabricating values', async () => {
  serve(true, { ...state, status: 'partial', record_count: 5, text: '<script>Not executable</script>',
    candidates: [{ id: 'a'.repeat(64), sequence: 'ACD', rank: null, metrics: { 'esmfold2-complex': null }, artifacts: [] }] });
  const user = userEvent.setup(); render(<DesignWorkflow />); await fill(user);
  await user.click(screen.getByRole('button', { name: 'Go' }));
  await screen.findByText(/Design partial/);
  await user.click(screen.getByText('esmfold2-complex'));
  expect(screen.getAllByText('Not available').length).toBeGreaterThan(0);
  expect(screen.getByText('<script>Not executable</script>')).toBeTruthy();
  expect(document.querySelector('script')).toBeNull();
});

it('keeps Go disabled when the selected target fails structural validation', async () => {
  const calls = serve();
  const original = globalThis.fetch;
  vi.stubGlobal('fetch', vi.fn(async (path: string, options?: RequestInit) => path === '/api/design/prepare'
    ? Response.json({ detail: 'Select one unambiguous existing chain.' }, { status: 422 }) : original(path, options)));
  const user = userEvent.setup(); render(<DesignWorkflow />);
  await screen.findByRole('option', { name: 'Anthropic test' });
  await user.type(screen.getByLabelText('Describe your objective'), 'A computational target');
  const file = new File(['invalid structure'], 'target.cif', { type: 'text/plain' });
  Object.defineProperty(file, 'text', { value: async () => 'invalid structure' });
  await user.upload(screen.getByLabelText(/Target structure/), file);
  await screen.findByText('Select one unambiguous existing chain.');
  expect((screen.getByRole('button', { name: 'Go' }) as HTMLButtonElement).disabled).toBe(true);
  expect(calls.some(call => call.path === '/api/designs')).toBe(false);
});

it('validates and submits binding-site positions from the displayed residue map', async () => {
  const calls = serve(); const user = userEvent.setup(); render(<DesignWorkflow />); await fill(user);
  await user.type(screen.getByLabelText(/Optional binding-site positions/), '1, 3');
  expect((screen.getByRole('button', { name: 'Go' }) as HTMLButtonElement).disabled).toBe(true);
  await user.clear(screen.getByLabelText(/Optional binding-site positions/));
  await user.type(screen.getByLabelText(/Optional binding-site positions/), '1, 2');
  await user.click(screen.getByRole('button', { name: 'Go' }));
  await screen.findByText(/Design running/);
  expect(calls.find(call => call.path === '/api/designs')?.body).toMatchObject({ settings: { binding_residues: [1, 2] } });
});
