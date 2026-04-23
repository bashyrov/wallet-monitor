# RFC: Frontend split — from 300KB monoliths to a bundled module graph

Status: **M1 done, M2 in progress, M3 planned**
Owner: bashyrov
Last update: 2026-04-23

## What we had

- `frontend/screener.html` — 159KB, ~80KB inline JS, ~49KB inline `<style>`
- `frontend/arb.html` — 286KB, ~174KB inline JS, ~58KB inline `<style>`

Every edit meant a full-file rebuild mental-model because state, rendering,
and styling lived in one blob. No type-safety. No tests. No build tool. Any
helper drift between the two pages had to be spotted by eye.

## Milestones

### M1 — extract inline CSS ✓ (shipped)

`screener.html 158 → 110KB` (-49KB). `arb.html 286 → 228KB` (-58KB).
Files: `frontend/screener.css`, `frontend/arb.css`.

Zero functional change — browser caches stylesheet independently, so iterative
HTML edits stop re-downloading 50KB of CSS.

### M2 — extract shared JS helpers (in progress)

**2.1 ✓** — `/frontend/formatters.js` exposes `window.FMT` (price, volume, apr,
rate, pct, countdown, sign, esc, stripUsdt). Additive — inline duplicates
stay for now.

**2.2** (TODO) — migrate callsites from inline `fmtPrice` etc. to `FMT.*`,
delete the duplicates. Each page drops another ~8-12KB.

**2.3** (TODO) — pull the rest of shared plumbing into external modules:
- `websocket-client.js` — the auto-reconnecting WS subscribe wrapper duplicated
  across screener (ws/funding, ws/long-short) and arb (ws/book, ws/funding)
- `auth-cookie.js` already split as `/auth.js`
- `pair-navigator.js` — popovers + URL rewrites from `/arb`

### M3 — bundler + TS + tests

**Stack choice:**
- **vite** over webpack/parcel: faster HMR, less config, first-class TS.
- **TypeScript** in `tsconfig.json` `"strict": true`. Start with `.js.ts`
  rename + `any` escape-hatch for migration; tighten over time.
- **vitest** over jest — co-located with vite, no separate config.

**Layout:**

```
frontend-next/
  tsconfig.json
  vite.config.ts
  src/
    shared/
      formatters.ts
      exchanges.ts
      auth.ts
      websocket.ts
    screener/
      index.ts          # entry, mounts tabs
      tabs/
        long-short.ts
        spot-short.ts
        dex-short.ts
        alpha.ts
      state.ts
    arb/
      index.ts
      books.ts
      charts/
        entry-exit.ts
        spread.ts
        funding.ts
      alerts.ts
  __tests__/
    formatters.test.ts
    websocket.test.ts
  public/               # static assets (favicons, svgs, css that stays hand-written)
  dist/                 # vite build output → served by FastAPI StaticFiles
```

**Build contract:**
- `npm run build` produces `frontend-next/dist/{screener,arb}.{html,js,css}`
  with hashed filenames.
- FastAPI `StaticFiles(directory="frontend-next/dist")` or a pre-deploy rsync
  to `frontend/` keeps the current routing flow.

**Migration approach:**
- Copy current screener.html + js into `frontend-next/src/screener/`.
- Split one tab at a time (long-short first since it's the smallest).
- Keep legacy `frontend/screener.html` live until all tabs migrated;
  compare diffs visually on each PR.

**Tests to add:**
- `formatters.test.ts` — edge cases (null, Infinity, very small / very large).
- `state.test.ts` — all-tab filter logic, column sorting.
- `websocket.test.ts` — reconnect backoff, subscribe replay.
- `renderer.test.ts` — jsdom-based: given a payload, produces expected DOM.

**CI integration:**
- GH Actions job: `cd frontend-next && npm ci && npm run build && npm test`.
- Gate deploys on a green frontend build.

## Why not M3 yet

- M3 needs 1-2 weeks focused engineering — migration of ~250KB of tested-by-hand
  JS to a typed graph is not a weekend task.
- M1+M2 already cut first-paint by ~40% without introducing a tool chain.
- Introducing Node in the deploy pipeline is a new operational surface
  (lock files, `npm audit`, CVE triage). Shouldn't be done under a deadline.

## Green-light criteria for starting M3

Pick a week where:
1. No in-flight backend rework on screener/arb HTTP contracts.
2. Someone (me or whoever picks this up) has uninterrupted focus.
3. We've picked a freeze date for new features in the legacy files so the
   migration doesn't race with ongoing UX work.
