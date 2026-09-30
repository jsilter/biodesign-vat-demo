import { lazy, Suspense, useEffect, useState, type KeyboardEvent, type ReactNode } from 'react';
import { loadCatalog, previewKinds, roles, stories, titles, type AuditRecord, type Catalog, type Role, type Scenario } from './data';
import { Workflow } from './Workflow';
import { DesignWorkflow } from './DesignWorkflow';
const EvidenceGraph = lazy(() => import('./EvidenceGraph'));

function passed(scenario: Scenario, number: number) {
  return scenario.report.checks.some(check => check.number === number && check.passed);
}

function ArtifactView({ artifact, scenario }: { artifact: AuditRecord['inputs'][number]; scenario: Scenario }) {
  const view = scenario.artifacts[artifact.sha256];
  return <details className="artifact">
    <summary><span>{artifact.kind} <small>{artifact.name}</small></span><span>{artifact.bytes_len} B</span></summary>
    <code>{artifact.sha256}</code>
    <p>{view.available ? `Bytes available · ${passed(scenario, 4) ? 'integrity checked by Python' : 'integrity not established'}` : 'Bytes withheld (digest only)'}</p>
    {previewKinds.has(artifact.kind) && view.text !== null ? <pre>{view.text}</pre> : <p>No text preview.</p>}
  </details>;
}

function instructionStatus(scenario: Scenario) {
  const opening = scenario.bundle.records.find(r => r.link.leaf_index === 0);
  if (!opening) return 'Unknown: opening leaf not disclosed';
  const refs = opening.inputs.filter(a => a.kind === 'prompt');
  if (!refs.length) return 'Absent from disclosed opening';
  return refs.every(a => scenario.artifacts[a.sha256].available) ? 'Bytes available' : 'Bytes withheld (all or some)';
}

function JsonDetails({ label, value }: { label: string; value: unknown }) {
  return <details className="raw"><summary>{label}</summary><pre>{JSON.stringify(value, null, 2)}</pre></details>;
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <div className="field"><dt>{label}</dt><dd>{children}</dd></div>;
}

function KeyCard({ scenario, role }: { scenario: Scenario; role: Exclude<Role, 'verifier'> }) {
  const entry = scenario.trust.entries.find(item => item.role === role);
  return <aside className="card key-card"><span className="eyebrow">Local identity</span><h3>{role} key</h3>
    <p>A distinct Ed25519 key for this fixture. Public trust is supplied with the demo, not independently established.</p>
    <dl><Field label="Key identifier"><code>{entry?.keyid ?? 'No listed key'}</code></Field>
      <Field label="Algorithm">Ed25519 · software-rooted</Field>
      <Field label="Public key"><code>{entry?.public_key ?? 'No listed key'}</code></Field>
      <Field label="Private key">Never exported to this UI</Field></dl>
  </aside>;
}

function Designer({ scenario }: { scenario: Scenario }) {
  const opening = scenario.bundle.records.find(record => record.link.leaf_index === 0);
  const statement = opening?.designer_statement;
  return <div className="two-column"><section className="card"><span className="eyebrow">01 / Signed declaration</span>
    <h3>The original request</h3><p>The designer signs the purpose of this session. It becomes the first record in the log.</p>
    <dl><Field label="Stated purpose">{statement?.purpose_tag ?? 'Opening statement not disclosed'}</Field>
      <Field label="Session"><code>{scenario.bundle.session_uuid}</code></Field>
      <Field label="Verifier’s identity result"><code>{scenario.report.designer_identity}</code></Field></dl>
    {!opening && <div className="notice">The opening leaf is missing. This view cannot establish the designer’s statement from the disclosed bundle.</div>}
    {statement && <JsonDetails label="Inspect designer statement" value={statement} />}
    <h4>Session instructions</h4><p>{instructionStatus(scenario)}</p>
    {opening?.inputs.filter(a => a.kind === 'prompt').map((a, i) => <ArtifactView key={i} artifact={a} scenario={scenario} />)}
    <div className="inset"><span className="eyebrow">Submitted output · synthetic fixture</span><code className="sequence">{scenario.sequence}</code>
      <p>The Python verifier checks that the submitted sequence exactly matches a recorded output.</p></div>
  </section><KeyCard scenario={scenario} role="designer" /></div>;
}

