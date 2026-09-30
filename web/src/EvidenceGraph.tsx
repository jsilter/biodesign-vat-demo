import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Background, Controls, ReactFlow, useNodesState, useEdgesState, type Node, type Edge } from '@xyflow/react';
import ELK from 'elkjs/lib/elk.bundled.js';
import '@xyflow/react/dist/style.css';
import { LEAFLESS_ACTIONS, archiveBase64, evidenceRequest, graphElements, publicPolicyBody, type Action, type Evidence, type Graph, type GraphMode, type PublicPolicy } from './graph-data';

export function Canvas({ graph, mode, overlay, showOutputs, select }: { graph: Graph; mode: GraphMode; overlay: string; showOutputs: boolean; select: (id: string) => void }) {
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [layoutError, setLayoutError] = useState('');
  const [ready, setReady] = useState(false);
  useEffect(() => {
    let active = true;
    setReady(false); setLayoutError('');
    const elements = graphElements(graph, mode, overlay, showOutputs);
    if (elements.nodes.length > 500) {
      setLayoutError('This view has more than 500 objects. Use the searchable list below to inspect every disclosed object.');
      setNodes([]); setEdges([]);
      return;
    }
    const elk = new ELK();
    elk.layout({ id: 'root', layoutOptions: { 'elk.algorithm': 'layered', 'elk.direction': 'DOWN',
      'elk.spacing.nodeNode': '45', 'elk.layered.spacing.nodeNodeBetweenLayers': '60' },
      children: elements.nodes.map(n => ({ id: n.id, width: 260, height: 105 })),
      edges: elements.edges.map(e => ({ id: e.id, sources: [e.source], targets: [e.target] })),
    }).then(layout => {
      if (!active) return;
      const positions = new Map(layout.children?.map(n => [n.id, { x: n.x ?? 0, y: n.y ?? 0 }]));
      setNodes(elements.nodes.map(n => ({ ...n, position: positions.get(n.id)! })));
      setEdges(elements.edges); setReady(true);
    }).catch(() => { if (active) setLayoutError('Graph layout failed. The record list and verification results are still available.'); });
    // The bundled build runs without a Web Worker; discard stale layout completions.
    return () => { active = false; };
  }, [graph, mode, overlay, showOutputs, setNodes, setEdges]);
  if (layoutError) return <p role="status">{layoutError}</p>;
  return <div className="evidence-canvas" aria-label="Recorded computation graph">
    {ready ? <ReactFlow nodes={nodes} edges={edges} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange}
      nodesConnectable={false} edgesReconnectable={false} deleteKeyCode={null} fitView minZoom={0.1}
      onNodeClick={(_, node) => select(node.id)} onNodeDoubleClick={(_, node) => select(node.id)}>
      <Background gap={24} /><Controls showInteractive={false} />
    </ReactFlow> : <p role="status">Arranging recorded steps…</p>}
  </div>;
}

