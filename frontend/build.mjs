// esbuild bundler — produces minified versions of the shared frontend
// modules under frontend/dist/. Optionally produces a single concatenated
// core.min.js for pages that want one HTTP request instead of N.
//
// Usage:
//   node frontend/build.mjs            — one-shot build
//   node frontend/build.mjs --watch    — rebuild on file change (dev)
//
// HTML pages opt in by swapping `<script src="/auth.js">` etc. for
// `<script src="/dist/core.min.js">`. Source files remain untouched and
// can keep loading individually during a gradual migration.

import { build, context } from 'esbuild';
import { mkdirSync, writeFileSync, readFileSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = __dirname;
const OUT  = join(ROOT, 'dist');

// Order matters: auth.js attaches `Auth` to the global scope; navbar.js
// uses Auth at registration time. Keep this list dependency-ordered.
const SHARED = [
  'auth.js',
  'theme.js',
  'toast.js',
  'formatters.js',
  'exchanges.js',
  'confirm.js',
  'popup.js',
  'expiry-banner.js',
  'banner.js',
  'anon-gate.js',
  'navbar.js',
  'footer.js',
];

mkdirSync(OUT, { recursive: true });

const watching = process.argv.includes('--watch');

// Per-file minification — each source produces dist/<name>.min.js. Lets a
// page that doesn't need the full bundle pick just what it uses.
const perFile = SHARED
  .filter(f => existsSync(join(ROOT, f)))
  .map(f => ({
    entryPoints: [join(ROOT, f)],
    outfile: join(OUT, f.replace(/\.js$/, '.min.js')),
    bundle: false,
    minify: true,
    target: ['es2020'],
    sourcemap: false,
    legalComments: 'none',
  }));

// Single bundle — concatenate the minified outputs into dist/core.min.js
// for one-request loading. We build this manually since the source files
// use IIFEs / globals (not ES modules), so esbuild's bundle: true would
// require imports we don't have.
async function buildCore() {
  const chunks = [];
  for (const f of SHARED) {
    const min = join(OUT, f.replace(/\.js$/, '.min.js'));
    if (existsSync(min)) {
      chunks.push(`/* ${f} */`);
      chunks.push(readFileSync(min, 'utf8').trim());
    }
  }
  const banner = `/* Avalant core bundle — built ${new Date().toISOString()} */`;
  writeFileSync(join(OUT, 'core.min.js'), [banner, ...chunks].join('\n') + '\n');
  const size = Buffer.byteLength(readFileSync(join(OUT, 'core.min.js')), 'utf8');
  console.log(`  core.min.js  ${(size / 1024).toFixed(1)} KB`);
}

if (watching) {
  const ctxs = await Promise.all(perFile.map(opts => context(opts)));
  await Promise.all(ctxs.map(c => c.watch()));
  console.log(`watching ${perFile.length} files for changes — Ctrl+C to stop`);
  // Rebuild core bundle on watch tick. Simple approach: rebuild every 500ms
  // if a per-file output changed. esbuild's watcher doesn't expose hooks
  // cleanly across multiple contexts, so we do a poor-man's rebuild loop.
  setInterval(buildCore, 1000);
} else {
  console.log(`building ${perFile.length} modules → ${OUT}`);
  await Promise.all(perFile.map(opts => build(opts)));
  for (const opts of perFile) {
    const size = Buffer.byteLength(readFileSync(opts.outfile), 'utf8');
    const name = opts.outfile.split('/').pop();
    console.log(`  ${name.padEnd(24)} ${(size / 1024).toFixed(1)} KB`);
  }
  await buildCore();
  console.log('done.');
}