function RecordDetail({ record, scenario }: { record: AuditRecord; scenario: Scenario }) {
  const artifacts = [...record.inputs, ...record.intermediates, ...record.results];
  return <section className="card inspector" aria-label={`Record ${record.link.leaf_index} details`}>
    <span className="eyebrow">Leaf {String(record.link.leaf_index).padStart(2, '0')} / Disclosed content</span>
    <h3>{record.record_type.replaceAll('_', ' ')}</h3>
    <p className="small">Displayed bytes are evidence under review. Read the verifier result before treating them as authenticated.</p>
    <dl><Field label="Recorded purpose">{record.user_session.purpose_tag}</Field></dl>
    {record.params.objectives.length > 0 && <><h4>Recorded objectives</h4><ul className="objectives">
      {record.params.objectives.map((objective, index) => <li key={index}><code>{objective.name}</code><span>{objective.direction} · {objective.weight}</span></li>)}
    </ul></>}
    <h4>Artifact commitments <span className="muted">/ {artifacts.length}</span></h4>
    {artifacts.length === 0 && <p>No artifacts attached to this record.</p>}
    {artifacts.map((artifact, index) => <ArtifactView key={index} artifact={artifact} scenario={scenario} />)}
    <JsonDetails label="Inspect raw record" value={record} />
  </section>;
}

function Recorder({ scenario, selected, onSelect }: { scenario: Scenario; selected: number; onSelect: (index: number) => void }) {
  const record = scenario.bundle.records.find(item => item.link.leaf_index === selected);
  return <><div className="two-column records-layout"><section className="card"><span className="eyebrow">02 / Append-only log</span>
    <h3>The activity log</h3><p>Select a record to see its details. The recorder links and signs the log so later changes can be detected.</p>
    <div className="record-list">{scenario.bundle.records.map(item => <button className="record-row" key={item.link.leaf_index} aria-pressed={selected === item.link.leaf_index} onClick={() => onSelect(item.link.leaf_index)}>
      <span className="record-index">{String(item.link.leaf_index).padStart(2, '0')}</span><span>{item.record_type.replaceAll('_', ' ')}<small>{item.results.length} result artifacts</small></span><span aria-hidden="true">↗</span>
    </button>)}</div>
    <p className="small">Counts are record leaves, not design runs. A call and a run can occupy separate leaves.</p>
    <JsonDetails label="Inspect signed tree head" value={scenario.bundle.signed_tree_head} />
    <JsonDetails label="Inspect recorder attestation" value={scenario.bundle.attestation} />
  </section>{record ? <RecordDetail record={record} scenario={scenario} /> : <section className="card"><h3>Record not disclosed</h3><p>Its contents and type are unknown.</p></section>}</div>
  <div className="notice quiet">The fixture declares <code>serverless_hosted</code> capture. This is synthetic metadata, not evidence of a real Modal deployment or independent capture.</div></>;
}

function Notary({ scenario }: { scenario: Scenario }) {
  const receipt = scenario.bundle.notary_receipt;
  if (!receipt) return <section className="card"><h3>No receipt included</h3><p>No notary verification is established by this bundle.</p></section>;
  const check = scenario.report.checks.find(c => c.number === 2);
  return <div className="two-column"><section className="card"><span className="eyebrow">03 / Simulated receipt</span>
    <h3>The signed receipt</h3><p>The demo notary checked the original log and signed a receipt for it. This does not mean the design is safe.</p>
    <div className="notice"><strong>Simulation, not independent notarization.</strong><br />No sequence screening. No independent timestamp. All four local signers run on this laptop.</div>
    <dl><Field label="Policy"><code>{receipt.policy_id}</code></Field>
      <Field label="Receipt’s original policy result">{receipt.validation_result} · full original chain</Field>
      <Field label="Current bundle’s notary verification">{!check ? 'Not run' : check.passed ? 'Verified under supplied local trust' : 'Receipt verification not established'}</Field>
      <Field label="Committed record count">{receipt.tree_size}</Field>
      <Field label="Root bound by receipt"><code>{receipt.head_root}</code></Field></dl>
    <JsonDetails label="Inspect notary receipt" value={receipt} />
    <p>Transparency: {scenario.report.transparency}. Consistency: {scenario.report.consistency}.</p>
    {scenario.bundle.transparency_evidence && <>
      <p>Log entry {scenario.bundle.transparency_evidence.entry.leaf_index} · checkpoint size {scenario.bundle.transparency_evidence.checkpoint.tree_size}</p>
      <JsonDetails label="Inspect local transparency evidence" value={scenario.bundle.transparency_evidence} />
      <p className="small">A previously saved checkpoint lets an observer check subsequent growth. Locally supplied history alone does not establish independent publication or time.</p>
    </>}
  </section><KeyCard scenario={scenario} role="notary" /></div>;
}

