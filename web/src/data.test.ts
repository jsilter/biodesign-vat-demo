import { describe, expect, it, vi } from 'vitest';
import { catalogSchema, loadCatalog, MAX_CATALOG_BYTES, MAX_PREVIEW_BYTES } from './data';
import { fixtureCatalog, serveCatalog } from './test-data';

it('accepts actual Python exports without manufacturing verifier success', async () => {
  serveCatalog();
  const catalog = await loadCatalog(new AbortController().signal);
  expect(catalog.scenarios.map(s => s.report.exit_code)).toEqual([0, 2, 0, 6]);
  expect(catalog.scenarios[3].bundle.records.map(r => r.link.leaf_index)).toEqual([5]);
});

export const invalidCases: [string, (data: ReturnType<typeof fixtureCatalog>) => void][] = [
  ['format', d => { d.format = 'wrong' as typeof d.format; }],
  ['duplicate scenario', d => { d.scenarios[1].id = 'clean'; }],
  ['missing scenario', d => { d.scenarios.pop(); }],
  ['timestamp', d => { d.generated_at = 'not a date'; }],
  ['naive timestamp', d => { d.generated_at = '2026-09-14T12:00:00'; }],
  ['digest', d => { d.scenarios[0].archive_sha256 = '../escape'; }],
  ['nonfinite value', d => { d.scenarios[0].report.revealed = Infinity; }],
  ['counts', d => { d.scenarios[0].report.revealed++; }],
  ['tree size', d => { d.scenarios[0].report.tree_size++; }],
  ['huge tree', d => { d.scenarios[0].bundle.signed_tree_head.tree_size = 101; }],
  ['record types', d => { d.scenarios[0].report.leaf_counts.session_open++; }],
  ['unknown record type', d => { Object.assign(d.scenarios[0].bundle.records[0], { record_type: 'not_a_record_type' }); }],
  ['duplicate position', d => { d.scenarios[0].bundle.records[1].link.leaf_index = 0; }],
  ['out of range', d => { d.scenarios[0].bundle.records[0].link.leaf_index = 100; }],
  ['check gap', d => { d.scenarios[0].report.checks.splice(1, 1); }],
  ['empty checks', d => { d.scenarios[0].report.checks = []; }],
  ['check after failure', d => { d.scenarios[0].report.checks[1].passed = false; }],
  ['wrong exit', d => { d.scenarios[0].report.exit_code = 2; }],
  ['wrong outcome', d => { d.scenarios[0].report.outcome = 'INCOMPLETE'; }],
  ['unverified signature claim', d => { d.scenarios[1].report.signer_roles = 'role-bound'; }],
  ['hidden identity claim', d => { d.scenarios[3].report.designer_identity = 'designer'; }],
  ['identity differs from statement', d => { d.scenarios[0].report.designer_identity = 'unrelated-designer'; }],
  ['identity differs from public trust', d => { d.scenarios[0].trust.entries.find(e => e.role === 'designer')!.keyid = 'unrelated-designer'; }],
  ['independent time claim', d => { d.scenarios[0].report.independent_timestamp = 'independently verified'; }],
  ['stripped transparency', d => { d.scenarios[0].bundle.transparency_evidence = null; d.scenarios[0].bundle.notary_receipt = null; }],
  ['checkpoint report size', d => { d.scenarios[0].report.checkpoint_size = 99; }],
  ['wrong log key', d => { d.scenarios[0].bundle.transparency_evidence!.checkpoint.keyid = '0'.repeat(64); }],
  ['missing prior checkpoint', d => { d.scenarios[2].bundle.transparency_evidence!.prior_checkpoint = null; }],
  ['artifact count', d => { d.scenarios[2].report.transcripts.present++; }],
  ['missing artifact', d => { delete d.scenarios[2].artifacts[Object.keys(d.scenarios[2].artifacts)[0]]; }],
  ['extra artifact', d => { d.scenarios[0].artifacts['0'.repeat(64)] = { available: true, text: 'leaked' }; }],
  ['withheld preview', d => { const a = Object.values(d.scenarios[2].artifacts).find(a => a.text !== null)!; a.available = false; }],
  ['preview bytes', d => { Object.values(d.scenarios[0].artifacts)[0].text = 'x'.repeat(MAX_PREVIEW_BYTES + 1); }],
];

describe('rejects inconsistent presentation data', () => {
  it.each(invalidCases)('%s', (_, mutate) => {
    const data = fixtureCatalog(); mutate(data);
    expect(catalogSchema.safeParse(data).success).toBe(false);
  });
});

it.each([
  ['TAMPERED', 2, 1], ['TAMPERED', 2, 4], ['BAD_SIGNATURE', 3, 2],
  ['NOT_INCLUDED', 4, 3], ['SEQUENCE_MISMATCH', 5, 4],
] as const)('accepts coherent %s at check %s/%s', (outcome, code, number) => {
  const data = fixtureCatalog();
  const r = data.scenarios[0].report;
  r.outcome = outcome; r.exit_code = code; r.checks = r.checks.slice(0, number);
  r.checks.at(-1)!.passed = false;
  if (number <= 2) Object.assign(r, { signer_roles: 'unverified', capture_tier: 'unverified', notarization: 'absent', head_key_resolution: 'unverified' });
  if (number <= 3) r.designer_identity = 'unverified';
  if (number <= 3) Object.assign(r, { transparency: 'absent', consistency: 'unverified', checkpoint_size: null, checkpoint_key: null });
  expect(catalogSchema.safeParse(data).success).toBe(true);
});

it.each(['http', 'json', 'utf8', 'stream'])('rejects %s failure', async kind => {
  vi.stubGlobal('fetch', vi.fn(async () => {
    if (kind === 'http') return new Response('missing', { status: 404 });
    if (kind === 'json') return new Response('{');
    if (kind === 'utf8') return new Response(new Uint8Array([255]));
    return new Response(new ReadableStream({ start(c) { c.error(new Error('read failed')); } }));
  }));
  await expect(loadCatalog(new AbortController().signal)).rejects.toThrow();
});

it.each([undefined, '1'])('bounds streamed bytes regardless of Content-Length %s', async length => {
  const cancel = vi.fn();
  let chunk = 0;
  vi.stubGlobal('fetch', vi.fn(async () => new Response(new ReadableStream({
    pull(c) { c.enqueue(new Uint8Array(chunk++ === 0 ? MAX_CATALOG_BYTES : 1).fill(32)); }, cancel,
  }), { headers: length ? { 'Content-Length': length } : {} })));
  await expect(loadCatalog(new AbortController().signal)).rejects.toThrow('byte limit');
  expect(cancel).toHaveBeenCalledOnce();
});

it('accepts exact catalog limit and split multibyte UTF-8', async () => {
  const data = fixtureCatalog();
  data.scenarios[0].report_text += '🧬';
  const encoded = new TextEncoder().encode(JSON.stringify(data));
  const split = encoded.findIndex(b => b === 0xf0) + 1;
  vi.stubGlobal('fetch', vi.fn(async () => new Response(new ReadableStream({ start(c) {
    c.enqueue(encoded.slice(0, split)); c.enqueue(encoded.slice(split));
    c.enqueue(new Uint8Array(MAX_CATALOG_BYTES - encoded.length).fill(32)); c.close();
  } }))));
  expect((await loadCatalog(new AbortController().signal)).scenarios[0].report_text.endsWith('🧬')).toBe(true);
});

it('honors an aborted load', async () => {
  serveCatalog();
  const controller = new AbortController(); controller.abort();
  await expect(loadCatalog(controller.signal)).rejects.toThrow();
});
