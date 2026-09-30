import { useState } from 'react';
import { Canvas, Reports } from './EvidenceGraph';
import { graphSchema, type Graph, type GraphMode } from './graph-data';

export const EMBEDDED_GRAPH_ID = 'vat-evidence';

/** Read the graph the Python projector embedded at render time; the page never fetches. */
export function embeddedGraph(root: Document = document): Graph {
  const element = root.getElementById(EMBEDDED_GRAPH_ID);
  if (!element?.textContent) throw new Error('This page carries no embedded evidence graph.');
  return graphSchema.parse(JSON.parse(element.textContent));
}

/** The read-only half of the evidence workspace: verdicts, graph and object inspection.
 *  Falsification, downloads and artifact previews need the backend and are absent here. */
export default function StaticEvidence({ graph, title }: { graph: Graph; title: string }) {
  const [mode, setMode] = useState<GraphMode>('Workflow');
  const [overlay, setOverlay] = useState('none');
  const [showOutputs, setShowOutputs] = useState(false);
  const [selected, setSelected] = useState('');
  const [search, setSearch] = useState('');
  const node = graph.nodes.find(n => n.id === selected);
  const record = graph.records.find(r => `record:${r.index}` === selected);
  const visible = graph.nodes.filter(n => `${n.label} ${n.id}`.toLowerCase().includes(search.toLowerCase()));
  return <section className="evidence-workspace" aria-label="Evidence graph workspace">
    <header className="page-heading"><div><span className="eyebrow">Recorded experiments</span><h1>{title}</h1>
      <p>A rendered snapshot of one verification. The verdicts below were computed by the Python verifier when this page was written; the page itself verifies nothing.</p></div></header>
    <div className="evidence-toolbar" aria-label="Evidence views">{(['Workflow', 'Signatures'] as const).map(value => <button key={value} aria-pressed={mode === value} onClick={() => setMode(value)}>{value}</button>)}
      <label>Record overlay<select value={overlay} onChange={e => setOverlay(e.target.value)}><option value="none">None</option><option value="order">Disclosed order</option><option value="chain">Hash chain</option></select></label>
      <button aria-pressed={showOutputs} onClick={() => setShowOutputs(!showOutputs)}>{showOutputs ? 'Group output values' : 'Expand output values'}</button>
    </div>
    <Reports graph={graph} label="Loaded disclosure" />
    <p className="small">{graph.trust}. Lines show recorded claims; consult both verification reports. Moving boxes does not change evidence.</p>
    {graph.missing_count > 0 && <aside className="notice"><strong>{graph.missing_count} undisclosed positions.</strong><p>Known missing positions: {graph.gaps.join(', ')}{graph.missing_count > graph.gaps.length ? `; ${graph.missing_count - graph.gaps.length} more` : ''}. Their contents are unknown. A referenced producer is not assigned to a gap.</p></aside>}
    <Canvas graph={graph} mode={mode} overlay={overlay} showOutputs={showOutputs} select={setSelected} />
    <div className="evidence-inspection"><section className="card"><h3>Disclosed objects</h3><label>Find an object<input value={search} onChange={e => setSearch(e.target.value)} /></label>
      <div className="evidence-object-list">{visible.slice(0, 200).map(n => <button key={n.id} aria-pressed={selected === n.id} onClick={() => setSelected(n.id)}>{n.label}<small>{n.status ?? n.kind} · {n.id.slice(0, 12)}</small></button>)}</div>
      {visible.length > 200 && <p>{visible.length} matches. Refine the search to see more.</p>}
      <details><summary>All disclosed records</summary><div className="evidence-object-list">{graph.records.map(r => <button key={r.index} onClick={() => setSelected(`record:${r.index}`)}>Leaf {r.index}: {r.type} · membership {r.membership}</button>)}</div></details>
    </section><section className="card evidence-detail" aria-label="Selected evidence"><h3>{node?.label ?? (record ? `Leaf ${record.index}` : selected === 'session-head' ? 'Session commitment' : 'Inspect a step')}</h3>
      {node && <><p>Signature: {node.signature ?? 'not applicable'} · bytes: {node.integrity ?? 'not checked'} · embedding: {node.embedding ?? 'not checked'}</p><pre>{JSON.stringify(node, null, 2)}</pre></>}
      {record && <pre>{JSON.stringify(record, null, 2)}</pre>}
      {record && record.artifacts.length > 0 && <p>Artifact bytes are not embedded; read them from the bundle with the connector or the backend.</p>}
      {selected === 'session-head' && <><p>Recorder commitment: {graph.commitment}. Head and attestation commit to the same root and count.</p><pre>{JSON.stringify({ head: graph.head, attestation: graph.attestation }, null, 2)}</pre></>}
      {!selected && <p>Select a box or a list entry. Raw values are displayed as text.</p>}
    </section></div>
    <p className="small">Signatures establish integrity under supplied keys—not independent execution, scientific acceptance, or the absence of off-ledger work.</p>
  </section>;
}
