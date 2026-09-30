import { expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Workflow } from './Workflow';

const id = '12345678-1234-4234-8234-123456789012';
const running = { id, status: 'running', logs: [{ at: 'now', message: 'Calling provider' }], text: '', returned_model: '', downloads: [] };

it('keeps research limits and costs visible while allowing source details to be expanded', async () => {
  serve();
  const user = userEvent.setup(); render(<Workflow />);
  await screen.findByRole('option', { name: /OpenAI/ });
  expect(screen.getByText(/does not generate protein sequences/)).toBeTruthy();
  expect(screen.getByText(/Model usage costs money/)).toBeTruthy();
  const summary = screen.getByText('Sources and demo limits');
  const details = summary.closest('details')!;
  expect(details.open).toBe(false);
  await user.click(summary);
  expect(details.open).toBe(true);
  expect(details.textContent).toContain('not by an independent observer');
  expect(details.textContent).toContain('not full papers');
  expect((screen.getByRole('button', { name: 'Go' }) as HTMLButtonElement).disabled).toBe(true);
});

function serve(start = running) {
  const calls: { path: string; body: unknown }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (path: string, options?: RequestInit) => {
    calls.push({ path, body: options?.body ? JSON.parse(String(options.body)) : undefined });
    let value: unknown = start;
    if (path === '/api/models') value = { models: [{ id: 'openai/test', name: 'OpenAI' }, { id: 'anthropic/test', name: 'Anthropic' }], configured: true };
    if (path.endsWith('/cancel')) value = { ...running, status: 'cancelled' };
    return new Response(JSON.stringify(value), { status: 200 });
  }));
  return calls;
}

it('defaults to Opus 4.8 and submits that model without requiring a selection', async () => {
  const calls: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (path: string, options?: RequestInit) => {
    if (path === '/api/models') return Response.json({ configured: true, models: [
      { id: 'openai/test', name: 'OpenAI' },
      { id: 'anthropic/claude-opus-4.8', name: 'Claude Opus 4.8' },
    ] });
    if (options?.body) calls.push(JSON.parse(String(options.body)));
    return Response.json(running);
  }));
  const user = userEvent.setup(); render(<Workflow />);
  await screen.findByRole('option', { name: /Claude Opus 4.8/ });
  expect((screen.getByLabelText('2. Choose an AI model') as HTMLSelectElement).value).toBe('anthropic/claude-opus-4.8');
  await user.type(screen.getByLabelText('1. Enter your question'), 'Research GFP');
  await user.click(screen.getByRole('button', { name: 'Go' }));
  await screen.findByText('Workflow running');
  expect(calls).toEqual([expect.objectContaining({ model: 'anthropic/claude-opus-4.8', prompt: 'Research GFP' })]);
});

