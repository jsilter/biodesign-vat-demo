import { useEffect, useRef, useState } from 'react';
import { z } from 'zod';

const artifact = z.object({ sha256: z.string().regex(/^[a-f0-9]{64}$/), name: z.string(), kind: z.string(), bytes_len: z.number() });
const schema = z.object({
  id: z.uuid(), status: z.enum(['running', 'recovering', 'cancelling', 'completed', 'partial', 'failed', 'cancelled', 'interrupted', 'needs_attention']),
  logs: z.array(z.object({ at: z.string(), message: z.string() })), text: z.string(), record_count: z.number(),
  artifacts: z.array(artifact), candidates: z.array(z.object({ id: z.string(), sequence: z.string(), rank: z.number().nullable(),
    metrics: z.record(z.string(), z.unknown()), artifacts: z.array(artifact) })),
  evidence_status: z.enum(['verified', 'unverified']).optional(),
  counts: z.object({ requested: z.number(), generated: z.number().nullable(), retained: z.number(), scored: z.number() }).optional(),
  jobs: z.array(z.object({ id: z.string(), tool: z.string(), status: z.string() })).optional(),
});
const targetSchema = z.object({ sequence: z.string(), limits: z.string(), mapping: z.array(z.object({
  index: z.number(), original_chain: z.string(), original_residue: z.string(), residue_name: z.string(),
})) });
type State = z.infer<typeof schema>;
class ServerError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

async function request(path: string, body?: unknown) {
  const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(15000) });
  if (!response.ok) {
    const error = await response.json();
    throw new ServerError(typeof error.detail === 'string' ? error.detail : `Server returned HTTP ${response.status}.`, response.status);
  }
  return response.json();
}

const terminal = new Set(['completed', 'partial', 'failed', 'cancelled', 'interrupted']);
function href(id: string, item: z.infer<typeof artifact>) {
  return `/api/designs/${id}/artifacts/${item.sha256}/${encodeURIComponent(item.name)}`;
}