const checkNames = ['Hash chain', 'Role-bound signatures', 'Merkle inclusion', 'Exact output binding', 'Session completeness'];

function Verifier({ scenario }: { scenario: Scenario }) {
  const report = scenario.report;
  return <><div className="two-column"><section className="card"><span className="eyebrow">04 / Deterministic verification</span>
    <div className="section-heading"><h3>Five checks. Distinct claims.</h3><span className={`badge ${report.outcome === 'OK' ? 'good' : 'warning'}`}>{report.outcome} · exit {report.exit_code}</span></div>
    <p>Recorded Python verifier output for the downloadable bundle and submitted sequence. No cryptography runs in the browser.</p>
    <ol className="checks">{checkNames.map((name, index) => {
      const check = report.checks.find(item => item.number === index + 1);
      return <li key={name} className={check ? check.passed ? 'passed' : 'failed' : 'not-run'}>
        <span className="check-mark" aria-hidden="true">{check ? check.passed ? '✓' : '×' : '−'}</span>
        <div><h4>{index + 1}. {name}{index === 4 && <small>Project extension</small>}</h4><p>{check?.detail ?? 'Not run: an earlier check failed.'}</p></div>
        <span className="check-status">{check ? check.passed ? 'Pass' : 'Fail' : 'Not run'}</span>
      </li>;
    })}</ol>
    {scenario.id === 'partial' && <p className="small">The hash-chain check covers only adjacent revealed records. It does not prove the hidden links across missing leaves.</p>}
  </section><aside className="card finding"><span className="eyebrow">Reviewer’s lens · curated fixture note</span>
    <h3>{scenario.id === 'doctored' ? 'Authentic ≠ acceptable.' : scenario.id === 'partial' ? 'A member is not the set.' : scenario.id === 'tampered' ? 'The evidence changed.' : 'Provenance, not permission.'}</h3>
    <p>{stories[scenario.id].finding}</p>
    <dl><Field label="Signer roles">{report.signer_roles}</Field><Field label="Designer identity"><code>{report.designer_identity}</code></Field>
      <Field label="Transcript availability (revealed records)">{report.transcripts.present} present · {report.transcripts.withheld} withheld · {report.transcripts.absent} absent</Field>
      <Field label="Reasoning availability (revealed records)">{report.reasoning.present} present · {report.reasoning.withheld} withheld · {report.reasoning.absent} absent</Field>
      <Field label="Session instructions">{instructionStatus(scenario)}</Field>
      <Field label="Revealed record types">{Object.entries(report.leaf_counts).map(([kind, count]) => `${kind}: ${count}`).join(' · ')} · {passed(scenario, 3) ? 'membership checked' : 'unverified contents'}</Field>
      <Field label="Capture metadata">{report.capture_tier} · synthetic fixture assertion</Field>
      <Field label="Independent time">{report.independent_timestamp}</Field></dl>
    <p className="small">A transcript hash commits to supplied bytes. It does not prove a complete or truthful account.</p>
  </aside></div><details className="raw"><summary>Inspect full Python verification report</summary><pre>{scenario.report_text}</pre></details></>;
}