function saveDownload(raw: Blob, name: string) {
  const url = URL.createObjectURL(raw);
  const link = document.createElement('a'); link.href = url; link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function Reports({ graph, label }: { graph: Graph; label: string }) {
  return <section className="evidence-verdict" aria-label={label}><h3>{label}</h3>
    <div className="evidence-verdicts"><div><small>Bundle</small><strong>{graph.bundle_report.outcome}</strong></div>
      <div><small>Recorded lineage</small><strong>{graph.provenance_report.outcome}</strong></div>
      <div><small>Disclosure</small><strong>{graph.records.length} / {graph.tree_size}</strong></div></div>
    {graph.bundle_report.public_transparency && graph.bundle_report.public_transparency !== 'absent' && <dl className="evidence-public" aria-label="Public transparency">
      <dt>Local notarization</dt><dd>{graph.bundle_report.notarization}</dd>
      <dt>Public transparency</dt><dd>{graph.bundle_report.public_transparency}</dd>
      <dt>Signing identity</dt><dd>{graph.bundle_report.public_signing_identity}</dd>
      <dt>Rekor log ID / index</dt><dd>{graph.bundle_report.rekor_log}</dd>
      <dt>RFC 3161 time</dt><dd>{graph.bundle_report.rfc3161_timestamp}</dd></dl>}
    <details><summary>Python verification details</summary>
      <ol>{graph.bundle_report.checks.map(c => <li key={c.number}>{c.passed ? '✓' : '×'} {c.name}: {c.detail}</li>)}</ol>
      <p>{graph.provenance_report.unspecified_inputs} inputs have unspecified origins.</p>
      <pre>{JSON.stringify(graph.provenance_report, null, 2)}</pre>
    </details>
  </section>;
}

export default function EvidenceGraph() {
  const [original, setOriginal] = useState<Evidence | null>(null);
  const [current, setCurrent] = useState<Evidence | null>(null);
  const [archive, setArchive] = useState<File | null>(null);
  const [trust, setTrust] = useState<File | null>(null);
  const [sequence, setSequence] = useState('');
  const [policy, setPolicy] = useState<PublicPolicy>({ identity: '', issuer: '', trustRoot: null, require: false });
  const [mode, setMode] = useState<GraphMode>('Workflow');
  const [overlay, setOverlay] = useState('none');
  const [showOutputs, setShowOutputs] = useState(false);
  const [selected, setSelected] = useState('');
  const [search, setSearch] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [compare, setCompare] = useState(false);
  const [action, setAction] = useState<Action>({ kind: 'withhold' });
  const [applied, setApplied] = useState<Action | null>(null);
  const [preview, setPreview] = useState<{ digest: string; text: string | null; bytes_len: number; truncated: boolean; integrity: string } | null>(null);
  const previewGeneration = useRef(0);
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const retainedId = useRef<string | null>(null);
  useEffect(() => { previewGeneration.current++; setPreview(null); }, [current, selected]);
  const release = (id: string) => { void fetch(`/api/evidence/${id}`, { method: 'DELETE' }).catch(() => { /* Server bounds retained uploads even if cleanup fails. */ }); };
  useEffect(() => () => { generation.current++; controller.current?.abort(); if (retainedId.current) release(retainedId.current); }, []);

  function invalidate() {
    generation.current++; controller.current?.abort(); setBusy(false); setError('');
    setOriginal(null); setCurrent(null); setSelected(''); setCompare(false); setApplied(null);
    if (retainedId.current) { release(retainedId.current); retainedId.current = null; }
  }
  async function run(work: (signal: AbortSignal) => Promise<Evidence>, loaded = false, experiment: Action | null = null) {
    const ticket = ++generation.current;
    controller.current?.abort(); const next = new AbortController(); controller.current = next;
    setBusy(true); setError('');
    try {
      const result = await work(next.signal);
      if (ticket !== generation.current) { if (loaded) release(result.id); return; }
      if (loaded) {
        if (retainedId.current) release(retainedId.current);
        retainedId.current = result.id; setOriginal(result); setSelected(''); setCompare(false);
      }
      setCurrent(result); setApplied(experiment);
    } catch (err) {
      if (ticket === generation.current && !next.signal.aborted) setError(err instanceof Error ? err.message : 'Evidence could not be loaded.');
    } finally { if (ticket === generation.current) setBusy(false); }
  }
  async function upload(event: FormEvent) {
    event.preventDefault();
    if (!archive || !trust) return;
    if (archive.size > 64 * 1024 ** 2 || trust.size > 64 * 1024) { setError('Use an archive up to 64 MiB and public trust up to 64 KiB.'); return; }
    if (policy.trustRoot && policy.trustRoot.size > 1024 ** 2) { setError('Use a Sigstore trusted root up to 1 MiB.'); return; }
    await run(async signal => evidenceRequest('', { method: 'POST', signal, headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ archive: archiveBase64(new Uint8Array(await archive.arrayBuffer())), trust: JSON.parse(await trust.text()), sequence,
        ...(await publicPolicyBody(policy).then(value => value ? { public: value } : {})) }),
    }), true);
  }
  async function example(name: string) {
    invalidate();
    await run(async signal => {
      const [zip, keys] = await Promise.all([fetch(`${import.meta.env.BASE_URL}demo/${name}.zip`, { signal }), fetch(`${import.meta.env.BASE_URL}demo/${name}.trust.json`, { signal })]);
      if (!zip.ok || !keys.ok) throw new Error('Example evidence is unavailable. Regenerate the frontend fixtures.');
      return evidenceRequest('', { method: 'POST', signal, headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ archive: archiveBase64(new Uint8Array(await zip.arrayBuffer())), trust: await keys.json() }) });
    }, true);
  }
  function reset() {
    generation.current++; controller.current?.abort(); setBusy(false); setError(''); setCurrent(original); setApplied(null); setCompare(false);
  }
  async function download() {
    if (!current) return;
    try {
      const response = await fetch(`/api/evidence/${current.id}/download`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(applied ?? {}) });
      if (!response.ok) throw new Error('Download failed.');
      saveDownload(await response.blob(), 'evidence.zip');
    } catch (err) { setError(err instanceof Error ? err.message : 'Download failed.'); }
  }
  async function inspectArtifact(digest: string) {
    const ticket = ++previewGeneration.current;
    setPreview(null);
    try {
      const response = await fetch(`/api/evidence/${current!.id}/artifact`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ digest, action: applied }) });
      if (!response.ok) throw new Error('Artifact bytes are unavailable.');
      const value = await response.json();
      if (ticket === previewGeneration.current) setPreview(value);
    } catch (err) { if (ticket === previewGeneration.current) setError(err instanceof Error ? err.message : 'Artifact unavailable.'); }
  }
  const graph = current?.graph;
  const node = graph?.nodes.find(n => n.id === selected);
  const record = graph?.records.find(r => `record:${r.index}` === selected);
  const selectedArtifacts = record?.artifacts ?? graph?.records.filter(r => node?.leaves.includes(r.index)).flatMap(r => r.artifacts) ?? [];
  const leaf = original?.graph.records.find(r => r.index === action.leaf);
  const visible = graph?.nodes.filter(n => `${n.label} ${n.id}`.toLowerCase().includes(search.toLowerCase())) ?? [];

  return <section className="evidence-workspace" aria-label="Evidence graph workspace">
    <header className="page-heading"><div><span className="eyebrow">Recorded experiments</span><h1>Follow the evidence.</h1>
      <p>See which outputs fed which steps—and what changes break verification.</p></div></header>
    <form className="card evidence-upload" onSubmit={upload}>
      <label>Evidence ZIP<input type="file" accept=".zip" onChange={e => { invalidate(); setArchive(e.target.files?.[0] ?? null); }} /></label>
      <label>Public trust JSON<input type="file" accept=".json" onChange={e => { invalidate(); setTrust(e.target.files?.[0] ?? null); }} /></label>
      <label>Sequence to verify (optional)<input value={sequence} onChange={e => { invalidate(); setSequence(e.target.value); }} /></label>
      <details className="evidence-public-policy"><summary>Public Sigstore evidence (optional)</summary>
        <label>Expected signing identity<input value={policy.identity} onChange={e => { invalidate(); setPolicy({ ...policy, identity: e.target.value }); }} placeholder="name@project.iam.gserviceaccount.com" /></label>
        <label>Expected issuer<input value={policy.issuer} onChange={e => { invalidate(); setPolicy({ ...policy, issuer: e.target.value }); }} placeholder="https://accounts.google.com" /></label>
        <label>Sigstore trusted root JSON<input type="file" accept=".json" onChange={e => { invalidate(); setPolicy({ ...policy, trustRoot: e.target.files?.[0] ?? null }); }} /></label>
        <label><input type="checkbox" checked={policy.require} onChange={e => { invalidate(); setPolicy({ ...policy, require: e.target.checked }); }} /> Require public transparency</label>
        <p>The identity is your expectation, confirmed against the certificate; the bundle never supplies it. Without a trusted root file the backend uses its offline Sigstore cache.</p>
      </details>
      <button className="download" disabled={busy || !archive || !trust}>Open evidence</button>
      <p>Public trust is supplied by you. Never upload private keys. A ZIP alone does not establish who to trust.</p>
    </form>
    <div className="evidence-toolbar"><span>Or open an example:</span>{['clean', 'tampered', 'partial'].map(name => <button key={name} disabled={busy} onClick={() => void example(name)}>{name}</button>)}</div>
    {busy && <p role="status">Checking evidence…</p>}{error && <p className="notice" role="alert">{error}</p>}
    {!graph && <div className="notice quiet">For a real local fork/join, run <code>PYTHONPATH=src .venv/bin/python scripts/provenance_demo.py</code>, then open its bundle.zip and trust.json here. Bundles without provenance nodes show records without invented dependencies.</div>}
    {graph && <>
      <div className="evidence-toolbar" aria-label="Evidence views">{(['Workflow', 'Signatures', 'Falsification'] as const).map(value => <button key={value} aria-pressed={mode === value} onClick={() => setMode(value)}>{value}</button>)}
        <label>Record overlay<select value={overlay} onChange={e => setOverlay(e.target.value)}><option value="none">None</option><option value="order">Disclosed order</option><option value="chain">Hash chain</option></select></label>
        <button aria-pressed={showOutputs} onClick={() => setShowOutputs(!showOutputs)}>{showOutputs ? 'Group output values' : 'Expand output values'}</button>
        <button onClick={() => void download()}>Download evidence</button>
      </div>
      <Reports graph={graph} label={applied ? 'Modified disclosure' : 'Loaded disclosure'} />
      <p className="small">{graph.trust}. Lines show recorded claims; consult both verification reports. Moving boxes does not change evidence.</p>
      {mode === 'Falsification' && <section className="card evidence-experiment"><h3>Change one thing.</h3><p>Each experiment starts from the loaded bundle. Signatures and commitments stay unchanged.</p>
        <div className="evidence-toolbar"><label>Action<select value={action.kind} onChange={e => setAction({ ...action, kind: e.target.value })}>
          <option value="withhold">Withhold a record</option><option value="parameter">Change a recorded parameter</option><option value="result">Alter returned bytes</option>
          <option value="artifact">Alter artifact bytes</option><option value="remove_binding">Remove a data binding</option><option value="redirect_binding">Redirect a data binding</option><option value="narrative">Edit unsigned narrative</option>
          {original?.graph.public_evidence && <><option value="strip_public_evidence">Remove public Sigstore evidence</option><option value="retarget_public_evidence">Point public evidence at another receipt</option><option value="substitute_public_identity">Claim another signing identity</option></>}
        </select></label>
        {!LEAFLESS_ACTIONS.includes(action.kind) && <label>Record<select value={action.leaf ?? ''} onChange={e => setAction({ ...action, leaf: Number(e.target.value), digest: undefined })}>
          <option value="" disabled>Choose a leaf</option>{original?.graph.records.map(r => <option key={r.index} value={r.index}>Leaf {r.index} · {r.type}</option>)}</select></label>}
        {action.kind === 'artifact' && <label>Artifact<select value={action.digest ?? ''} onChange={e => setAction({ ...action, digest: e.target.value })}><option value="" disabled>Choose an artifact</option>{leaf?.artifacts.map((a, i) => <option key={i} value={a.sha256}>{a.name}</option>)}</select></label>}
        {action.kind === 'substitute_public_identity' && <label>Claimed identity<input value={action.value ?? ''} onChange={e => setAction({ ...action, value: e.target.value })} placeholder="someone@example.com" /></label>}
        {action.kind === 'redirect_binding' && <label>Producer node ID<input value={action.producer ?? ''} onChange={e => setAction({ ...action, producer: e.target.value })} /></label>}
        {['narrative', 'parameter'].includes(action.kind) && <label>New text<input value={action.value ?? ''} onChange={e => setAction({ ...action, value: e.target.value })} /></label>}
        <button disabled={busy || (!LEAFLESS_ACTIONS.includes(action.kind) && action.leaf === undefined)} onClick={() => void run(signal => evidenceRequest(`/${original!.id}/experiment`, { method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(action) }), false, { ...action })}>Apply and verify</button>
        <button onClick={reset}>Reset</button>
        {applied && <><button aria-pressed={compare} onClick={() => setCompare(!compare)}>Compare with original</button><button onClick={() => saveDownload(new Blob([JSON.stringify({ action: applied, description: current?.description }, null, 2)], { type: 'application/json' }), 'experiment.json')}>Download action</button></>}
        </div>{applied && <p>{current?.description}</p>}
      </section>}
      {compare && original && original.graph.session === graph.session && original.graph.root === graph.root && <Reports graph={original.graph} label="Original disclosure · same commitment" />}
      {graph.missing_count > 0 && <aside className="notice"><strong>{graph.missing_count} undisclosed positions.</strong><p>Known missing positions: {graph.gaps.join(', ')}{graph.missing_count > graph.gaps.length ? `; ${graph.missing_count - graph.gaps.length} more` : ''}. Their contents are unknown. A referenced producer is not assigned to a gap.</p></aside>}
      <Canvas graph={graph} mode={mode} overlay={overlay} showOutputs={showOutputs} select={setSelected} />
      <div className="evidence-inspection"><section className="card"><h3>Disclosed objects</h3><label>Find an object<input value={search} onChange={e => setSearch(e.target.value)} /></label>
        <div className="evidence-object-list">{visible.slice(0, 200).map(n => <button key={n.id} aria-pressed={selected === n.id} onClick={() => setSelected(n.id)}>{n.label}<small>{n.status ?? n.kind} · {n.id.slice(0, 12)}</small></button>)}</div>
        {visible.length > 200 && <p>{visible.length} matches. Refine the search to see more.</p>}
        <details><summary>All disclosed records</summary><div className="evidence-object-list">{graph.records.map(r => <button key={r.index} onClick={() => setSelected(`record:${r.index}`)}>Leaf {r.index}: {r.type} · membership {r.membership}</button>)}</div></details>
      </section><section className="card evidence-detail" aria-label="Selected evidence"><h3>{node?.label ?? (record ? `Leaf ${record.index}` : selected === 'session-head' ? 'Session commitment' : 'Inspect a step')}</h3>
        {node && <><p>Signature: {node.signature ?? 'not applicable'} · bytes: {node.integrity ?? 'not checked'} · embedding: {node.embedding ?? 'not checked'}</p><pre>{JSON.stringify(node, null, 2)}</pre></>}
        {record && <pre>{JSON.stringify(record, null, 2)}</pre>}
        {selectedArtifacts.length > 0 && <details><summary>Read disclosed artifacts</summary><div className="evidence-object-list">{selectedArtifacts.map((a, i) => <button key={i} onClick={() => void inspectArtifact(a.sha256)}>{a.name} · {a.bytes_len} bytes</button>)}</div></details>}
        {preview && <section aria-label="Artifact preview"><p>Bytes: {preview.integrity} · {preview.bytes_len}{preview.truncated ? ' · first 64 KiB shown' : ''}</p><pre>{preview.text ?? 'Binary artifact; preview unavailable.'}</pre></section>}
        {selected === 'session-head' && <><p>Recorder commitment: {graph.commitment}. Head and attestation commit to the same root and count.</p><pre>{JSON.stringify({ head: graph.head, attestation: graph.attestation }, null, 2)}</pre></>}
        {!selected && <p>Select a box or a list entry. Raw values are displayed as text.</p>}
      </section></div>
    </>}
    <p className="small">Signatures establish integrity under supplied keys—not independent execution, scientific acceptance, or the absence of off-ledger work.</p>
  </section>;
}