export function DesignWorkflow() {
  const [configured, setConfigured] = useState(false);
  const [tools, setTools] = useState<{ id: string; name: string; standalone: boolean; configured: boolean;
    implemented: boolean;
    input_role: string; readiness: 'offline_ready' | 'deployed' | 'validated' | 'blocked';
    licence: 'open_source' | 'noncommercial' | 'mixed'; blocker?: string }[]>([]);
  const [prompt, setPrompt] = useState('');
  const [models, setModels] = useState<{ id: string; name: string }[]>([]);
  const [model, setModel] = useState('');
  const [structure, setStructure] = useState('');
  const [format, setFormat] = useState('cif');
  const [chain, setChain] = useState('A');
  const [count, setCount] = useState(4);
  const [minimum, setMinimum] = useState(60);
  const [maximum, setMaximum] = useState(100);
  const [seed, setSeed] = useState(0);
  const [binding, setBinding] = useState('');
  const [target, setTarget] = useState<z.infer<typeof targetSchema> | null>(null);
  const [targetError, setTargetError] = useState('');
  const [preparing, setPreparing] = useState(false);
  const [job, setJob] = useState<State | null>(null);
  const [pending, setPending] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [restore, setRestore] = useState('');
  const guard = useRef(false);
  const log = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let live = true;
    Promise.all([request('/api/design/config'), request('/api/models')]).then(([config, catalog]) => {
      const c = z.object({ configured: z.boolean(), tools: z.array(z.object({ id: z.string(), name: z.string(),
        standalone: z.boolean(), configured: z.boolean(), implemented: z.boolean(), input_role: z.string(),
        readiness: z.enum(['offline_ready', 'deployed', 'validated', 'blocked']),
        licence: z.enum(['open_source', 'noncommercial', 'mixed']), blocker: z.string().optional() })).default([]) }).parse(config);
      const m = z.object({ models: z.array(z.object({ id: z.string(), name: z.string() })), configured: z.boolean() }).parse(catalog);
      if (!live) return;
      setConfigured(c.configured && m.configured); setTools(c.tools); setModels(m.models);
      setModel(m.models.find(item => item.id === 'anthropic/claude-opus-4.8')?.id || m.models[0]?.id || '');
    }).catch(() => { if (live) setError('Could not load design configuration. Check the backend.'); });
    return () => { live = false; };
  }, []);
  useEffect(() => {
    setTarget(null); setTargetError('');
    if (!structure || !chain.trim()) { setPreparing(false); return; }
    let live = true;
    setPreparing(true);
    const timer = setTimeout(() => {
      request('/api/design/prepare', { structure, format, chain }).then(value => {
        if (live) setTarget(targetSchema.parse(value));
      }).catch(failure => { if (live) setTargetError(failure instanceof ServerError ? failure.message : 'Could not validate the target.'); })
        .finally(() => { if (live) setPreparing(false); });
    }, 250);
    return () => { live = false; clearTimeout(timer); };
  }, [structure, format, chain]);
  useEffect(() => {
    if (!job || terminal.has(job.status)) return;
    const id = job.id;
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    function poll() {
      request(`/api/designs/${id}`).then(value => { if (live) { setJob(schema.parse(value)); setError(''); } })
        .catch(() => { if (live) setError('Progress connection interrupted; the remote job may still be running.'); })
        .finally(() => { if (live) timer = setTimeout(poll, 2000); });
    }
    timer = setTimeout(poll, 2000);
    return () => { live = false; clearTimeout(timer); };
  }, [job?.id, job?.status]);
  useEffect(() => { log.current?.scrollTo?.(0, log.current.scrollHeight); }, [job?.logs]);
  const active = busy || pending !== null || (job !== null && !terminal.has(job.status));
  const bindingResidues = binding.trim() ? binding.split(',').map(value => Number(value.trim())) : [];
  const validBinding = bindingResidues.every(value => Number.isInteger(value) && value >= 1 && value <= (target?.sequence.length || 0))
    && new Set(bindingResidues).size === bindingResidues.length;
  async function start(recover = false) {
    if (guard.current) return;
    guard.current = true; setBusy(true); setError('');
    const payload = recover && pending ? pending : { id: crypto.randomUUID(), prompt, model, structure, format, chain,
      settings: { count, min_length: minimum, max_length: maximum, seed, binding_residues: bindingResidues } };
    setPending(payload);
    try { setJob(schema.parse(await request('/api/designs', payload))); setPending(null); }
    catch (failure) {
      if (failure instanceof ServerError && [409, 422, 503].includes(failure.status)) {
        setPending(null); setError(failure.message);
      } else setError(`${failure instanceof Error ? failure.message : 'Start failed.'} Recover the same request to check its identity safely.`);
    }
    finally { guard.current = false; setBusy(false); }
  }
  async function cancel() {
    if (!job || !window.confirm('Cancel local and Modal work? Charges already incurred cannot be reversed.')) return;
    setBusy(true);
    try { setJob(schema.parse(await request(`/api/designs/${job.id}/cancel`, {}))); setError(''); }
    catch { setError('Cancellation could not be confirmed. Remote work may still run; retry cancellation.'); }
    finally { setBusy(false); }
  }
  return <section className="card workflow" aria-label="Live protein design">
    <span className="eyebrow">Design and score</span><h2>Run a protein-design attempt</h2>
    <p>BoltzGen generates candidates. ESMFold2-Fast predicts structures; ESMC-300M scores sequence plausibility. These are computational results, not proof of binding or safety.</p>
    {tools.length > 0 && <details><summary>{tools.length} scientific tools cataloged</summary><ul>
      {tools.map(tool => <li key={tool.id}><strong>{tool.name}</strong>: {tool.input_role} · {tool.readiness.replace('_', ' ')} · {tool.licence.replace('_', ' ')}
        {!tool.implemented && <span> · inventory only</span>}
        {tool.blocker && <span> — {tool.blocker}</span>}</li>)}</ul>
      <p className="small">This form runs the fixed BoltzGen → ESMFold2-Fast → ESMC composition. ProteinMPNN is standalone first and is not called by Go.</p>
    </details>}
    {!configured && <p className="notice">Design requires deployed, validated Modal workers and an OpenRouter key. Research and pre-built examples remain available.</p>}
    <label htmlFor="design-prompt">Describe your objective</label><textarea id="design-prompt" value={prompt} disabled={active} onChange={e => setPrompt(e.target.value)} />
    <label htmlFor="design-model">AI model</label><select id="design-model" value={model} disabled={active} onChange={e => setModel(e.target.value)}>{models.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select>
    <label htmlFor="design-file">Target structure (PDB or mmCIF, up to 10 MiB)</label><input id="design-file" type="file" accept=".pdb,.cif,.mmcif" disabled={active} onChange={async e => {
      const file = e.target.files?.[0]; setStructure('');
      if (!file) return;
      if (file.size > 10 * 1024 * 1024) { setError('Target file exceeds 10 MiB.'); return; }
      try { setStructure(await file.text()); setFormat(file.name.toLowerCase().endsWith('.pdb') ? 'pdb' : 'cif'); setError(''); }
      catch { setError('Could not read the target file.'); }
    }} />
    <label htmlFor="design-chain">Target chain</label><input id="design-chain" value={chain} disabled={active} onChange={e => setChain(e.target.value)} />
    {preparing && <p role="status">Checking the target structure…</p>}
    {targetError && <p role="alert">{targetError}</p>}
    {target && <><h3>Target: {target.sequence.length} modeled residues</h3><pre className="workflow-response">{target.sequence}</pre>
      <p className="small">{target.limits}</p><details><summary>Residue positions for the binding site</summary>
        <div className="workflow-logs">{target.mapping.map(row => <div key={row.index}>{row.index}: {row.original_chain} {row.original_residue} ({row.residue_name})</div>)}</div>
      </details></>}
    <label htmlFor="design-binding">Optional binding-site positions (comma-separated)</label><input id="design-binding" value={binding} disabled={active} onChange={e => setBinding(e.target.value)} placeholder="For example: 5, 6, 7" />
    {!validBinding && <p role="alert">Use unique positions from the prepared target map.</p>}
    {([['Candidate count', count, setCount, 1, 100], ['Minimum binder length', minimum, setMinimum, 10, 500], ['Maximum binder length', maximum, setMaximum, 10, 500], ['Seed', seed, setSeed, 0, 2147483647]] as const).map(([label, value, setter, min, max]) => <label key={label}>{label}<input type="number" min={min} max={max} step="1" value={value} disabled={active} onChange={e => setter(Number(e.target.value))} /></label>)}
    <p className="small">Go authorizes one generation and scoring of retained candidates. GPU jobs cost money: H100 for generation/folding, L4 for ESMC; 30-minute job timeouts. Input and summary text go to OpenRouter; scientific inputs go to Modal.</p>
    <div className="workflow-actions"><button className="download" disabled={active || !configured || !prompt.trim() || !target || preparing || !validBinding || !model || minimum > maximum || ![count, minimum, maximum, seed].every(Number.isInteger) || count < 1 || count > 100 || minimum < 10 || minimum > 500 || maximum < 10 || maximum > 500 || seed < 0 || seed > 2147483647} onClick={() => void start()}>Go</button>
      {pending && !busy && <button onClick={() => void start(true)}>Recover design request</button>}
      {job && !terminal.has(job.status) && <button disabled={busy} onClick={() => void cancel()}>Cancel design</button>}
    </div>
    <label htmlFor="design-restore">Recover a saved run ID</label><input id="design-restore" value={restore} onChange={e => setRestore(e.target.value)} /><button disabled={active || !z.uuid().safeParse(restore).success} onClick={() => {
      request(`/api/designs/${restore}`).then(value => { setJob(schema.parse(value)); setError(''); }).catch(() => setError('Saved run could not be recovered.'));
    }}>Load saved run</button>
    {error && <p role="alert">{error}</p>}
    {job && <><p role="status">Design {job.status} · {job.record_count} observed records · Run ID: {job.id}</p>
      {job.evidence_status === 'unverified' && <p className="notice">Scientific results are available, but signed evidence could not be verified.</p>}
      {job.counts && <p>Requested: {job.counts.requested} · Generated: {job.counts.generated ?? 'Not available'} · Retained: {job.counts.retained} · Fully scored: {job.counts.scored}</p>}
      {job.jobs && <ul>{job.jobs.map(stage => <li key={stage.id}>{stage.tool}: {stage.status}</li>)}</ul>}
      <div className="workflow-logs" role="log" ref={log}>{job.logs.map((entry, index) => <div key={index}>{entry.at} {entry.message}</div>)}</div>
      <h3>{job.candidates.length} retained candidates</h3>{job.candidates.map((candidate, index) => <article key={candidate.id}><h4>Candidate {index + 1}{candidate.rank !== null ? ` · BoltzGen rank ${candidate.rank}` : ''}</h4><pre className="workflow-response">{candidate.sequence}</pre>
        <table><thead><tr><th>Assessment</th><th>Computed value</th></tr></thead><tbody>
          {([['esmfold2-binder', 'Binder pLDDT (0–100)', 'mean_plddt', 100], ['esmfold2-complex', 'Complex pLDDT (0–100)', 'mean_plddt', 100],
            ['esmfold2-complex', 'Complex ipTM (0–1)', 'iptm', 1], ['esmc-sequence', 'ESMC pseudo-perplexity', 'pseudo_perplexity', 1]] as const).map(([key, label, field, scale]) => {
            const values = candidate.metrics[key];
            const value = values && typeof values === 'object' ? (values as Record<string, unknown>)[field] : undefined;
            return <tr key={label}><td>{label}</td><td>{typeof value === 'number' && Number.isFinite(value) ? (value * scale).toFixed(2) : 'Not available'}</td></tr>;
          })}
        </tbody></table>
        {Object.entries(candidate.metrics).map(([name, metrics]) => <details key={name}><summary>{name}</summary><pre className="workflow-response">{metrics === null ? 'Not available' : JSON.stringify(metrics, null, 2)}</pre></details>)}
        <div className="workflow-actions">{candidate.artifacts.map(item => <a key={`${item.sha256}-${item.name}`} href={href(job.id, item)} download={item.name}>{item.name}</a>)}</div>
      </article>)}
      {job.text && <><h3>Computed results and limits</h3><pre className="workflow-response">{job.text}</pre></>}
      <details><summary>All artifacts and audit downloads</summary>{job.artifacts.map(item => <div key={`${item.sha256}-${item.name}`}><a href={href(job.id, item)} download={item.name}>{item.name}</a></div>)}</details>
      <p className="small">Local signatures show integrity relative to local keys, not independent execution or the absence of off-ledger work. Record count is not candidate count.</p>
    </>}
  </section>;
}