function Workspace({ scenario, role, setRole }: { scenario: Scenario; role: Role; setRole: (role: Role) => void }) {
  const [selected, setSelected] = useState(scenario.bundle.records[0]?.link.leaf_index ?? 0);
  const head = scenario.bundle.signed_tree_head;
  const signatureChecked = scenario.report.checks.some(check => check.number === 2 && check.passed);
  const selectRecord = (index: number) => { setSelected(index); setRole('recorder'); };
  const navigateTab = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const next = event.key === 'ArrowRight' ? (index + 1) % 4 : event.key === 'ArrowLeft' ? (index + 3) % 4 : event.key === 'Home' ? 0 : event.key === 'End' ? 3 : null;
    if (next !== null) { event.preventDefault(); setRole(roles[next]); document.getElementById(`tab-${roles[next]}`)?.focus(); }
  };
  return <>
    <div className="workspace-bar"><div><span className="eyebrow">Example audit trail · {stories[scenario.id].name}</span><p>Pre-built example, separate from live research</p></div><span className="badge neutral">{scenario.report.outcome} · exit {scenario.report.exit_code}</span></div>
    <div className="role-tabs" role="tablist" aria-label="Audit roles">{roles.map((item, index) => <button key={item} id={`tab-${item}`} role="tab" aria-selected={role === item} aria-controls={`panel-${item}`} tabIndex={role === item ? 0 : -1} onClick={() => setRole(item)} onKeyDown={event => navigateTab(event, index)}>
      <span className="tab-number">0{index + 1}</span><span>{item}</span><span className="tab-arrow" aria-hidden="true">↗</span>
    </button>)}</div>
    <section className="role-panel" id={`panel-${role}`} role="tabpanel" aria-labelledby={`tab-${role}`} tabIndex={0}>
      <header className="page-heading"><div><span className="eyebrow">{stories[scenario.id].label}</span><h1>{titles[role]}</h1><p>{stories[scenario.id].description}</p></div>
        <a className="download" href={`${import.meta.env.BASE_URL}demo/${scenario.id}.zip`} download>Download bundle <span aria-hidden="true">↓</span></a></header>
      <section className="commitment" aria-label="Record disclosure"><div className="commitment-heading"><div><span className="eyebrow">Committed session</span><p><strong>{scenario.bundle.records.length}</strong> disclosed <span className="muted">/ {head.tree_size} {signatureChecked ? 'records in signed head' : 'records claimed by head'}</span></p></div><div className="root-label"><span className="eyebrow">Merkle root {signatureChecked ? '' : '· not yet verified'}</span><details><summary><code>{head.root_hash.slice(0, 24)}…</code></summary><code>{head.root_hash}</code></details></div></div>
        <p className="small">{passed(scenario, 3) ? 'Disclosed record membership checked by Python.' : 'Disclosed record membership not established.'} Read-only local simulation.</p>
        <div className="leaf-strip">{Array.from({ length: head.tree_size }, (_, index) => {
          const disclosed = scenario.bundle.records.some(record => record.link.leaf_index === index);
          return <button key={index} disabled={!disclosed} onClick={() => selectRecord(index)} className={disclosed ? 'disclosed' : 'withheld'} aria-label={`Record ${index}: ${disclosed ? 'disclosed, inspect' : 'not disclosed, type unknown'}`}><span>{String(index).padStart(2, '0')}</span><small>{disclosed ? 'Disclosed' : 'Unknown'}</small></button>;
        })}</div>
        <p className="small">{scenario.report.unrevealed_leaves ? `${scenario.report.unrevealed_leaves} undisclosed positions. Missing record types are unknown.` : 'Every position is disclosed. Verification determines whether its contents are authentic.'}</p>
      </section>
      {role === 'designer' && <Designer scenario={scenario} />}
      {role === 'recorder' && <Recorder scenario={scenario} selected={selected} onSelect={setSelected} />}
      {role === 'notary' && <Notary scenario={scenario} />}
      {role === 'verifier' && <Verifier scenario={scenario} />}
    </section>
    <section className="evidence-downloads" aria-label="Independent verification files"><div><span className="eyebrow">Take the evidence with you</span><p>Verify the ZIP locally with the supplied public trust and sequence.</p></div><div>
      <a href={`${import.meta.env.BASE_URL}demo/${scenario.id}.trust.json`} download>Public trust ↗</a>
      <a href={`${import.meta.env.BASE_URL}demo/${scenario.id}.sequence.txt`} download>Sequence ↗</a></div>
      <details><summary>Local verification command & archive digest</summary><p>From the repository root, with the downloaded files in the current directory. The sequence argument is literal text, not a filename (Bash command):</p><pre>{`PYTHONPATH=src .venv/bin/python -m vatlog.cli verify ${scenario.id}.zip --trust ${scenario.id}.trust.json --require-transparency --sequence "$(< ${scenario.id}.sequence.txt)"`}</pre><code>ZIP SHA-256: {scenario.archive_sha256}</code><p className="small">The digest and displayed report are unsigned presentation data. Supplied demo keys do not establish independent identity. Head-age checking is not enabled in this walkthrough.</p></details>
    </section>
  </>;
}

