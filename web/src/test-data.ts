import { readFileSync } from 'node:fs';
import { vi } from 'vitest';
import type { Catalog } from './data';

/** Real Python-generated evidence; fresh parse prevents mutations leaking between tests. */
export function fixtureCatalog(): Catalog {
  return JSON.parse(readFileSync('public/demo/catalog.json', 'utf8')) as Catalog;
}

export function serveCatalog(data: unknown = fixtureCatalog()) {
  return vi.stubGlobal('fetch', vi.fn(async (path: string) => new Response(JSON.stringify(
    path === '/api/models' ? { models: [{ id: 'openai/test', name: 'OpenAI test' }], configured: false } : data,
  ))));
}
