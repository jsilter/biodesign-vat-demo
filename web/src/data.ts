import { z } from 'zod';

const digest = z.string().regex(/^[a-f0-9]{64}$/);
const timestamp = z.iso.datetime({ offset: true });
export const MAX_CATALOG_BYTES = 8 * 1024 * 1024;
export const MAX_PREVIEW_BYTES = 64 * 1024;
export const previewKinds = new Set(['prompt', 'transcript', 'reasoning', 'aa_sequence', 'nt_sequence']);
const count = z.number().int().nonnegative();
const counts = z.object({ present: count, withheld: count, absent: count });
const checkpoint = z.object({
  checkpoint_format: z.literal('vat-transparency-checkpoint/1'), log_id: z.string(),
  tree_size: count, root_hash: digest, timestamp, keyid: digest, signature: z.string(),
  simulated: z.literal(true), independent_timestamp: z.literal(false),
});
const artifact = z.object({ kind: z.string(), name: z.string(), sha256: digest, bytes_len: count });
const record = z.object({
  record_type: z.enum(['session_open', 'agent_call', 'design_run', 'session_seal']),
  link: z.object({ leaf_index: count, sha_prev: digest }).passthrough(),
  user_session: z.object({ purpose_tag: z.string(), user_id: z.string() }).passthrough(),
  designer_statement: z.object({ keyid: z.string(), purpose_tag: z.string(), signature: z.string() }).passthrough().nullable(),
  params: z.object({ objectives: z.array(z.object({ name: z.string(), direction: z.string(), weight: z.number() }).passthrough()) }).passthrough(),
  inputs: z.array(artifact), intermediates: z.array(artifact), results: z.array(artifact),
}).passthrough();