export function App() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [scenarioId, setScenarioId] = useState<Scenario['id']>('clean');
  const [role, setRole] = useState<Role>('designer');
  const [designOpened, setDesignOpened] = useState(false);
  const [evidenceOpened, setEvidenceOpened] = useState(false);
  // Once opened, the graph workspace stays mounted while hidden so loaded evidence survives tab changes.
  const [evidenceMounted, setEvidenceMounted] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    loadCatalog(controller.signal).then(data => {
      if (!controller.signal.aborted) {
        for (const scenario of data.scenarios) scenario.bundle.records.sort((a, b) => a.link.leaf_index - b.link.leaf_index);
        setCatalog(data);
      }
    })
      .catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, [attempt]);
  const scenario = catalog?.scenarios.find(item => item.id === scenarioId);
  return <div className="app-shell"><aside className="sidebar"><a className="brand" href="./" aria-label="VAT demo home"><span className="brand-symbol" aria-hidden="true">v.</span><span>VAT<span className="brand-subtitle">Verifiable audit trail</span></span></a>
    <div className="sidebar-intro"><span className="eyebrow">Biodesign demo</span><h2>See what the AI did.</h2><p>Run research in Designer, or explore an example below.</p></div>
    <nav className="scenario-nav" aria-label="Demo scenarios"><span className="eyebrow">Explore a scenario</span>{(Object.keys(stories) as Scenario['id'][]).map(id => <button key={id} aria-pressed={id === scenarioId} onClick={() => setScenarioId(id)}><span className="scenario-number">{stories[id].label.split(' / ')[0]}</span><span>{stories[id].name}<small>{id === 'clean' ? 'Establish the baseline' : id === 'tampered' ? 'Detect a changed record' : id === 'doctored' ? 'Read beyond the signature' : 'Account for the whole set'}</small></span><span aria-hidden="true">→</span></button>)}</nav>
    <div className="sidebar-note"><span className="status-dot" /><strong>About the examples</strong><p>These are pre-built examples, not your live results. Switch roles to see the request, records, receipt, and checks.</p></div>
    <footer className="sidebar-footer">BOSTON · BIOLOGY × COMPUTATION<br /><span>Reference implementation / 2026</span></footer>
  </aside><main>
    <nav className="evidence-navigation" aria-label="Workspace"><button aria-pressed={!evidenceOpened} onClick={() => setEvidenceOpened(false)}>Research & examples</button><button aria-pressed={evidenceOpened} onClick={() => { setEvidenceOpened(true); setEvidenceMounted(true); }}>Evidence graph</button></nav>
    <div hidden={!evidenceOpened}>{evidenceMounted && <Suspense fallback={<p role="status">Loading graph workspace…</p>}><EvidenceGraph /></Suspense>}</div>
    <div hidden={evidenceOpened}>
        {catalog && <div hidden={role !== 'designer' || error}>
          <div className="workflow-actions workflow-mode"><button className="download" onClick={() => setDesignOpened(!designOpened)}>{designOpened ? 'Return to research' : 'Open protein design'}</button></div>
          <div hidden={designOpened}><Workflow /></div><div hidden={!designOpened}><DesignWorkflow /></div>
        </div>}
    {error ? <section className="loading" role="alert"><span className="eyebrow">Demo unavailable</span><h1>Evidence could not be loaded.</h1><p>The catalog is missing, malformed, or unavailable. Stop the server, run <code>npm run fixtures</code> in <code>web/</code> (or rebuild production), then restart and retry.</p><button className="download" onClick={() => { setCatalog(null); setError(false); setAttempt(value => value + 1); }}>Retry loading</button></section>
      : scenario ? <Workspace key={scenario.id} scenario={scenario} role={role} setRole={setRole} />
        : <section className="loading" role="status"><span className="eyebrow">Opening the evidence workspace</span><h1>Loading demo evidence…</h1></section>}
    </div>
    <footer className="assurance-footer"><strong>Integrity has a boundary.</strong><p>Local signatures do not prove independent execution capture, independent timing, or the absence of off-ledger work. Completeness refers only to the committed session.</p>{catalog && <small>Evidence generated {new Date(catalog.generated_at).toISOString()} · Python verifier · synthetic fixtures</small>}</footer>
  </main></div>;
}
