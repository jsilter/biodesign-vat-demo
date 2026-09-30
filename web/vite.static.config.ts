import { defineConfig, type Plugin } from 'vite';

/** Fold the emitted script and stylesheet into the page so one file carries the whole viewer.
 *  Claude Science shows an .html artifact in a sandboxed iframe that can load nothing else. */
function singleFile(): Plugin {
  return { name: 'vat-single-file', enforce: 'post', generateBundle(_, bundle) {
    const page = bundle['static.html'];
    if (page?.type !== 'asset') throw new Error('static.html was not emitted');
    let html = String(page.source);
    for (const [name, chunk] of Object.entries(bundle)) {
      if (name === 'static.html') continue;
      const text = chunk.type === 'chunk' ? chunk.code : String(chunk.source);
      const tag = new RegExp(`<(?:script|link)[^>]*(?:src|href)="\\./${name.replaceAll(/[.*+?^${}()|[\]\\]/g, '\\$&')}"[^>]*>(?:</script>)?`);
      if (!tag.test(html)) throw new Error(`${name} is not referenced by static.html`);
      html = html.replace(tag, () => name.endsWith('.css') ? `<style>${text}</style>` : `<script type="module">${text.replaceAll('</script', '<\\/script')}</script>`);
      delete bundle[name];
    }
    if (/<(?:script|link)[^>]*(?:src|href)="\.\//.test(html)) throw new Error('static.html still references a separate file');
    page.source = html;
  } };
}

export default defineConfig({
  base: './',
  plugins: [singleFile()],
  publicDir: false,
  build: { outDir: 'dist-static', emptyOutDir: true, assetsInlineLimit: Infinity, cssCodeSplit: false, modulePreload: false,
    rollupOptions: { input: 'static.html', output: { entryFileNames: 'evidence.js', assetFileNames: 'evidence[extname]' } } },
});
