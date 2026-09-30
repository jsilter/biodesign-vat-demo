import { graphSchema, type Graph } from './graph-data';

export function exampleGraph(): Graph {
  return graphSchema.parse({
    session: 'session', root: 'a'.repeat(64), tree_size: 4, commitment: 'passed', trust: 'Operator-supplied public keys',
    bundle_report: { outcome: 'OK', checks: [{ number: 1, name: 'hash chain', passed: true, detail: 'links intact' }] },
    provenance_report: { outcome: 'VALID', checks: [], unspecified_inputs: 1 },
    nodes: [
      { id: 'first', kind: 'execution', label: 'Unfamiliar producer', status: 'succeeded', signature: 'passed', leaves: [1], memberships: [{ leaf: 1, status: 'passed' }] },
      { id: 'output:first', kind: 'output', label: '/result', leaves: [], memberships: [] },
      { id: 'second', kind: 'execution', label: 'Another name', status: 'succeeded', signature: 'passed', leaves: [2], memberships: [{ leaf: 2, status: 'passed' }] },
    ],
    edges: [
      { id: 'generation', source: 'first', target: 'output:first', kind: 'generation', label: 'produced' },
      { id: 'data', source: 'output:first', target: 'second', kind: 'data', label: '/args/0' },
    ],
    record_edges: [{ id: 'chain', source: 'record:1', target: 'record:2', kind: 'chain', label: 'hash link matches' }],
    records: [0, 1, 2, 3].map(index => ({ index, type: index ? 'function_call' : 'session_open', membership: 'passed', params: {}, artifacts: [] })),
    gaps: [], missing_count: 0, head: { keyid: 'recorder' }, attestation: {},
  });
}
