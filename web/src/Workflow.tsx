import { useEffect, useRef, useState } from 'react';
import { z } from 'zod';

const snapshotSchema = z.object({
  id: z.uuid(), status: z.enum(['running', 'completed', 'failed', 'cancelled']),
  logs: z.array(z.object({ at: z.string(), message: z.string() })),
  text: z.string(), returned_model: z.string(),
  downloads: z.array(z.enum(['bundle.zip', 'trust.json', 'report.txt', 'response.txt'])),
});
type Snapshot = z.infer<typeof snapshotSchema>;
const modelsSchema = z.object({ models: z.array(z.object({ id: z.string(), name: z.string() })), configured: z.boolean() });

async function request(path: string, body?: unknown, signal?: AbortSignal) {
  const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000) });
  if (!response.ok) throw new Error(`Local workflow server returned HTTP ${response.status}.`);
  return response.json();
}

export function Workflow() {
  const [prompt, setPrompt] = useState('');
  const [model, setModel] = useState('');
  const [catalog, setCatalog] = useState<z.infer<typeof modelsSchema> | null>(null);
  const [catalogAttempt, setCatalogAttempt] = useState(0);
  const [catalogError, setCatalogError] = useState('');
  const [job, setJob] = useState<Snapshot | null>(null);
  const [pending, setPending] = useState<{ id: string; prompt: string; model: string } | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const submitting = useRef(false);
  const logView = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    setCatalogError('');
    request('/api/models', undefined, controller.signal).then(raw => {
      const value = modelsSchema.parse(raw);
      if (!controller.signal.aborted) {
        setCatalog(value);
        setModel(current => value.models.some(item => item.id === current) ? current
          : value.models.find(item => item.id === 'anthropic/claude-opus-4.8')?.id || value.models[0]?.id || '');
      }
    }).catch(() => { if (!controller.signal.aborted) setCatalogError('Could not load AI models. Check that the backend is running, then retry.'); });
    return () => controller.abort();
  }, [catalogAttempt]);

  useEffect(() => {
    if (!job || job.status !== 'running') return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = snapshotSchema.parse(await request(`/api/workflows/${job.id}`, undefined, controller.signal));
        if (!controller.signal.aborted) { setJob(next); setError(''); }
      } catch { if (!controller.signal.aborted) setError('Progress connection interrupted. Reconnecting; Go remains disabled.'); }
      if (!controller.signal.aborted) timer = setTimeout(poll, 1000);
    };
    timer = setTimeout(poll, 1000);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [job?.id, job?.status]);

  useEffect(() => { logView.current?.scrollTo?.(0, logView.current.scrollHeight); }, [job?.logs]);

  const active = busy || pending !== null || job?.status === 'running';
  async function start(recover = false) {
    if (submitting.current) return;
    submitting.current = true;
    const payload = recover && pending ? pending : { id: crypto.randomUUID(), prompt, model };
    setPending(payload); setBusy(true); setError('');
    if (!recover) setJob(null);
    try {
      setJob(snapshotSchema.parse(await request('/api/workflows', payload)));
      setPending(null);
    } catch { setError('Start could not be confirmed. Recover the same request safely; it will not start a duplicate job.'); }
    finally { submitting.current = false; setBusy(false); }
  }

  async function cancel() {
    if (!job || !window.confirm('Cancel this workflow? Provider charges already incurred cannot be reversed.')) return;
    setCancelling(true);
    try { setJob(snapshotSchema.parse(await request(`/api/workflows/${job.id}/cancel`, {}))); setError(''); }
    catch { setError('Cancellation could not be confirmed. The workflow may still be running; retry cancellation.'); }
    finally { setCancelling(false); }
  }

  return <section className="card workflow" aria-label="Live Designer workflow">
    <span className="eyebrow">Live research</span><h2>What do you want to research?</h2>
    <p>The AI can search papers and summarize what it finds. You’ll see its progress here, then get an answer and a downloadable record.</p>
    <p className="small">Research only: this demo does not generate protein sequences or check biological safety.</p>
    <label htmlFor="workflow-prompt">1. Enter your question</label>
    <textarea id="workflow-prompt" rows={4} value={prompt} disabled={active} onChange={event => setPrompt(event.target.value)} placeholder="For example: Find recent papers on my target protein and summarize the evidence." />
    <label htmlFor="workflow-model">2. Choose an AI model</label>
    <select id="workflow-model" value={model} disabled={active || !catalog} onChange={event => setModel(event.target.value)}>
      {!catalog && <option value="">Loading OpenAI and Anthropic models…</option>}
      {catalog?.models.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
    </select>
    {catalogError && <p role="alert">{catalogError} <button onClick={() => setCatalogAttempt(value => value + 1)}>Retry models</button></p>}
    {catalog && !catalog.configured && <div className="notice"><p>Live research needs an API key. You can still explore the examples below.</p><details><summary>How to enable live research</summary><p>Set OPENROUTER_API_KEY in the repository root .env, then restart with .venv/bin/python scripts/dev.py.</p></details></div>}
    <label>3. Start research</label>
    <p className="small">Go sends your question to OpenRouter and searches to public paper services. Model usage costs money; automatic timeout retries may add charges.</p>
    <div className="workflow-actions"><button className="download" disabled={active || !prompt.trim() || !model || !catalog?.configured} onClick={() => void start()}>Go</button>
      {job?.status === 'running' && <button disabled={cancelling} onClick={() => void cancel()}>{cancelling ? 'Cancelling…' : 'Cancel workflow'}</button>}
      {pending && !busy && <button onClick={() => void start(true)}>Recover request</button>}
      {catalog && !catalog.configured && <button onClick={() => setCatalogAttempt(value => value + 1)}>Retry models</button>}
    </div>
    <p role="status">{busy ? 'Starting…' : job ? `Workflow ${job.status}${job.status === 'completed' ? ' · research only' : ''}` : catalog?.configured ? 'Ready' : catalog ? 'Live workflow unavailable · API key required' : 'Connecting to workflow backend…'}</p>
    {error && <p role="alert">{error}</p>}
    {job && <><h3>Progress</h3><div className="workflow-logs" role="log" aria-label="Most recent workflow logs" ref={logView}>
      {job.logs.map((entry, index) => <div key={index}><time>{entry.at}</time> {entry.message}</div>)}
    </div><small>Download your results before restarting the server.</small></>}
    {job?.status === 'completed' && <><h3>Your answer</h3><pre className="workflow-response">{job.text}</pre>
      <h3>Save your results</h3><p>The files include the answer and a signed record of the AI calls and searches.</p>
      <div className="workflow-actions">{job.downloads.map(name => <a className="download" key={name} href={`/api/workflows/${job.id}/files/${name}`} download>{name}</a>)}</div></>}
    <details className="raw"><summary>Sources and demo limits</summary><p>Paper searches use PubMed, Europe PMC and bioRxiv. Preprints are not peer-reviewed. The AI receives metadata and abstracts, not full papers.</p><p>Records are signed locally, not by an independent observer. Tool errors are recorded too. No protein sequence is generated or verified.</p>{job && <p>Run ID: <code>{job.id}</code>. Model: <code>{job.returned_model || model}</code>.</p>}</details>
  </section>;
}
