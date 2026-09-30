import { expect, it } from 'vitest';
import { archiveBase64, graphElements } from './graph-data';
import { exampleGraph } from './graph-test-data';


it('renders only supplied relationships, independent of names and presentation order', () => {
  const graph = exampleGraph();
  const before = graphElements(graph, 'Workflow', 'none');
  graph.nodes.reverse(); graph.nodes.forEach(n => { n.label = 'renamed'; });
  expect(graphElements(graph, 'Workflow', 'none').edges).toEqual(before.edges);
  expect(before.edges.map(e => [e.source, e.target])).toEqual([['first', 'output:first'], ['output:first', 'second']]);
  graph.edges = [];
  expect(graphElements(graph, 'Workflow', 'none').edges).toEqual([]);
});

it('keeps chronology separate and membership states supplied by Python', () => {
  const graph = exampleGraph();
  graph.records[2].membership = 'failed';
  expect(graphElements(graph, 'Workflow', 'none').edges).toHaveLength(2);
  const overlay = graphElements(graph, 'Signatures', 'chain');
  expect(overlay.edges.find(e => e.id === 'membership:2')?.label).toBe('failed');
  expect(overlay.edges.some(e => e.id === 'chain')).toBe(true);
  expect(graphElements(graph, 'Workflow', 'order').edges.some(e => e.id === 'chain')).toBe(false);
});

it('does not create a relationship between unknown gaps and missing producers', () => {
  const graph = exampleGraph(); graph.gaps = [3]; graph.missing_count = 1;
  graph.nodes.push({ id: 'undisclosed', kind: 'missing', label: 'Undisclosed producer', leaves: [], memberships: [] });
  const view = graphElements(graph, 'Workflow', 'none');
  expect(view.nodes.find(n => n.id === 'undisclosed')?.data.label).toContain('Undisclosed producer');
  expect(view.edges.some(e => e.source === 'undisclosed' || e.target === 'undisclosed')).toBe(false);
});

it('encodes exact archive bytes including non-ASCII octets and chunk boundaries', () => {
  const bytes = Uint8Array.from({ length: 17000 }, (_, i) => i % 256);
  expect(Uint8Array.from(atob(archiveBase64(bytes)), c => c.charCodeAt(0))).toEqual(bytes);
});

it('groups output occurrences only through explicit generation edges', () => {
  const graph = exampleGraph();
  const grouped = graphElements(graph, 'Workflow', 'none', false);
  expect(grouped.nodes.map(n => n.id)).toEqual(['first', 'second']);
  expect(grouped.edges.map(e => [e.source, e.target, e.label])).toEqual([['first', 'second', '/args/0']]);
  expect(graphElements(graph, 'Workflow', 'none', true).nodes).toHaveLength(3);
  expect(graph.nodes).toHaveLength(3);
});
