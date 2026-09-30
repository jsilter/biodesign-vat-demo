import { expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import StaticEvidence, { EMBEDDED_GRAPH_ID, embeddedGraph } from './StaticEvidence';
import { exampleGraph } from './graph-test-data';

function embed(text: string) {
  document.getElementById(EMBEDDED_GRAPH_ID)?.remove();
  const script = document.createElement('script');
  script.id = EMBEDDED_GRAPH_ID; script.type = 'application/json'; script.textContent = text;
  document.head.append(script);
}

it('reads the embedded graph and shows verdicts and objects without any request', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
  const api = vi.fn(async () => { throw new Error('the static page must not fetch'); });
  vi.stubGlobal('fetch', api);
  embed(JSON.stringify(exampleGraph()));
  const user = userEvent.setup(); render(<StaticEvidence graph={embeddedGraph()} title="Canary" />);
  expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Canary');
  expect(screen.getByRole('region', { name: 'Loaded disclosure' }).textContent).toContain('VALID');
  await user.click(screen.getByRole('button', { name: /Unfamiliar producer/ }));
  expect(screen.getByRole('region', { name: 'Selected evidence' }).textContent).toContain('Signature: passed');
  expect(screen.queryByRole('button', { name: 'Falsification' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Download evidence' })).toBeNull();
  expect(api).not.toHaveBeenCalled();
});

it('rejects a page without an embedded graph or with one the schema refuses', () => {
  document.getElementById(EMBEDDED_GRAPH_ID)?.remove();
  expect(() => embeddedGraph()).toThrow('no embedded evidence graph');
  embed('{"session": "only"}');
  expect(() => embeddedGraph()).toThrow();
});