it('explains missing backend credentials while preventing live submission', async () => {
  const fetcher = vi.fn(async () => Response.json({ configured: false,
    models: [{ id: 'anthropic/claude-opus-4.8', name: 'Claude Opus 4.8' }] }));
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup(); render(<Workflow />);
  await screen.findByText(/Live research needs an API key/);
  await user.click(screen.getByText('How to enable live research'));
  expect(screen.getByText(/Set OPENROUTER_API_KEY/).textContent).toContain('repository root .env');
  await user.type(screen.getByLabelText('1. Enter your question'), 'Research GFP');
  expect((screen.getByRole('button', { name: 'Go' }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByRole('status').textContent).toContain('API key required');
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it('submits the pasted prompt/model once and disables Go while running', async () => {
  const calls = serve();
  const user = userEvent.setup(); render(<Workflow />);
  await screen.findByRole('option', { name: /Anthropic/ });
  expect((screen.getByLabelText('2. Choose an AI model') as HTMLSelectElement).value).toBe('openai/test');
  await user.type(screen.getByLabelText('1. Enter your question'), 'actual request');
  await user.selectOptions(screen.getByLabelText('2. Choose an AI model'), 'anthropic/test');
  await user.dblClick(screen.getByRole('button', { name: 'Go' }));
  await screen.findByText('Workflow running');
  const starts = calls.filter(call => call.path === '/api/workflows');
  expect(starts).toHaveLength(1);
  expect(starts[0].body).toMatchObject({ prompt: 'actual request', model: 'anthropic/test' });
  expect((screen.getByRole('button', { name: 'Go' }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByRole('log').textContent).toContain('Calling provider');
});

it('requires confirmation before sending cancellation', async () => {
  const calls = serve(); const user = userEvent.setup(); render(<Workflow />);
  await screen.findByRole('option', { name: /OpenAI/ });
  await user.type(screen.getByLabelText('1. Enter your question'), 'x');
  await user.click(screen.getByRole('button', { name: 'Go' }));
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  await user.click(await screen.findByRole('button', { name: 'Cancel workflow' }));
  expect(calls.some(call => call.path.endsWith('/cancel'))).toBe(false);
  confirm.mockReturnValue(true);
  await user.click(screen.getByRole('button', { name: 'Cancel workflow' }));
  await screen.findByText('Workflow cancelled');
  expect(calls.filter(call => call.path.endsWith('/cancel'))).toHaveLength(1);
  expect(screen.queryByRole('link', { name: 'bundle.zip' })).toBeNull();
});

it('renders completed output literally and shows only fixed evidence downloads', async () => {
  serve({ ...running, status: 'completed', text: '<script>not executable</script>', returned_model: 'openai/returned', downloads: ['bundle.zip', 'trust.json', 'report.txt', 'response.txt'] } as typeof running);
  const user = userEvent.setup(); const { container } = render(<Workflow />);
  await screen.findByRole('option', { name: /OpenAI/ });
  await user.type(screen.getByLabelText('1. Enter your question'), 'x');
  await user.click(screen.getByRole('button', { name: 'Go' }));
  await screen.findByText('<script>not executable</script>');
  expect(container.querySelector('script')).toBeNull();
  expect(screen.getByRole('link', { name: 'bundle.zip' }).getAttribute('href')).toBe(`/api/workflows/${id}/files/bundle.zip`);
  expect(screen.getByText(/No protein sequence is generated or verified/)).toBeTruthy();
});

it('recovers an uncertain start with the same id instead of issuing a new job', async () => {
  const calls = serve(); const original = vi.mocked(fetch);
  const normal = original.getMockImplementation()!;
  let fail = true;
  original.mockImplementation(async (...args) => {
    if (args[0] === '/api/workflows' && fail) { fail = false; calls.push({ path: '/uncertain', body: JSON.parse(String(args[1]?.body)) }); throw new Error('network'); }
    return normal(...args);
  });
  const user = userEvent.setup(); render(<Workflow />);
  await screen.findByRole('option', { name: /OpenAI/ });
  await user.type(screen.getByLabelText('1. Enter your question'), 'x');
  await user.click(screen.getByRole('button', { name: 'Go' }));
  await user.click(await screen.findByRole('button', { name: 'Recover request' }));
  await screen.findByText('Workflow running');
  expect(calls.find(call => call.path === '/api/workflows')?.body).toEqual(calls.find(call => call.path === '/uncertain')?.body);
});

it('handles unavailable backend without permitting a submission', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
  render(<Workflow />);
  await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('backend'));
  expect((screen.getByRole('button', { name: 'Go' }) as HTMLButtonElement).disabled).toBe(true);
});

it('polls a running call through completion and presents the actual result', async () => {
  serve();
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (...args) => {
    if (args[0] === `/api/workflows/${id}`) return new Response(JSON.stringify({ ...running,
      status: 'completed', text: 'Actual completed result', returned_model: 'anthropic/returned', downloads: ['report.txt'] }));
    return original(...args);
  });
  const user = userEvent.setup(); render(<Workflow />);
  await screen.findByRole('option', { name: /OpenAI/ });
  await user.type(screen.getByLabelText('1. Enter your question'), 'x');
  await user.click(screen.getByRole('button', { name: 'Go' }));
  await screen.findByText('Actual completed result', {}, { timeout: 2500 });
  expect(screen.getByRole('link', { name: 'report.txt' })).toBeTruthy();
  expect((screen.getByRole('button', { name: 'Go' }) as HTMLButtonElement).disabled).toBe(false);
});
