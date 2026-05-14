/* Avalant Service Worker
 *
 * Strategy:
 *   /api/*        — network only, never cache (real-time data + auth)
 *   /ws/*         — never cache (WebSockets bypass SW anyway, but we
 *                   short-circuit just in case)
 *   /vendor/*     — cache-first, immutable URLs (version in filename)
 *   /dist/*       — cache-first, immutable URLs (version in filename)
 *   /fonts/*      — cache-first, woff2 are hash-versioned by Google
 *   *.js / *.css  — stale-while-revalidate; serve cached, update in
 *                   background; HTML cache-bust headers cap staleness
 *   HTML pages    — network-first with cache fallback; HEAD-of-cache
 *                   stays available offline / on slow networks
 *
 * Versioning: bump VERSION on every meaningful asset rev. Old caches
 * are purged on `activate`. clients.claim() makes the new SW control
 * existing tabs without a reload.
 */
const VERSION = 'v20260515-01';
const STATIC_CACHE = `avalant-static-${VERSION}`;
const HTML_CACHE = `avalant-html-${VERSION}`;
const RUNTIME_CACHE = `avalant-runtime-${VERSION}`;

// Assets to preload on install — minimal critical set so first repeat
// visit is fully offline-capable. Not everything (would bloat install
// time); the runtime cache fills in the rest as the user navigates.
const PRELOAD = [
  '/dist/core.min.js?v=20260514a',
  '/design.css',
  '/navbar.css',
  '/fonts/fonts.css',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(STATIC_CACHE)
      .then((c) => c.addAll(PRELOAD).catch(() => null))  // best-effort
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys
          .filter((k) => k.startsWith('avalant-') && !k.endsWith(VERSION))
          .map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

function isApiOrWs(url) {
  return url.pathname.startsWith('/api/') ||
         url.pathname.startsWith('/ws/') ||
         url.pathname.startsWith('/internal/');
}

function isVersionedAsset(url) {
  // /vendor/* and /dist/* have version/hash in filename — never change
  // for a given URL, so cache-first is safe.
  if (url.pathname.startsWith('/vendor/') || url.pathname.startsWith('/dist/')) {
    return true;
  }
  // Anything with ?v= cache-buster query is also versioned at the URL
  // level — the deploy script bumps the version when content changes.
  if (url.searchParams.has('v')) return true;
  return false;
}

function isFont(url) {
  return url.pathname.startsWith('/fonts/') ||
         url.pathname.endsWith('.woff2') ||
         url.pathname.endsWith('.woff') ||
         url.pathname.endsWith('.ttf');
}

function isCacheableStatic(url) {
  return url.pathname.match(/\.(js|css|svg|png|jpg|jpeg|webp|ico|gif)$/);
}

self.addEventListener('fetch', (e) => {
  // Only intercept GETs from the same origin.
  if (e.request.method !== 'GET') return;
  let url;
  try { url = new URL(e.request.url); } catch (_) { return; }
  if (url.origin !== self.location.origin) return;

  // Real-time data / auth — pure passthrough.
  if (isApiOrWs(url)) return;

  // HTML navigation: network-first, fallback to cache for offline/slow.
  if (e.request.mode === 'navigate' || e.request.destination === 'document') {
    e.respondWith(networkFirst(e.request));
    return;
  }

  // Versioned static assets (vendor, dist, ?v=… queried): cache-first.
  // These URLs change on deploy; old URLs become orphan and get GC'd
  // when the cache size limit kicks in (browsers do this automatically).
  if (isVersionedAsset(url) || isFont(url)) {
    e.respondWith(cacheFirst(e.request, STATIC_CACHE));
    return;
  }

  // Regular static assets (.js, .css, .png, .svg without versioning):
  // stale-while-revalidate — serve cached for instant render, update
  // in background so next load sees fresh.
  if (isCacheableStatic(url)) {
    e.respondWith(staleWhileRevalidate(e.request));
    return;
  }

  // Anything else: pass through.
});

async function networkFirst(request) {
  try {
    const resp = await fetch(request);
    // Only cache successful HTML responses. 4xx/5xx stays in network.
    if (resp.ok) {
      const cache = await caches.open(HTML_CACHE);
      cache.put(request, resp.clone()).catch(() => {});
    }
    return resp;
  } catch (err) {
    const cached = await caches.match(request);
    if (cached) return cached;
    throw err;
  }
}

async function cacheFirst(request, cacheName) {
  const cached = await caches.match(request);
  if (cached) return cached;
  try {
    const resp = await fetch(request);
    if (resp.ok) {
      const cache = await caches.open(cacheName);
      cache.put(request, resp.clone()).catch(() => {});
    }
    return resp;
  } catch (err) {
    throw err;
  }
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(RUNTIME_CACHE);
  const cached = await cache.match(request);
  const fetchPromise = fetch(request)
    .then((resp) => {
      if (resp.ok) cache.put(request, resp.clone()).catch(() => {});
      return resp;
    })
    .catch(() => null);
  // Return cached immediately if available; otherwise wait for network.
  return cached || fetchPromise || new Response('', { status: 504 });
}