export const scenarioSchema = z.object({
  id: z.enum(['clean', 'tampered', 'doctored', 'partial']),
  bundle: z.object({
    session_uuid: z.string(), records: z.array(record).max(100),
    signed_tree_head: z.object({
      root_hash: digest, tree_size: count.max(100), keyid: z.string(), timestamp, signature: z.string(),
    }).passthrough(),
    attestation: z.object({ payloadType: z.string(), payload: z.string(), signatures: z.array(z.object({ keyid: z.string(), sig: z.string() })) }).passthrough(),
    notary_receipt: z.object({
      keyid: z.string(), policy_id: z.literal('local-null-screen/1'),
      validation_result: z.literal('passed'), simulated: z.literal(true),
      independent_timestamp: z.literal(false), head_root: digest,
      tree_size: count, observed_at: timestamp, signature: z.string(),
    }).passthrough().nullable(),
    transparency_evidence: z.object({
      evidence_format: z.literal('vat-transparency-evidence/1'),
      entry: z.object({ entry_format: z.literal('vat-transparency-entry/1'), log_id: z.string(),
        leaf_index: count, accepted_at: timestamp, notary_receipt_digest: digest,
        attestation_digest: digest, session_uuid: z.string(), session_root: digest, session_tree_size: count }),
      checkpoint, inclusion_proof: z.record(z.string(), z.unknown()),
      prior_checkpoint: checkpoint.nullable(), consistency_proof: z.record(z.string(), z.unknown()).nullable(),
    }).nullable(),
  }).passthrough(),
  trust: z.object({ pinned_key: z.null(), entries: z.array(z.object({
    role: z.enum(['designer', 'recorder', 'notary', 'transparency_log']), keyid: z.string(), public_key: z.string(),
  })).length(4).refine(entries => new Set(entries.map(e => e.role)).size === 4 && new Set(entries.map(e => e.keyid)).size === 4) }),
  sequence: z.string().min(1), artifacts: z.record(digest, z.object({ available: z.boolean(), text: z.string().refine(text => new TextEncoder().encode(text).length <= MAX_PREVIEW_BYTES).nullable() })), archive_sha256: digest,
  report: z.object({
    outcome: z.enum(['OK', 'TAMPERED', 'BAD_SIGNATURE', 'NOT_INCLUDED', 'SEQUENCE_MISMATCH', 'INCOMPLETE']),
    exit_code: count,
    checks: z.array(z.object({ number: z.number().int().min(1).max(5), name: z.string(), passed: z.boolean(), detail: z.string() })).max(5),
    revealed: count, tree_size: count, unrevealed_leaves: count,
    signer_roles: z.string(), designer_identity: z.string(), notarization: z.string(),
    independent_timestamp: z.string(), capture_tier: z.string(), instructions: z.string(),
    transparency: z.string(), consistency: z.string(), checkpoint_size: count.nullable(), checkpoint_key: digest.nullable(),
    head_key_resolution: z.string(), leaf_counts: z.record(z.string(), count),
    transcripts: counts, reasoning: counts,
  }),
  report_text: z.string(),
}).superRefine((scenario, ctx) => {
  const { bundle, report, artifacts } = scenario;
  const fail = (message: string) => ctx.addIssue({ code: 'custom', message });
  const positions = bundle.records.map(r => r.link.leaf_index);
  if (new Set(positions).size !== positions.length || positions.some(i => i >= bundle.signed_tree_head.tree_size)) fail('Invalid disclosed positions');
  if (report.revealed !== positions.length || report.tree_size !== bundle.signed_tree_head.tree_size || report.unrevealed_leaves !== report.tree_size - positions.length) fail('Inconsistent disclosure counts');
  const leafCounts: Record<string, number> = {};
  const refs = bundle.records.flatMap(r => [...r.inputs, ...r.results, ...r.intermediates]);
  const digests = new Set(refs.map(a => a.sha256));
  for (const r of bundle.records) leafCounts[r.record_type] = (leafCounts[r.record_type] ?? 0) + 1;
  if (Object.keys(leafCounts).length !== Object.keys(report.leaf_counts).length || Object.entries(leafCounts).some(([key, n]) => report.leaf_counts[key] !== n)) fail('Inconsistent record types');
  if (digests.size !== Object.keys(artifacts).length || [...digests].some(d => !Object.hasOwn(artifacts, d))) fail('Artifact references do not match');
  for (const [d, view] of Object.entries(artifacts)) {
    if (view.text !== null && (!view.available || !refs.some(a => a.sha256 === d && previewKinds.has(a.kind)))) fail('Invalid preview availability');
  }
  for (const kind of ['transcript', 'reasoning'] as const) {
    const actual = { present: 0, withheld: 0, absent: 0 };
    for (const r of bundle.records) {
      const selected = [...r.inputs, ...r.results, ...r.intermediates].filter(a => a.kind === kind);
      actual.present += Number(selected.some(a => artifacts[a.sha256]?.available));
      actual.withheld += Number(selected.some(a => artifacts[a.sha256]?.available === false));
      actual.absent += Number(selected.length === 0);
    }
    if (actual.present !== report[kind === 'transcript' ? 'transcripts' : 'reasoning'].present || actual.withheld !== report[kind === 'transcript' ? 'transcripts' : 'reasoning'].withheld || actual.absent !== report[kind === 'transcript' ? 'transcripts' : 'reasoning'].absent) fail('Inconsistent artifact counts');
  }
  const checks = report.checks;
  if (!checks.length || checks.some((c, i) => c.number !== i + 1 || (!c.passed && i !== checks.length - 1))) fail('Invalid check progression');
  const expected: Record<string, [number, number[]]> = { OK: [0, []], TAMPERED: [2, [1, 4]], BAD_SIGNATURE: [3, [2]], NOT_INCLUDED: [4, [3]], SEQUENCE_MISMATCH: [5, [4]], INCOMPLETE: [6, [5]] };
  const [exitCode, failures] = expected[report.outcome];
  const last = checks.at(-1);
  if (report.exit_code !== exitCode || (report.outcome === 'OK' ? checks.length !== 5 || checks.some(c => !c.passed) : !last || last.passed || !failures.includes(last.number))) fail('Inconsistent outcome');
  const passed = (n: number) => checks.some(c => c.number === n && c.passed);
  if (!passed(2) && (report.signer_roles !== 'unverified' || report.capture_tier !== 'unverified' || report.notarization !== 'absent' || report.head_key_resolution !== 'unverified')) fail('Unestablished signature claims');
  if ((!passed(3) || !positions.includes(0)) && report.designer_identity !== 'unverified') fail('Unestablished designer identity');
  if (passed(3) && positions.includes(0)) {
    const statement = bundle.records.find(r => r.link.leaf_index === 0)?.designer_statement;
    const designer = scenario.trust.entries.find(e => e.role === 'designer');
    if (!statement || report.designer_identity !== statement.keyid || statement.keyid !== designer?.keyid) fail('Inconsistent designer identity');
  }
  if (report.independent_timestamp !== 'unavailable (local clock)') fail('Independent time is unavailable in this demo');
  if (!passed(3) && (report.transparency !== 'absent' || report.consistency !== 'unverified'
      || report.checkpoint_size !== null || report.checkpoint_key !== null)) fail('Unestablished transparency claim');
  if (passed(3)) {
    const evidence = bundle.transparency_evidence;
    if (!evidence || !bundle.notary_receipt) fail('Required transparency missing');
    else {
      const { entry, checkpoint: head, prior_checkpoint: prior } = evidence;
      const key = scenario.trust.entries.find(e => e.role === 'transparency_log');
      if (entry.log_id !== head.log_id || entry.session_uuid !== bundle.session_uuid
          || entry.session_root !== bundle.signed_tree_head.root_hash
          || entry.session_tree_size !== bundle.signed_tree_head.tree_size
          || entry.leaf_index >= head.tree_size || head.keyid !== key?.keyid
          || report.checkpoint_size !== head.tree_size || report.checkpoint_key !== head.keyid
          || report.transparency !== 'simulated local log; inclusion verified'
          || report.consistency !== (head.tree_size > 1 ? 'immediate consistency verified' : 'first checkpoint')) fail('Inconsistent transparency claims');
      if (head.tree_size > 1 ? !prior || prior.tree_size !== head.tree_size - 1 || prior.log_id !== head.log_id
          || prior.keyid !== head.keyid || !evidence.consistency_proof : prior !== null || evidence.consistency_proof !== null) fail('Inconsistent checkpoint history');
    }
  }
  if (passed(2) && (report.signer_roles !== 'role-bound' || report.notarization !== (bundle.notary_receipt ? 'simulated (local-null-screen/1)' : 'absent'))) fail('Inconsistent signature claims');
  if (passed(5) && report.unrevealed_leaves !== 0 || report.outcome === 'INCOMPLETE' && report.unrevealed_leaves === 0) fail('Inconsistent completeness');
});

