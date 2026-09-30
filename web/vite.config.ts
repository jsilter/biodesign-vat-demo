import { defineConfig } from 'vitest/config';

/** Loopback ports; scripts/dev.py sets both for every child, so the proxy follows the API. */
function port(name: string, fallback: number): number {
  const raw = process.env[name]?.trim();
  if (!raw) return fallback;
  if (!/^\d+$/.test(raw) || Number(raw) < 1 || Number(raw) > 65535) throw new Error(`${name} must be a port number from 1 to 65535, not ${JSON.stringify(raw)}`);
  return Number(raw);
}

export default defineConfig({
  base: './',
  server: { port: port('VAT_UI_PORT', 5173), strictPort: true, proxy: { '/api': `http://127.0.0.1:${port('VAT_API_PORT', 8000)}` } },
  preview: { port: port('VAT_UI_PORT', 5173), strictPort: true },
  test: { environment: 'jsdom', setupFiles: ['./src/test-setup.ts'] },
});
