import { z } from 'zod';
import { MarkerType, type Edge, type Node } from '@xyflow/react';

const state = z.enum(['passed', 'failed', 'unchecked', 'skipped']);
const edge = z.object({ id: z.string(), source: z.string(), target: z.string(), kind: z.string(), label: z.string() });
const artifact = z.object({ name: z.string(), kind: z.string(), sha256: z.string(), bytes_len: z.number() });
export const graphSchema = z.object({
  session: z.string(), root: z.string(), tree_size: z.number(), commitment: state, trust: z.string(),
  bundle_report: z.object({ outcome: z.string(), checks: z.array(z.object({ number: z.number(), name: z.string(), passed: z.boolean(), detail: z.string() })),
    notarization: z.string().optional(), public_transparency: z.string().optional(), public_signing_identity: z.string().nullable().optional(),
    rekor_log: z.string().nullable().optional(), rfc3161_timestamp: z.string().nullable().optional() }).passthrough(),
  public_evidence: z.record(z.string(), z.unknown()).nullable().optional(),
  provenance_report: z.object({ outcome: z.string(), checks: z.array(z.record(z.string(), z.unknown())), unspecified_inputs: z.number() }),
  nodes: z.array(z.object({ id: z.string(), kind: z.string(), label: z.string(), leaves: z.array(z.number()),
    status: z.string().optional(), signature: state.optional(), integrity: state.optional(), embedding: state.optional(),
    memberships: z.array(z.object({ leaf: z.number(), status: state })),
    signers: z.array(z.string()).optional(), statement: z.record(z.string(), z.unknown()).optional(),
  }).passthrough()).max(100000),
  edges: z.array(edge).max(100000), record_edges: z.array(edge).max(20000),
  records: z.array(z.object({ index: z.number(), type: z.string(), membership: state,
    artifacts: z.array(artifact), params: z.record(z.string(), z.unknown()),
  })).max(10000), gaps: z.array(z.number()).max(100), missing_count: z.number(),
  head: z.record(z.string(), z.unknown()), attestation: z.record(z.string(), z.unknown()),
});
export const evidenceSchema = z.object({ id: z.uuid(), graph: graphSchema, description: z.string().optional() });
export type Graph = z.infer<typeof graphSchema>;
export type Evidence = z.infer<typeof evidenceSchema>;
export type GraphMode = 'Workflow' | 'Signatures' | 'Falsification';
export type Action = { kind: string; leaf?: number; value?: string; digest?: string; producer?: string };
export const PUBLIC_ACTIONS = ['strip_public_evidence', 'retarget_public_evidence', 'substitute_public_identity'];
export const LEAFLESS_ACTIONS = ['narrative', ...PUBLIC_ACTIONS];
export type PublicPolicy = { identity: string; issuer: string; trustRoot: File | null; require: boolean };
/** The verifier's expectations for public Sigstore evidence; never read from the bundle. */
export async function publicPolicyBody(policy: PublicPolicy): Promise<Record<string, unknown> | undefined> {
  if (!policy.identity && !policy.trustRoot && !policy.require) return undefined;
  return { require_public_transparency: policy.require, sigstore_identity: policy.identity || null, sigstore_issuer: policy.issuer || null,
    ...(policy.trustRoot ? { sigstore_trusted_root: await policy.trustRoot.text() } : {}) };
}

/** Layout changes positions only. All relationships are supplied by the Python projection. */
export function graphElements(graph: Graph, mode: GraphMode, overlay: string, showOutputs = true): { nodes: Node[]; edges: Edge[] } {
  let nodes: Node[] = graph.nodes.map(n => ({ id: n.id, position: { x: 0, y: 0 },
    data: { label: `${n.label}\n${n.status ?? n.kind}${mode === 'Signatures' && n.signature ? ` · signature ${n.signature}` : ''}` },
    className: `evidence-node evidence-${n.kind}${n.signature === 'failed' || n.embedding === 'failed' ? ' evidence-failed' : n.status && n.status !== 'succeeded' ? ' evidence-execution-error' : ''}`,
    style: { width: 230, minHeight: 80 },
  }));
  let edges: Edge[] = graph.edges.map(e => ({ ...e, type: 'smoothstep', markerEnd: { type: MarkerType.ArrowClosed },
    style: { stroke: e.kind === 'control' ? '#946530' : '#527b7b', strokeDasharray: ['control', 'reference'].includes(e.kind) ? '6 5' : undefined },
  }));
  if (!showOutputs) {
    const outputs = new Set(graph.nodes.filter(n => n.kind === 'output').map(n => n.id));
    const producers = new Map(graph.edges.filter(e => ['generation', 'reference'].includes(e.kind)).map(e => [e.target, e.source]));
    nodes = nodes.filter(n => !outputs.has(n.id));
    edges = edges.filter(e => !outputs.has(e.target)).map(e => ({ ...e, source: producers.get(e.source) ?? e.source }));
  }
  if (mode === 'Signatures' || overlay !== 'none') {
    nodes = [...nodes, ...graph.records.map(r => ({ id: `record:${r.index}`, position: { x: 0, y: 0 },
      data: { label: `Leaf ${r.index} · ${r.type.replaceAll('_', ' ')}\nMembership ${r.membership}` },
      className: `evidence-node evidence-record${r.membership === 'failed' ? ' evidence-failed' : ''}`, style: { width: 230, minHeight: 80 },
    }))];
    edges = [...edges, ...graph.record_edges.filter(e => e.kind === overlay).map(e => ({ ...e, type: 'smoothstep', style: { strokeDasharray: '3 5', stroke: '#72788e' } }))];
    if (mode === 'Signatures') {
      nodes.push({ id: 'session-head', position: { x: 0, y: 0 }, data: { label: `Signed session commitment\n${graph.commitment} · ${graph.tree_size} leaves` },
        className: 'evidence-node evidence-head', style: { width: 260, minHeight: 80 } });
      for (const r of graph.records) edges.push({ id: `membership:${r.index}`, source: `record:${r.index}`, target: 'session-head', label: r.membership, type: 'smoothstep' });
      for (const n of graph.nodes) for (const leaf of n.leaves) edges.push({ id: `placement:${n.id}:${leaf}`, source: n.id, target: `record:${leaf}`, label: 'recorded in', type: 'smoothstep' });
    }
  }
  return { nodes, edges };
}

export async function evidenceRequest(path: string, init: RequestInit): Promise<Evidence> {
  const response = await fetch(`/api/evidence${path}`, init);
  if (!response.ok) throw new Error(response.status === 413 ? 'Evidence exceeds the upload limit.' : `Evidence request failed (${response.status}). Check the backend and the selected files.`);
  return evidenceSchema.parse(await response.json());
}

export function archiveBase64(bytes: Uint8Array): string {
  let binary = '';
  for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(binary);
}
