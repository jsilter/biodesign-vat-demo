import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import StaticEvidence, { embeddedGraph } from './StaticEvidence';
import './styles.css';

const root = createRoot(document.getElementById('root')!);
try {
  root.render(<StrictMode><StaticEvidence graph={embeddedGraph()} title={document.title} /></StrictMode>);
} catch (err) {
  root.render(<p className="notice" role="alert">{err instanceof Error ? err.message : 'Embedded evidence could not be read.'}</p>);
}