export const catalogSchema = z.object({
  format: z.literal('vat-ui-demo/1'), generated_at: timestamp,
  scenarios: z.array(scenarioSchema).length(4).refine(items => new Set(items.map(item => item.id)).size === 4, 'Duplicate scenarios'),
});

export type Scenario = z.infer<typeof scenarioSchema>;
export type Catalog = z.infer<typeof catalogSchema>;
export type AuditRecord = z.infer<typeof record>;
export type Role = 'designer' | 'recorder' | 'notary' | 'verifier';

/** Validate display data, not signatures. Only Python verifies the downloaded bundle. */
export async function loadCatalog(signal: AbortSignal): Promise<Catalog> {
  const response = await fetch(`${import.meta.env.BASE_URL}demo/catalog.json`, { signal, cache: 'no-store' });
  if (!response.ok) throw new Error(`Demo data could not be loaded (HTTP ${response.status}).`);
  if (!response.body) throw new Error('Empty catalog response.');
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let size = 0;
  let text = '';
  try {
    while (true) {
      signal.throwIfAborted();
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_CATALOG_BYTES) throw new Error('Demo catalog byte limit exceeded.');
      text += decoder.decode(value, { stream: true });
    }
    text += decoder.decode();
    signal.throwIfAborted();
    return catalogSchema.parse(JSON.parse(text));
  } finally {
    try { await reader.cancel(); } finally { reader.releaseLock(); }
  }
}

export const roles: Role[] = ['designer', 'recorder', 'notary', 'verifier'];
export const titles: Record<Role, string> = {
  designer: 'What was requested?', recorder: 'What was recorded?',
  notary: 'Who signed it?', verifier: 'Does it check out?',
};

export const stories: Record<Scenario['id'], { name: string; label: string; description: string; finding: string }> = {
  clean: { name: 'Complete session', label: '01 / Baseline', description: 'An intact audit trail, from stated purpose to the submitted output.', finding: 'The five checks pass. This establishes provenance within the local trust model, not biological safety.' },
  tampered: { name: 'Altered record', label: '02 / Tampering', description: 'A purpose tag was changed after the recorder sealed the session.', finding: 'The altered record breaks the chain. Verification stops at the first failed check; later checks have not run.' },
  doctored: { name: 'Conflicting objectives', label: '03 / Content review', description: 'Authentic records can still reveal a problem with what was requested.', finding: 'The fixture’s recorded objectives diverge from the stated purpose. This is a curated reviewer finding, not a cryptographic failure or automated hazard assessment.' },
  partial: { name: 'Selective disclosure', label: '04 / Missing evidence', description: 'One authentic record is disclosed from a ten-record commitment.', finding: 'Membership is not completeness. Nine record positions are missing; their types and contents are unknown to the verifier.' },
};
