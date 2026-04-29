// Frontend bundling — esbuild runs at deploy time on the dev machine
// (or in a Docker stage), writes minified bundles to dist/. Containers
// bind-mount frontend/ as :ro, so dist/*.js are served directly by
// nginx.
//
// Each source script targets `window` for its public API (Auth, toast,
// EX, ...), so the bundler can splice them together with no module
// system — old-school IIFEs glued in declaration order.
//
// Usage:
//   cd frontend && npm install && npm run build
//
// Outputs:
//   dist/core.js   — auth + toast + confirm + theme + exchanges +
//                    formatters. Loaded synchronously on every page.
//   dist/aux.js    — banner + popup + expiry-banner + navbar. These
//                    are non-critical, deferred. Bundled together so
//                    one HTTP request covers all four.

import esbuild from "esbuild";
import { readFileSync, writeFileSync, mkdirSync, statSync } from "node:fs";
import { join } from "node:path";

const ROOT = new URL(".", import.meta.url).pathname;
const SRC = (f) => join(ROOT, f);
const DIST = (f) => join(ROOT, "dist", f);

mkdirSync(join(ROOT, "dist"), { recursive: true });

// Files that share globals via `window.X = ...`. Order matters because
// confirm.js etc. assume Auth/toast are already on `window`. esbuild's
// `entryPoints` doesn't enforce order, so we concatenate manually then
// minify the result in one pass.
const CORE = ["auth.js", "toast.js", "confirm.js", "theme.js", "exchanges.js", "formatters.js"];
const AUX  = ["banner.js", "popup.js", "expiry-banner.js", "navbar.js"];

function concat(files) {
  return files.map((f) => {
    const path = SRC(f);
    const stat = statSync(path);
    return `/* ${f} ${stat.size}b */\n${readFileSync(path, "utf8")}\n`;
  }).join("\n");
}

async function bundle(name, files) {
  const source = concat(files);
  const result = await esbuild.transform(source, {
    minify: true,
    target: "es2020",
    legalComments: "none",
  });
  const out = DIST(name);
  writeFileSync(out, result.code, "utf8");
  const before = files.reduce((s, f) => s + statSync(SRC(f)).size, 0);
  const after = result.code.length;
  console.log(`  ${name}: ${(before / 1024).toFixed(1)} KB → ${(after / 1024).toFixed(1)} KB (-${Math.round((1 - after / before) * 100)}%)`);
}

console.log("Building bundles...");
await bundle("core.js", CORE);
await bundle("aux.js", AUX);
console.log("Done.");
