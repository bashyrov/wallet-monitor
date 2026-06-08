/* Extracted from frontend/screener.html — loaded once via
   <script src="/screener.js" defer>; cached separately from the HTML
   shell. 2026-05-14. */

// Screener is public; user-specific features (watchlist, alerts) auto-disable
// when Auth.isLoggedIn() is false.
const IS_AUTHED = Auth.isLoggedIn();

// ── Empty/loading/error state renderer ─────────────────────────────────
// One helper for every screener tab. Always returns the FULL <tr> so we
// can drop it into a tbody as innerHTML. State `kind` drives icon + colour:
//   loading — spinning ring (during first paint / refresh)
//   empty   — dashed ring ("no data yet, will appear when computed")
//   error   — red X + reason + optional retry button
//   filtered — empty variant with "no matches" title
// `colspan` matches the table header (8-11 depending on the tab).
function _emptyRow(opts){
  const { kind='loading', title='Loading…', sub='', colspan=9, retryFn=null } = opts || {};
  const cls = 'empty-msg is-' + kind;
  const retryBtn = retryFn
    ? `<button class="empty-retry" onclick="${retryFn}">Retry</button>`
    : '';
  return `<tr><td colspan="${colspan}" class="${cls}"><div class="empty-inner">`
       + `<div class="empty-spinner"></div>`
       + `<div class="empty-title">${title}</div>`
       + (sub ? `<div class="empty-sub">${sub}</div>` : '')
       + retryBtn
       + `</div></td></tr>`;
}

// ── WS idle-disconnect ──────────────────────────────────────────────
// После 5 минут неактивности закрываем WS — экономим клиентский CPU
// (нет парсинга incoming frames) и server-side load (нет broadcast'а
// клиенту который не смотрит). Re-open при любом mouse/scroll/key
// событии или возврате на вкладку (visibilitychange).
const _Idle = (() => {
  const IDLE_MS = 5 * 60 * 1000;
  let _lastActivity = Date.now();
  let _closed = false;
  const _wakers = [];

  function track() { _lastActivity = Date.now(); if (_closed) wakeUp(); }
  function isIdle() { return Date.now() - _lastActivity > IDLE_MS; }
  function shouldStayClosed() { return _closed; }
  function onWake(fn) { _wakers.push(fn); }
  function closeAll() {
    _closed = true;
    for (const w of _wakers) { try { w.close && w.close(); } catch (_) {} }
  }
  function wakeUp() {
    if (!_closed) return;
    _closed = false;
    for (const w of _wakers) { try { w.open && w.open(); } catch (_) {} }
  }

  ['mousemove', 'mousedown', 'keydown', 'scroll', 'touchstart', 'wheel']
    .forEach(ev => document.addEventListener(ev, track, { passive: true }));
  document.addEventListener('visibilitychange', () => { if (!document.hidden) track(); });

  setInterval(() => {
    if (document.hidden) return;
    if (isIdle() && !_closed) {
      try { console.debug('[idle] closing WS — no activity for 5 min'); } catch (_) {}
      closeAll();
    }
  }, 60_000);

  return { track, isIdle, shouldStayClosed, onWake, closeAll, wakeUp };
})();

// ── constants ──────────────────────────────────────────────────────────────────
// EXCHANGES is sourced from /api/meta/venues via exchanges.js. We keep a
// hard-coded fallback so the dropdown still renders if the meta call
// hasn't resolved yet (fresh page load race).
const _EXCHANGES_FALLBACK = ['binance','bybit','okx','gate','kucoin','mexc','bitget','hyperliquid','aster','ethereal','whitebit','bingx','htx','paradex','extended','lighter','backpack','kraken'];
let EXCHANGES = (window.EX && window.EX.lists && window.EX.lists.screener_all && window.EX.lists.screener_all.length)
  ? window.EX.lists.screener_all
  : _EXCHANGES_FALLBACK;
if (window.EX && window.EX.ready) {
  window.EX.ready.then(() => {
    if (window.EX.lists.screener_all.length) {
      EXCHANGES = window.EX.lists.screener_all;
      // Re-render any filter UI that was rendered with the fallback list.
      try { typeof rebuildExchangeFilter === 'function' && rebuildExchangeFilter(); } catch(_){}
    }
  });
}
// Kept as legacy aliases for existing call sites; values sourced from the
// shared /exchanges.js module so the palette never drifts.
const EX_LABEL = (window.EX && window.EX.labels) || { binance:'Binance', bybit:'Bybit', okx:'OKX', gate:'Gate', kucoin:'KuCoin', mexc:'MEXC', bitget:'Bitget', hyperliquid:'Hyperliquid', aster:'Aster', ethereal:'Ethereal', whitebit:'WhiteBIT', bingx:'BingX', backpack:'Backpack', lighter:'Lighter', paradex:'Paradex' };
const EX_COLOR = (window.EX && window.EX.colors) || { binance:'#F0B90B', bybit:'#F0842D', okx:'#C8C8C8', gate:'#17C684', kucoin:'#09BA86', mexc:'#17D854', bitget:'#00D2C8', hyperliquid:'#64B4FF', aster:'#8A63D2', ethereal:'#C864C8', whitebit:'#2DCCCD', bingx:'#1DB8F2', backpack:'#4ADE80', lighter:'#A78BFA', paradex:'#FF6A6A' };

function exTradeUrl(exchange, symbol) {
  const s = (symbol || '').toUpperCase();
  const sl = s.toLowerCase();
  const urls = {
    binance:      `https://www.binance.com/en/futures/${s}USDT`,
    bybit:        `https://www.bybit.com/trade/usdt/${s}USDT`,
    okx:          `https://www.okx.com/trade-swap/${sl}-usdt-swap`,
    gate:         `https://www.gate.io/futures/usdt/${s}_USDT`,
    kucoin:       `https://www.kucoin.com/futures/trade/${s}USDTM`,
    mexc:         `https://futures.mexc.com/exchange/${s}_USDT`,
    bitget:       `https://www.bitget.com/futures/usdt/${s}USDT`,
    hyperliquid:  `https://app.hyperliquid.xyz/trade/${s}`,
    aster:        `https://www.asterdex.com/en/trade/pro/futures/${s}USDT`,
    ethereal:     `https://app.ethereal.trade/trade/${s}-PERP`,
    whitebit:     `https://whitebit.com/futures/${s}_USDT`,
    bingx:        `https://bingx.com/en/perpetual/${s}-USDT/`,
    kraken:       `https://futures.kraken.com/trade/PI_${s}USD`,
  };
  return urls[exchange] || null;
}

function exBadge(exchange, symbol) {
  const url = exTradeUrl(exchange, symbol);
  const label = EX_LABEL[exchange] || exchange;
  // Dot + plain-text name — matches sidebar / Alpha strip / infobar.
  // No more tinted-pill-with-coloured-text; the dot carries the brand colour.
  const inner = `<span class="ex-dot" data-ex="${exchange}"></span><span class="ex-name">${label}</span>`;
  if (url) {
    return `<a href="${url}" target="_blank" rel="noopener" class="ex-badge" title="Open ${label}" onclick="event.stopPropagation()">${inner}</a>`;
  }
  return `<span class="ex-badge">${inner}</span>`;
}

function symbolLink(symbol, exchange) {
  const url = exTradeUrl(exchange, symbol);
  if (url) return `<a href="${url}" target="_blank" rel="noopener" class="sym-link" title="Trade on ${EX_LABEL[exchange]||exchange}" onclick="event.stopPropagation()">${symbol}</a>`;
  return symbol;
}

// ── state ──────────────────────────────────────────────────────────────────────
// URL param 'mode' accepts the canonical names (all / long-short / spot-short /
// dex-short / funding / alpha) AND the legacy shortcuts (arb / spot / dex) so
// existing bookmarks still work. Internal mode IDs stay short (arb/spot/dex).
const _MODE_ALIAS = {
  'long-short': 'arb', 'longshort': 'arb', 'arbitrage': 'arb',
  'spot-short': 'spot', 'spotshort': 'spot',
  'dex-short': 'dex',  'dexshort':  'dex',
};
let _mode = (() => {
  const VALID = new Set(['all','arb','spot','dex','funding','funding-arb','alpha']);
  const u = (new URLSearchParams(location.search).get('mode') || '').toLowerCase();
  if (u) {
    if (VALID.has(u)) return u;
    if (_MODE_ALIAS[u]) return _MODE_ALIAS[u];
  }
  const ls = localStorage.getItem('screener_mode');
  if (ls && VALID.has(ls)) return ls;
  return 'arb';
})();
// Canonicalise URL param name for sharing (?mode=long-short not ?mode=arb)
const _MODE_CANON = {arb:'long-short', spot:'spot-short', dex:'dex-short', 'dex-spot':'dex-spot', 'funding-arb':'funding-arb'};
let _rows = [];           // funding rows
let _arbRows = [];                    // flat array for table rendering
// Tracks whether the first in_pct landing has triggered a one-time sort.
// Reset to false on data reload (loadArb/loadSpot/loadDex) so a fresh
// dataset gets sorted by live values once they arrive. Declared up-front
// because the loadX() entry points reference it before _writeInOutOntoRows
// is parsed (script-init timing).
const _inOutFirstSorted = { arb: false, spot: false, dex: false };
const _arbRowsByKey = new Map();      // key → opp, source of truth for diffs
let _arbMeta = { fees: {}, exchanges: [] };
const _arbKey = (o) => `${o.symbol}|${o.long_exchange}|${o.short_exchange}`;

// Sticky-remove window for row drops. The /ws/arb feed says "removed"
// for any key not in the latest top-1000 — that includes pairs that
// briefly lost a mark price (one venue's funding tick missing) and
// will be back next cycle. Without stickiness the row vanishes for
// 1-3s then reappears, which the user sees as "metric тупит / падает".
// We delay actual deletion by ARB_REMOVAL_GRACE so a flapping pair
// stays on screen with its last good values.
const ARB_REMOVAL_GRACE_MS = 12000;
const _arbRemovalTimers = new Map(); // key → setTimeout handle

function _applyArbPayload(data) {
  // Handles both snapshot messages and incremental diffs from /ws/arb.
  // Server sends {type: 'snapshot', opportunities: [...]} on connect,
  // then {type: 'diff', added?:[], updated?:[], removed?:[]} every tick.
  // Legacy untyped payloads (full snapshot) are treated as snapshot too.
  if (!data) return;
  const cancelPendingRemoval = (k) => {
    const t = _arbRemovalTimers.get(k);
    if (t) { clearTimeout(t); _arbRemovalTimers.delete(k); }
  };
  if (data.type === 'diff') {
    if (Array.isArray(data.added)) {
      for (const o of data.added) {
        const key = _arbKey(o);
        cancelPendingRemoval(key);
        _arbRowsByKey.set(key, o);
      }
    }
    if (Array.isArray(data.updated)) {
      for (const o of data.updated) {
        const key = _arbKey(o);
        cancelPendingRemoval(key);
        _arbRowsByKey.set(key, o);
      }
    }
    if (Array.isArray(data.removed)) {
      for (const k of data.removed) {
        const key = Array.isArray(k) ? k.join('|') : k;
        // Don't delete immediately. Schedule a delayed removal; if the
        // pair re-appears (added/updated) before then, the schedule is
        // cancelled by cancelPendingRemoval above.
        if (_arbRemovalTimers.has(key)) continue;
        const handle = setTimeout(() => {
          _arbRemovalTimers.delete(key);
          _arbRowsByKey.delete(key);
          _arbRows = Array.from(_arbRowsByKey.values());
          // Re-render the relevant table after the deferred drop.
          if (typeof applyArb === 'function') applyArb();
        }, ARB_REMOVAL_GRACE_MS);
        _arbRemovalTimers.set(key, handle);
      }
    }
    if (data.fees)      _arbMeta.fees = data.fees;
    if (data.exchanges) _arbMeta.exchanges = data.exchanges;
  } else {
    // Snapshot: trust the server's authoritative set, but still treat
    // any drop relative to current state as soft-removal so a stale-
    // snapshot reconnect doesn't wipe the table.
    const incoming = new Set();
    for (const o of (data.opportunities || [])) {
      const key = _arbKey(o);
      incoming.add(key);
      cancelPendingRemoval(key);
      _arbRowsByKey.set(key, o);
    }
    for (const key of [..._arbRowsByKey.keys()]) {
      if (incoming.has(key)) continue;
      if (_arbRemovalTimers.has(key)) continue;
      const handle = setTimeout(() => {
        _arbRemovalTimers.delete(key);
        _arbRowsByKey.delete(key);
        _arbRows = Array.from(_arbRowsByKey.values());
        if (typeof applyArb === 'function') applyArb();
      }, ARB_REMOVAL_GRACE_MS);
      _arbRemovalTimers.set(key, handle);
    }
    if (data.fees)      _arbMeta.fees = data.fees;
    if (data.exchanges) _arbMeta.exchanges = data.exchanges;
  }
  _arbRows = Array.from(_arbRowsByKey.values());
}
let _filtered = [];
let _filteredArb = [];
let _exDisabled = new Set();   // exchanges to hide (empty = show all)
let _hiddenTokens = new Set(JSON.parse(localStorage.getItem('screener_hidden_tokens') || '[]'));
let _sortF = { col: 'apr', asc: false };
// Default sort = In (entry-divergence). Rows without a live orderbook
// have null in_pct and sink to the bottom — so the top of the table is
// always the most-actionable rows with real bid/ask quotes.
let _sortA = { col: 'in_pct', asc: false };
let _validOnly = false;
let _crossOnly = true;   // show only tokens on 2+ exchanges by default

// ── pagination ────────────────────────────────────────────────────────────────
let PAGE_SIZE = 15;
let _pageF = 0;
let _pageA = 0;

// ── open card state (mobile) ─────────────────────────────────────────────────
let _openFundingKey = null;  // symbol+exchange
let _openArbKey    = null;   // symbol+long+short

// ── WebSocket state ────────────────────────────────────────────────────────────
// /ws/funding was retired — it pushed a ~90 KB gzip full-snapshot every 200ms
// to every screener visitor regardless of which tab they were on, costing
// ~27 MB/min of pure waste on Long/Short, Spot/Short, etc. The Funding tab
// now polls /api/screener/funding via setInterval (see _fundingPoll below).
let _wsArb = null;  // arbitrage WebSocket
let _fundingPollTimer = null;

// ── init nav ───────────────────────────────────────────────────────────────────
(function initNav() {
  const u = Auth.getUser();
  if (u) {
    const el = document.getElementById('nav-avatar');
    if (el) el.textContent = (u.username || u.email || 'U')[0].toUpperCase();
  }
})();

// ── hidden tokens ─────────────────────────────────────────────────────────────
function _saveHiddenTokens() {
  localStorage.setItem('screener_hidden_tokens', JSON.stringify([..._hiddenTokens]));
}

function renderHiddenChips() {
  const makeChips = (tokens) => [...tokens].map(t => `
    <span class="hidden-chip">${esc(t)}<span class="hidden-chip-x" onclick="removeHiddenToken('${esc(t)}')">×</span></span>
  `).join('');
  const d = document.getElementById('hidden-chips');     if (d) d.innerHTML = makeChips(_hiddenTokens);
  const m = document.getElementById('hidden-chips-mob'); if (m) m.innerHTML = makeChips(_hiddenTokens);
}

function addHiddenToken(fromMob = false) {
  const inputId = fromMob ? 'hidden-token-input-mob' : 'hidden-token-input';
  const inp = document.getElementById(inputId);
  const token = inp.value.trim().toUpperCase();
  if (!token) return;
  _hiddenTokens.add(token);
  _saveHiddenTokens();
  inp.value = '';
  // also clear the other input
  const other = document.getElementById(fromMob ? 'hidden-token-input' : 'hidden-token-input-mob');
  if (other) other.value = '';
  renderHiddenChips();
  _reapplyCurrentMode();
}

function removeHiddenToken(token) {
  _hiddenTokens.delete(token);
  _saveHiddenTokens();
  renderHiddenChips();
  _reapplyCurrentMode();
}

// ── left panel exchange list ───────────────────────────────────────────────────
function buildExDrop() {
  const items = document.getElementById('lp-ex-items');
  items.innerHTML = EXCHANGES.map(ex => `
    <div class="lp-ex-item checked" id="ex-item-${ex}" onclick="toggleEx('${ex}')">
      <div class="lp-ex-dot" style="background:${EX_COLOR[ex]}"></div>
      <span class="lp-ex-label">${EX_LABEL[ex]}</span>
      <div class="lp-ex-health" id="ex-health-${ex}" title="data freshness"></div>
      <div class="lp-ex-check"></div>
    </div>
  `).join('');
  _updateExCount();
}

// ── Exchange freshness polling ─────────────────────────────────────────────
const _exHealth = {};      // {ex: {healthy, age_s, via, klass}}
let _exHealthTimer = null;
async function refreshExchangeHealth() {
  try {
    const r = await Auth.apiFetch('/screener/exchange-health');
    if (!r || !r.ok) return;
    const body = await r.json();
    const ex = body.exchanges || {};
    for (const name of EXCHANGES) {
      const h = ex[name] || {};
      const age = h.age_s;
      const healthy = !!h.healthy;
      let klass = 'unknown';
      if (age != null) {
        if (healthy && age <= 2) klass = 'live';
        else if (healthy)        klass = 'slow';
        else if (h.via === 'none' || h.via == null) klass = 'unknown';
        else                     klass = 'stale';
      }
      _exHealth[name] = { ...h, klass };
      const dot = document.getElementById(`ex-health-${name}`);
      if (dot) {
        dot.classList.remove('live', 'slow', 'stale', 'unknown');
        dot.classList.add(klass);
        const ageStr = age == null ? 'no data' : `${age.toFixed(1)}s`;
        dot.title = `${name} — ${h.via || '?'} · ${ageStr}`;
      }
    }
    renderAlphaStatus();
  } catch (_) {}
}
function startExchangeHealthPoll() {
  if (_exHealthTimer) return;
  refreshExchangeHealth();
  _exHealthTimer = setInterval(() => { if (document.hidden) return; refreshExchangeHealth(); }, 3000);
}

function _updateExCount(){
  const el=document.getElementById('lp-ex-count'); if(!el) return;
  const active = EXCHANGES.length - _exDisabled.size;
  el.textContent = `${active}/${EXCHANGES.length}`;
  el.style.color = active === EXCHANGES.length ? 'var(--green)'
                 : active === 0 ? 'var(--red)'
                 : 'var(--yellow)';
  el.style.background = active === EXCHANGES.length ? 'rgba(26,255,171,.08)'
                      : active === 0 ? 'rgba(248,113,113,.08)'
                      : 'rgba(229,192,123,.08)';
  el.style.borderColor = active === EXCHANGES.length ? 'rgba(26,255,171,.22)'
                       : active === 0 ? 'rgba(248,113,113,.22)'
                       : 'rgba(229,192,123,.22)';
  _renderExBtnDots();
}

// Render the small exchange-color dots inside the accordion button
function _renderExBtnDots(){
  const wrap = document.getElementById('fb-ex-dots');
  if (!wrap) return;
  wrap.innerHTML = EXCHANGES
    .filter(ex => !_exDisabled.has(ex))
    .slice(0, 8)
    .map(ex => `<span class="d" style="background:${EX_COLOR[ex]}"></span>`)
    .join('');
}

// Exchange dropdown open/close (positioned under the button)
function toggleExAcc(){
  const acc = document.getElementById('fb-ex-acc');
  const btn = document.getElementById('fb-ex-btn');
  const open = acc.classList.toggle('open');
  btn.classList.toggle('open', open);
  if (open) {
    const r = btn.getBoundingClientRect();
    const bar = acc.parentElement; // .filters-bar (position:relative)
    const pr = bar.getBoundingClientRect();
    acc.style.left = (r.left - pr.left) + 'px';
    acc.style.top  = (r.bottom - pr.top + 6) + 'px';
  }
}
// close on outside click
document.addEventListener('click', (e) => {
  const acc = document.getElementById('fb-ex-acc');
  if (!acc || !acc.classList.contains('open')) return;
  if (e.target.closest('#fb-ex-btn') || e.target.closest('#fb-ex-acc')) return;
  acc.classList.remove('open');
  document.getElementById('fb-ex-btn')?.classList.remove('open');
});

// Alpha-only exchange status strip (freshness dots)
function renderAlphaStatus(){
  const wrap = document.getElementById('alpha-status-items');
  if (!wrap) return;
  wrap.innerHTML = EXCHANGES.map(ex => {
    const h = _exHealth[ex] || {};
    const klass = h.klass || 'stale';
    const age = h.age_s;
    const ageStr = age == null ? '—' : `${age.toFixed(1)}s`;
    return `<span class="as-ex" title="${ex} — ${h.via||'?'} · ${ageStr}">
      <span class="d ${klass}"></span>${EX_LABEL[ex]||ex}
    </span>`;
  }).join('');

  // Update overall indicator dot
  const klasses = EXCHANGES.map(ex => (_exHealth[ex]||{}).klass || 'unknown');
  const overall = klasses.includes('stale') ? 'stale' : klasses.includes('slow') ? 'slow' : klasses.every(k => k === 'unknown') ? 'unknown' : 'live';
  const dot = document.getElementById('status-lbl-dot');
  if (dot) dot.className = 'lbl-dot ' + overall;
  const dotMini = document.getElementById('status-lbl-dot-mini');
  if (dotMini) dotMini.className = 'lbl-dot ' + overall;
}

function _reapplyCurrentMode() {
  if (_mode === 'funding') applyFilter();
  else if (_mode === 'spot') applySpot();
  else if (_mode === 'dex')  applyDex();
  else if (_mode === 'funding-arb') applyFA();
  else if (_mode === 'all')  renderAll();
  else applyArb();
}

function toggleEx(ex) {
  const item = document.getElementById(`ex-item-${ex}`);
  if (_exDisabled.has(ex)) { _exDisabled.delete(ex); item.classList.add('checked'); }
  else { _exDisabled.add(ex); item.classList.remove('checked'); }
  _updateExCount();
  _reapplyCurrentMode();
}

function exSelectAll() {
  _exDisabled.clear();
  EXCHANGES.forEach(ex => {
    const a = document.getElementById(`ex-item-${ex}`); if (a) a.classList.add('checked');
    const b = document.getElementById(`mob-ex-${ex}`);  if (b) b.classList.add('checked');
  });
  _updateExCount();
  _reapplyCurrentMode();
}

function exSelectNone() {
  EXCHANGES.forEach(ex => {
    _exDisabled.add(ex);
    const a = document.getElementById(`ex-item-${ex}`); if (a) a.classList.remove('checked');
    const b = document.getElementById(`mob-ex-${ex}`);  if (b) b.classList.remove('checked');
  });
  _updateExCount();
  _reapplyCurrentMode();
}

function exSelectInvert() {
  EXCHANGES.forEach(ex => {
    const was = _exDisabled.has(ex);
    if (was) _exDisabled.delete(ex); else _exDisabled.add(ex);
    const a = document.getElementById(`ex-item-${ex}`); if (a) a.classList.toggle('checked', was);
    const b = document.getElementById(`mob-ex-${ex}`);  if (b) b.classList.toggle('checked', was);
  });
  _updateExCount();
  _reapplyCurrentMode();
}

// ── mobile filters toggle ─────────────────────────────────────────────────────
function toggleMobFilters() {
  const panel = document.getElementById('mob-filters');
  const btn   = document.getElementById('mob-filter-btn');
  const open  = panel.classList.toggle('open');
  btn.classList.toggle('active', open);
}

function buildMobExChips() {
  const wrap = document.getElementById('lp-ex-items-mob');
  if (!wrap) return;
  wrap.innerHTML = EXCHANGES.map(ex => `
    <div class="mob-ex-chip checked" id="mob-ex-${ex}" style="color:${EX_COLOR[ex]}" onclick="toggleExMob('${ex}')">
      ${EX_LABEL[ex]}
    </div>
  `).join('');
}

function toggleExMob(ex) {
  // mirror to main toggleEx
  const item = document.getElementById(`ex-item-${ex}`);
  const mobItem = document.getElementById(`mob-ex-${ex}`);
  if (_exDisabled.has(ex)) {
    _exDisabled.delete(ex);
    if (item) item.classList.add('checked');
    if (mobItem) mobItem.classList.add('checked');
  } else {
    _exDisabled.add(ex);
    if (item) item.classList.remove('checked');
    if (mobItem) mobItem.classList.remove('checked');
  }
  _reapplyCurrentMode();
}

// ── left panel toggle ──────────────────────────────────────────────────────────
function togglePanel() {
  const panel = document.getElementById('left-panel');
  const btn   = document.getElementById('panel-toggle');
  const collapsed = panel.classList.toggle('collapsed');
  btn.classList.toggle('collapsed', collapsed);
  btn.style.left = collapsed ? '0' : '260px';
}

// ── mode switch ────────────────────────────────────────────────────────────────
function switchMode(mode) {
  _mode = mode;
  // Persist so a page reload lands back on the same tab. Also mirror in URL
  // so the user can share or bookmark a direct link.
  try { localStorage.setItem('screener_mode', mode); } catch {}
  try {
    const url = new URL(window.location);
    // Write the canonical param name (long-short / spot-short / dex-short) so
    // copy-pasted URLs are self-describing. Internal mode stays short.
    url.searchParams.set('mode', _MODE_CANON[mode] || mode);
    history.replaceState(null, '', url);
  } catch {}
  ['all','arb','spot','dex','dex-spot','funding','funding-arb','alpha'].forEach(m => {
    const t = document.getElementById('tab-' + m);
    if (t) t.classList.toggle('active', mode === m);
    const s = document.getElementById('section-' + m);
    if (s) s.style.display = mode === m ? '' : 'none';
  });
  document.getElementById('lp-filters-funding').style.display   = mode === 'funding' ? '' : 'none';
  document.getElementById('lp-filters-arb').style.display       = (mode === 'arb' || mode === 'alpha' || mode === 'all' || mode === 'spot' || mode === 'dex' || mode === 'dex-spot' || mode === 'funding-arb') ? '' : 'none';
  const mf = document.getElementById('mob-filters-funding'); if (mf) mf.style.display = mode === 'funding' ? '' : 'none';
  const ma = document.getElementById('mob-filters-arb');     if (ma) ma.style.display = (mode === 'arb' || mode === 'alpha' || mode === 'all' || mode === 'spot' || mode === 'dex' || mode === 'dex-spot' || mode === 'funding-arb') ? '' : 'none';
  const mobSortF = document.getElementById('mobile-sort-funding'); if (mobSortF) mobSortF.style.display = mode === 'funding' ? '' : 'none';
  const mobSortA = document.getElementById('mobile-sort-arb');     if (mobSortA) mobSortA.style.display = mode === 'arb'     ? '' : 'none';
  renderAlphaStatus();
  if (mode === 'arb' && !_arbRows.length) loadArb();
  else if (mode === 'alpha') loadAlpha();
  else if (mode === 'funding') { _pollFunding(false); applyFilter(); }
  else if (mode === 'spot') loadSpot();
  else if (mode === 'dex') loadDex();
  else if (mode === 'dex-spot') loadDexSpot();
  else if (mode === 'funding-arb') loadFundingArb();
  else if (mode === 'all') loadAll();
  else applyArb();
}

// ── DEX-short arbitrage ──────────────────────────────────────────────────────
// Sticky row-set for REST-polled tables (spot, dex). Same purpose as
// the diff-stickiness on /ws/arb: a specific pair can briefly fall
// out of one poll cycle (mark price hiccup, basis ranking shuffle)
// and come back next cycle. Without stickiness the row blinks. We
// keep the row visible for STICKY_GRACE_MS after it stops appearing.
const STICKY_GRACE_MS = 12000;
function _mergeSticky(prevRows, incoming, keyFn) {
  if (!incoming.length) return prevRows;  // server returned empty — keep prev (existing behaviour)
  const seenKeys = new Set(incoming.map(keyFn));
  const now = Date.now();
  // Update or insert from incoming
  const byKey = new Map();
  for (const r of incoming) byKey.set(keyFn(r), { r, lastSeen: now });
  // Carry over rows from prev that weren't in incoming AND are within grace window
  for (const r of prevRows) {
    const k = keyFn(r);
    if (seenKeys.has(k)) continue;
    const ts = r._lastSeen || (now - 1);
    if (now - ts < STICKY_GRACE_MS) {
      byKey.set(k, { r: { ...r, _stale: true, _lastSeen: ts }, lastSeen: ts });
    }
  }
  return Array.from(byKey.values()).map(({ r, lastSeen }) =>
    Object.assign({}, r, { _lastSeen: r._lastSeen || lastSeen }));
}

let _dexRows = [];
let _dexFiltered = [];
let _dexTimer = null;
let _pageDX = 0;
let _dexSort = { col: 'in_pct', dir: 'desc' };

// /ws/dex-short — Class 1 broadcaster, 2s aggregate diff.
// Wire format: {type:'snapshot', opportunities:[]} on connect; then
// {type:'diff', added?, updated?, removed?:[[sym,short_ex],...]}.
// dex_arbitrage.json itself is rewritten only every 30s (DexScreener
// rate-limit), so most ticks see mtime unchanged and the server skips
// pushing — bandwidth stays near zero between refreshes.
const _dexRowsByKey = new Map();
const _dexKey = (o) => `${o.symbol}|${o.short_exchange}`;
let _wsDex = null;
const _retryDex = { val: 0 }, _pingDex = { val: null }, _retryTimerDex = { val: null };

function _applyDexPayload(data) {
  if (!data) return;
  if (data.type === 'diff') {
    if (Array.isArray(data.added)) for (const o of data.added) _dexRowsByKey.set(_dexKey(o), o);
    if (Array.isArray(data.updated)) for (const o of data.updated) _dexRowsByKey.set(_dexKey(o), o);
    if (Array.isArray(data.removed)) {
      for (const k of data.removed) {
        const key = Array.isArray(k) ? k.join('|') : k;
        _dexRowsByKey.delete(key);
      }
    }
  } else {
    // snapshot — authoritative full set
    _dexRowsByKey.clear();
    for (const o of (data.opportunities || [])) _dexRowsByKey.set(_dexKey(o), o);
  }
  _dexRows = Array.from(_dexRowsByKey.values());
}

const _connectDex = _makeWs({
  path: 'dex-short',
  retryRef: _retryDex, pingRef: _pingDex, retryTimerRef: _retryTimerDex,
  onMessage: (data) => {
    _applyDexPayload(data);
    if (_mode === 'dex') applyDex();
  },
});

async function loadDex() {
  _inOutFirstSorted.dex = false;
  if (!_dexRows.length) {
    document.getElementById('tbody-dex').innerHTML =
      '<tr><td colspan="9" class="empty-msg"><span class="spinner"></span>Scanning DEX pairs…</td></tr>';
  }
  // First paint via REST so we don't wait up to 2s for the first WS tick.
  // After that the WS keeps the table live without timer-based polling.
  try {
    const r = await Auth.apiFetch('/screener/dex-short');
    if (r.ok) {
      const j = await r.json();
      _applyDexPayload({type: 'snapshot', opportunities: j.opportunities || []});
      applyDex();
    }
  } catch (e) {
    if (!_dexRows.length) {
      document.getElementById('tbody-dex').innerHTML = _emptyRow({kind:'error',title:'Failed to load DEX/Short',sub:(e.message||'Network error').slice(0,200),colspan:9,retryFn:'loadDex()'});
    }
  }
  if (!_wsDex || _wsDex.readyState === WebSocket.CLOSED) {
    _wsDex = _connectDex();
  }
  // No timer — the WS pushes diffs at the Class 1 broadcast cadence.
}

function sortDex(col) {
  if (_dexSort.col === col) _dexSort.dir = _dexSort.dir === 'desc' ? 'asc' : 'desc';
  else { _dexSort.col = col; _dexSort.dir = 'desc'; }
  document.querySelectorAll('#tbl-dex th[data-dcol]').forEach(th => {
    const arrow = th.querySelector('.sort-arrow');
    if (th.dataset.dcol === col) { th.classList.add('sorted'); if (arrow) arrow.textContent = _dexSort.dir === 'desc' ? '↓' : '↑'; }
    else { th.classList.remove('sorted'); if (arrow) arrow.textContent = '↕'; }
  });
  applyDex();
}

function applyDex(keepPage = false) {
  const q = (document.getElementById('search').value || '').trim().toUpperCase();
  const minNet = parseFloat(document.getElementById('f-min-net').value) || null;
  const minGS  = parseFloat(document.getElementById('f-min-gs').value) || null;
  const minVol = parseFloat(document.getElementById('f-min-vol').value) || 0;
  _dexFiltered = _dexRows.filter(r => {
    if (q && !r.symbol.toUpperCase().includes(q)) return false;
    if (_exDisabled.has(r.short_exchange)) return false;
    if (_hiddenTokens.has(r.symbol)) return false;
    if (minNet != null && r.net_profit < minNet) return false;
    if (minGS  != null && r.gross < minGS) return false;
    if (minVol && (r.perp_volume_usd || 0) < minVol) return false;
    if ((r.basis_pct || 0) < 0) return false;
    if (r.in_pct === null || r.out_pct === null) return false;
    return true;
  });
  const dir = _dexSort.dir === 'desc' ? -1 : 1;
  _dexFiltered.sort((a, b) => {
    const av = a[_dexSort.col], bv = b[_dexSort.col];
    if (typeof av === 'string') return (av || '').localeCompare(bv || '') * dir;
    return ((av ?? 0) - (bv ?? 0)) * dir;
  });
  if (!keepPage) _pageDX = 0;
  renderDex();
}

function renderDex() {
  const tbody = document.getElementById('tbody-dex');
  const start = _pageDX * PAGE_SIZE;
  const page = _dexFiltered.slice(start, start + PAGE_SIZE);
  if (!_dexFiltered.length) {
    tbody.innerHTML = _emptyRow({
      kind: 'empty',
      title: _dexRows.length ? 'No opportunities match your filter' : 'No DEX/Short data yet',
      sub: _dexRows.length
        ? 'Try widening the spread or fee range above.'
        : 'DEX/Short scan can take 10-30 seconds — DexScreener is rate-limited.',
      colspan: 9,
    });
    renderPager('pager-dex', _pageDX, _dexFiltered.length, 'goPageDX');
    renderDexCards();
    return;
  }
  tbody.innerHTML = page.map(r => {
    const netCls  = r.net_profit > 0 ? 'net-pos' : 'net-neg';
    const netSign = r.net_profit >= 0 ? '+' : '';
    const basisSign = r.basis_pct >= 0 ? '+' : '';
    const basisCls  = r.basis_pct >= 0 ? 'rate-neg' : 'rate-pos';
    const fundSign  = r.short_funding_8h >= 0 ? '+' : '';
    const fundCls   = r.short_funding_8h >= 0 ? 'rate-pos' : 'rate-neg';
    const shealth = _exHealth[r.short_exchange] || {};
    const sdot = `<span class="ex-status-dot ${shealth.klass || ''}" title="${r.short_exchange}"></span>`;
    const dexLabel = (r.dex_name || 'dex').toUpperCase();
    const chain = (r.dex_chain || '').toUpperCase();
    const dexLink = r.dex_pair_url ? `<a href="${r.dex_pair_url}" target="_blank" rel="noopener" onclick="event.stopPropagation()" style="color:var(--purple);text-decoration:none;font-weight:600">${dexLabel}</a>` : `<span style="color:var(--purple);font-weight:600">${dexLabel}</span>`;
    const dexDetailUrl = `/arb?type=dex-short&symbol=${esc(r.symbol)}&chain=${esc(r.dex_chain||'')}&long=${esc(r.dex_name||'')}&short=${esc(r.short_exchange)}&addr=${esc(r.dex_base_address||'')}&pair=${esc((r.dex_pair_url||'').split('/').pop())}`;
    return `<tr data-short-ex="${r.short_exchange}" style="cursor:pointer" onclick="window.open('${dexDetailUrl}','_blank')">
      <td class="td-symbol"><a href="${dexDetailUrl}" target="_blank" onclick="event.stopPropagation()" style="color:inherit;text-decoration:none;border-bottom:1px dotted var(--text3)">${esc(r.symbol)}</a></td>
      <td>
        <div class="arb-pair">
          <div class="arb-ex-rate">
            <span class="arb-label">dex</span>
            ${dexLink}${chain ? `<span style="color:var(--text3);font-size:10px;margin-left:6px">${chain}</span>` : ''}
          </div>
          <div style="font-size:11px;color:var(--text3);margin-left:36px">${fmtPrice(r.dex_price)} · <span style="font-family:var(--mono)">liq ${fmtVol(r.dex_liquidity_usd)}</span></div>
        </div>
      </td>
      <td>
        <div class="arb-pair">
          <div class="arb-ex-rate">
            <span class="arb-label">short</span>
            ${fmtExRate(r.short_exchange, r.funding_rate, r.symbol)}${sdot}
          </div>
          <div style="font-size:11px;color:var(--text3);margin-left:36px">${fmtPrice(r.perp_price)} · <span style="font-family:var(--mono)">${fmtVol(r.perp_volume_usd)}</span></div>
        </div>
      </td>
      <td class="td-inout">${_fmtIO(r.in_pct)}</td>
      <td class="td-inout">${_fmtIO(r.out_pct)}</td>
      <td class="td-gross"><span class="${fundCls}">${fundSign}${r.short_funding_8h.toFixed(4)}%</span><br><span style="font-size:10px;color:var(--text3);font-weight:400">per 8h</span></td>
      <td class="td-fees">
        <span title="dex rt: ${r.fee_dex.toFixed(3)}% + perp rt: ${r.fee_perp.toFixed(3)}%">−${r.total_fees.toFixed(4)}%</span>
      </td>
      <td><span class="td-net ${netCls}">${netSign}${r.net_profit.toFixed(4)}%</span></td>
      <td style="display:flex;gap:4px;align-items:center">
        <a href="/arb?type=dex&symbol=${esc(r.symbol)}&chain=${esc(r.dex_chain)}&long=${esc(r.dex_name)}&short=${esc(r.short_exchange)}&addr=${esc(r.dex_base_address)}&pair=${esc((r.dex_pair_url||'').split('/').pop())}" target="_blank" class="arb-detail-btn" title="Open detail" onclick="event.stopPropagation()">↗</a>
      </td>
    </tr>`;
  }).join('');
  renderPager('pager-dex', _pageDX, _dexFiltered.length, 'goPageDX');
  renderDexCards();
  // in/out comes baked into row data — no fetch needed.
}

function renderDexCards() {
  const wrap = document.getElementById('cards-dex');
  if (!wrap) return;
  if (!_dexFiltered.length) {
    wrap.innerHTML = `<div class="empty-msg-card"><div class="empty-spinner"></div><div class="empty-title">${_dexRows.length ? 'No matches' : 'No DEX/Short data yet'}</div><div class="empty-sub">${_dexRows.length ? 'Adjust filters above.' : 'DexScreener can take 10-30s.'}</div></div>`;
    return;
  }
  const start = _pageDX * PAGE_SIZE;
  const page  = _dexFiltered.slice(start, start + PAGE_SIZE);
  wrap.innerHTML = page.map(r => {
    const netCls   = r.net_profit > 0 ? 'net-pos' : 'net-neg';
    const netSign  = r.net_profit >= 0 ? '+' : '';
    const basisCls = r.basis_pct >= 0 ? 'rate-neg' : 'rate-pos';
    const basisSign = r.basis_pct >= 0 ? '+' : '';
    const fundCls  = r.short_funding_8h >= 0 ? 'rate-pos' : 'rate-neg';
    const fundSign = r.short_funding_8h >= 0 ? '+' : '';
    const aprCls   = r.net_apr > 0 ? 'rate-pos' : r.net_apr < 0 ? 'rate-neg' : 'rate-zero';
    const aprSign  = r.net_apr >= 0 ? '+' : '';
    const dexLabel = (r.dex_name || 'DEX').toUpperCase();
    const chain    = (r.dex_chain || '').toUpperCase();
    const dexChip  = `<span class="ex-chip"><span class="ex-dot" style="background:#A78BFA"></span><span class="ex-name">${dexLabel}${chain ? ` <span style="color:var(--text3);font-weight:500">· ${chain}</span>` : ''}</span></span>`;
    const detailUrl = `/arb?type=dex-short&symbol=${esc(r.symbol)}&chain=${esc(r.dex_chain||'')}&long=${esc(r.dex_name||'')}&short=${esc(r.short_exchange)}&addr=${esc(r.dex_base_address||'')}&pair=${esc((r.dex_pair_url||'').split('/').pop())}`;
    const key = `dex|${r.symbol}|${r.dex_name}|${r.short_exchange}`;
    const isOpen = _openArbKey === key;
    return `
    <div class="card${isOpen?' open':''}" onclick="toggleCard(this)" data-key="${key}">
      <div class="card-head">
        <span class="type-pill tp-dex" style="margin-right:6px">DEX</span>
        ${symbolLink(r.symbol, r.short_exchange)}
        <div class="card-badges">
          ${dexChip}
          <span style="color:var(--text3);font-size:11px">→</span>
          ${exBadge(r.short_exchange, r.symbol)}
        </div>
        <div class="card-right">
          <span class="card-net ${netCls}">${netSign}${r.net_profit.toFixed(4)}%</span>
          <span style="font-size:10px;color:var(--text3);font-family:var(--mono)">net / 8h</span>
        </div>
        <span class="card-chevron">▼</span>
      </div>
      <div class="card-body">
        <div class="card-row">
          <span class="card-lbl">DEX</span>
          <div class="card-exrow">
            <div class="card-exrow-item">${dexChip}</div>
            <div style="font-size:11px;color:var(--text3)">${fmtPrice(r.dex_price)} · liq ${fmtVol(r.dex_liquidity_usd)}</div>
          </div>
        </div>
        <div class="card-row">
          <span class="card-lbl">Short</span>
          <div class="card-exrow">
            <div class="card-exrow-item">${exBadge(r.short_exchange, r.symbol)}</div>
            <div style="font-size:11px;color:var(--text3)">${fmtPrice(r.perp_price)} · ${fmtVol(r.perp_volume_usd)}</div>
          </div>
        </div>
        <div class="card-row">
          <span class="card-lbl">Funding / 8h</span>
          <span class="card-val ${fundCls}">${fundSign}${r.short_funding_8h.toFixed(4)}%</span>
        </div>
        <div class="card-row">
          <span class="card-lbl">Fees</span>
          <span class="card-val" style="color:var(--text3)">−${r.total_fees.toFixed(4)}%</span>
        </div>
        <div class="card-row">
          <span class="card-lbl">Net APR</span>
          <span class="card-val ${aprCls}">${aprSign}${r.net_apr.toFixed(2)}%</span>
        </div>
        <div style="margin-top:12px">
          <a href="${detailUrl}" target="_blank" onclick="event.stopPropagation()" style="display:inline-flex;align-items:center;gap:6px;padding:7px 14px;border-radius:8px;background:var(--surface3);color:var(--text2);font-size:12px;font-weight:600;text-decoration:none">↗ Open detail</a>
        </div>
      </div>
    </div>`;
  }).join('');
}

function goPageDX(p) { _pageDX = p; renderDex(); }

// ── DEX/Spot arbitrage (DEX↔CEX spot-only, no funding/perp) ───────────────────
// Behind go-fetcher AVALANT_DEX_SPOT=1. REST endpoint returns cold envelope
// if the file isn't being written; in that case the table shows the empty
// state with the explanatory subtitle.
let _dexSpotRows = [];
let _dexSpotFiltered = [];
let _pageDS = 0;
let _dexSpotSort = { col: 'abs_spread_pct', dir: 'desc' };

const _dexSpotRowsByKey = new Map();
const _dexSpotKey = (o) => `${o.symbol}|${o.cex_exchange}`;
let _wsDexSpot = null;
const _retryDS = { val: 0 }, _pingDS = { val: null }, _retryTimerDS = { val: null };

function _applyDexSpotPayload(data) {
  if (!data) return;
  if (data.type === 'diff') {
    if (Array.isArray(data.added)) for (const o of data.added) _dexSpotRowsByKey.set(_dexSpotKey(o), o);
    if (Array.isArray(data.updated)) for (const o of data.updated) _dexSpotRowsByKey.set(_dexSpotKey(o), o);
    if (Array.isArray(data.removed)) {
      for (const k of data.removed) {
        const key = Array.isArray(k) ? k.join('|') : k;
        _dexSpotRowsByKey.delete(key);
      }
    }
  } else {
    _dexSpotRowsByKey.clear();
    for (const o of (data.opportunities || [])) _dexSpotRowsByKey.set(_dexSpotKey(o), o);
  }
  _dexSpotRows = Array.from(_dexSpotRowsByKey.values());
}

const _connectDexSpot = _makeWs({
  path: 'dex-spot',
  retryRef: _retryDS, pingRef: _pingDS, retryTimerRef: _retryTimerDS,
  onMessage: (data) => {
    _applyDexSpotPayload(data);
    if (_mode === 'dex-spot') applyDexSpot();
  },
});

async function loadDexSpot() {
  if (!_dexSpotRows.length) {
    document.getElementById('tbody-dex-spot').innerHTML =
      '<tr><td colspan="8" class="empty-msg"><span class="spinner"></span>Scanning DEX↔CEX spot pairs…</td></tr>';
  }
  try {
    const r = await Auth.apiFetch('/screener/dex-spot');
    if (r.ok) {
      const j = await r.json();
      _applyDexSpotPayload({type: 'snapshot', opportunities: j.opportunities || []});
      applyDexSpot();
    }
  } catch (e) {
    if (!_dexSpotRows.length) {
      document.getElementById('tbody-dex-spot').innerHTML = _emptyRow({kind:'error',title:'Failed to load DEX/Spot',sub:(e.message||'Network error').slice(0,200),colspan:8,retryFn:'loadDexSpot()'});
    }
  }
  if (!_wsDexSpot || _wsDexSpot.readyState === WebSocket.CLOSED) {
    _wsDexSpot = _connectDexSpot();
  }
}

function sortDexSpot(col) {
  if (_dexSpotSort.col === col) _dexSpotSort.dir = _dexSpotSort.dir === 'desc' ? 'asc' : 'desc';
  else { _dexSpotSort.col = col; _dexSpotSort.dir = 'desc'; }
  document.querySelectorAll('#tbl-dex-spot th[data-dscol]').forEach(th => {
    const arrow = th.querySelector('.sort-arrow');
    if (th.dataset.dscol === col) { th.classList.add('sorted'); if (arrow) arrow.textContent = _dexSpotSort.dir === 'desc' ? '↓' : '↑'; }
    else                          { th.classList.remove('sorted'); if (arrow) arrow.textContent = '↕'; }
  });
  applyDexSpot();
}

function applyDexSpot(keepPage = false) {
  // Symbol search + exchange filter reuse the same global controls.
  _dexSpotFiltered = _dexSpotRows.filter(r => {
    if (_exDisabled.has(r.cex_exchange)) return false;
    if (_search && !r.symbol.toLowerCase().includes(_search.toLowerCase())) return false;
    return true;
  });
  const dir = _dexSpotSort.dir === 'desc' ? -1 : 1;
  const col = _dexSpotSort.col;
  _dexSpotFiltered.sort((a, b) => {
    let av = a[col], bv = b[col];
    if (typeof av === 'string') return av.localeCompare(bv) * dir;
    return ((+av || 0) - (+bv || 0)) * dir;
  });
  if (!keepPage) _pageDS = 0;
  renderDexSpot();
}

function renderDexSpot() {
  const tbody = document.getElementById('tbody-dex-spot');
  if (!tbody) return;
  if (!_dexSpotFiltered.length) {
    tbody.innerHTML = _emptyRow({
      kind: 'empty',
      title: _dexSpotRows.length ? 'No matches' : 'No DEX/Spot data yet',
      sub: _dexSpotRows.length
        ? 'Adjust filters above.'
        : 'AVALANT_DEX_SPOT=1 must be set on the fetcher. DexScreener can take 10-30s for the first scan.',
      colspan: 8,
    });
    return;
  }
  const start = _pageDS * PAGE_SIZE;
  const page = _dexSpotFiltered.slice(start, start + PAGE_SIZE);
  tbody.innerHTML = page.map(r => {
    const spreadCls = r.abs_spread_pct > 0 ? 'rate-pos' : 'rate-zero';
    const netCls = r.net_pct > 0 ? 'net-pos' : 'net-neg';
    const netSign = r.net_pct >= 0 ? '+' : '';
    const dirArrow = r.direction === 'dex_to_cex' ? '▲' : '▼';
    const dirLabel = r.direction === 'dex_to_cex' ? 'DEX→CEX' : 'CEX→DEX';
    const dirCls = r.direction === 'dex_to_cex' ? 'rate-pos' : 'rate-neg';
    const detailUrl = `/arb?type=dex-spot&symbol=${esc(r.symbol)}&chain=${esc(r.dex_chain||'')}&long=${esc(r.dex_name||'')}&short=${esc(r.cex_exchange)}&addr=${esc(r.dex_base_address||'')}&pair=${esc((r.dex_pair_url||'').split('/').pop())}`;
    return `<tr style="cursor:pointer" onclick="window.open('${detailUrl}','_blank')">
      <td class="td-symbol"><a href="${detailUrl}" target="_blank" onclick="event.stopPropagation()" style="color:inherit;text-decoration:none;border-bottom:1px dotted var(--text3)">${esc(r.symbol)}</a></td>
      <td><div class="cell-ex">${(r.dex_name||'').toUpperCase()}${r.dex_chain ? ` · <span style="color:var(--text3)">${(r.dex_chain||'').toUpperCase()}</span>` : ''}</div><div class="cell-sub">Liq ${fmtVol(r.dex_liquidity_usd)} · Vol ${fmtVol(r.dex_volume_usd)}</div></td>
      <td><div class="cell-ex">${esc(r.cex_exchange)}</div><div class="cell-sub">Vol ${fmtVol(r.cex_volume_usd)}</div></td>
      <td class="${dirCls}" style="white-space:nowrap">${dirArrow} ${dirLabel}</td>
      <td class="num ${spreadCls}">${r.abs_spread_pct.toFixed(4)}%</td>
      <td class="num">${r.total_fees.toFixed(3)}%</td>
      <td class="num ${netCls}">${netSign}${r.net_pct.toFixed(4)}%</td>
      <td><a href="${detailUrl}" target="_blank" onclick="event.stopPropagation()" class="arb-detail-btn" title="Open detail">↗</a></td>
    </tr>`;
  }).join('');
}

function goPageDS(p) { _pageDS = p; renderDexSpot(); }

// ── Spot-short arbitrage (mirrors arb layout + cadence) ──────────────────────
let _spotRows = [];
let _spotFiltered = [];
let _spotTimer = null;
let _pageSP = 0;
let _spotSort = { col: 'in_pct', dir: 'desc' };

// /ws/spot-short — Class 1 broadcaster, 2s aggregate diff.
// Same wire format as /ws/long-short with `spot_exchange` instead of
// `long_exchange`. Removed-row payload is [sym, spot_ex, short_ex].
const _spotRowsByKey = new Map();
const _spotKey = (o) => `${o.symbol}|${o.spot_exchange}|${o.short_exchange}`;
let _wsSpot = null;
const _retrySp = { val: 0 }, _pingSp = { val: null }, _retryTimerSp = { val: null };

function _applySpotPayload(data) {
  if (!data) return;
  if (data.type === 'diff') {
    if (Array.isArray(data.added)) for (const o of data.added) _spotRowsByKey.set(_spotKey(o), o);
    if (Array.isArray(data.updated)) for (const o of data.updated) _spotRowsByKey.set(_spotKey(o), o);
    if (Array.isArray(data.removed)) {
      for (const k of data.removed) {
        const key = Array.isArray(k) ? k.join('|') : k;
        _spotRowsByKey.delete(key);
      }
    }
  } else {
    _spotRowsByKey.clear();
    for (const o of (data.opportunities || [])) _spotRowsByKey.set(_spotKey(o), o);
  }
  _spotRows = Array.from(_spotRowsByKey.values());
}

const _connectSpot = _makeWs({
  path: 'spot-short',
  retryRef: _retrySp, pingRef: _pingSp, retryTimerRef: _retryTimerSp,
  onMessage: (data) => {
    _applySpotPayload(data);
    if (_mode === 'spot') applySpot();
  },
});

async function loadSpot() {
  _inOutFirstSorted.spot = false;
  if (!_spotRows.length) {
    document.getElementById('tbody-spot').innerHTML =
      '<tr><td colspan="9" class="empty-msg"><span class="spinner"></span>Computing spot-short arbitrage…</td></tr>';
  }
  // First-paint via REST (cold-start cache might be empty). After that the
  // WS keeps the table live without timer-based polling.
  try {
    const r = await Auth.apiFetch('/screener/spot-short');
    if (r.ok) {
      const j = await r.json();
      _applySpotPayload({type: 'snapshot', opportunities: j.opportunities || []});
      applySpot();
    }
  } catch (e) {
    if (!_spotRows.length) {
      document.getElementById('tbody-spot').innerHTML = _emptyRow({kind:'error',title:'Failed to load Spot/Short',sub:(e.message||'Network error').slice(0,200),colspan:9,retryFn:'loadSpot()'});
    }
  }
  if (!_wsSpot || _wsSpot.readyState === WebSocket.CLOSED) {
    _wsSpot = _connectSpot();
  }
}

function sortSpot(col) {
  if (_spotSort.col === col) _spotSort.dir = _spotSort.dir === 'desc' ? 'asc' : 'desc';
  else { _spotSort.col = col; _spotSort.dir = 'desc'; }
  document.querySelectorAll('#tbl-spot th[data-scol]').forEach(th => {
    const arrow = th.querySelector('.sort-arrow');
    if (th.dataset.scol === col) { th.classList.add('sorted'); if (arrow) arrow.textContent = _spotSort.dir === 'desc' ? '↓' : '↑'; }
    else { th.classList.remove('sorted'); if (arrow) arrow.textContent = '↕'; }
  });
  applySpot();
}

function applySpot(keepPage = false) {
  const q = (document.getElementById('search').value || '').trim().toUpperCase();
  const minNet = parseFloat(document.getElementById('f-min-net').value) || null;
  const minGS  = parseFloat(document.getElementById('f-min-gs').value) || null;
  const minVol = parseFloat(document.getElementById('f-min-vol').value) || 0;
  _spotFiltered = _spotRows.filter(r => {
    if (q && !r.symbol.toUpperCase().includes(q)) return false;
    if (_exDisabled.has(r.spot_exchange) || _exDisabled.has(r.short_exchange)) return false;
    if (_hiddenTokens.has(r.symbol)) return false;
    if (minNet != null && r.net_profit < minNet) return false;
    if (minGS  != null && r.gross < minGS) return false;
    if (minVol && (Math.min(r.spot_volume_usd || 0, r.perp_volume_usd || 0) < minVol)) return false;
    // Drop reverse direction (negative basis = perp below spot, no
    // arb to capture).
    if ((r.basis_pct || 0) < 0) return false;
    if (r.in_pct === null || r.out_pct === null) return false;
    return true;
  });
  const dir = _spotSort.dir === 'desc' ? -1 : 1;
  // Push null/undefined to the bottom regardless of direction — rows
  // without a live in_pct (no orderbook subscription) shouldn't pollute
  // the actionable top of the table.
  const NULL_DESC = -Infinity, NULL_ASC = Infinity;
  _spotFiltered.sort((a, b) => {
    let av = a[_spotSort.col], bv = b[_spotSort.col];
    if (typeof av === 'string') return av.localeCompare(bv) * dir;
    if (av == null) av = dir === -1 ? NULL_DESC : NULL_ASC;
    if (bv == null) bv = dir === -1 ? NULL_DESC : NULL_ASC;
    return (av - bv) * dir;
  });
  if (!keepPage) _pageSP = 0;
  renderSpot();
}

function renderSpot() {
  const tbody = document.getElementById('tbody-spot');
  const start = _pageSP * PAGE_SIZE;
  const page = _spotFiltered.slice(start, start + PAGE_SIZE);
  if (!_spotFiltered.length) {
    tbody.innerHTML = _emptyRow({
      kind: 'empty',
      title: _spotRows.length ? 'No opportunities match your filter' : 'No Spot/Short data yet',
      sub: _spotRows.length
        ? 'Try widening the spread or fee range above.'
        : 'Spot/perp basis recomputed every 500ms — first paint usually within ~5s.',
      colspan: 9,
    });
    renderPager('pager-spot', _pageSP, _spotFiltered.length, 'goPageSP');
    renderSpotCards();
    return;
  }
  tbody.innerHTML = page.map(r => {
    const netCls  = r.net_profit > 0 ? 'net-pos' : 'net-neg';
    const netSign = r.net_profit >= 0 ? '+' : '';
    const grossSign = r.gross >= 0 ? '+' : '';
    const basisSign = r.basis_pct >= 0 ? '+' : '';
    const basisCls  = r.basis_pct >= 0 ? 'rate-neg' : 'rate-pos'; // positive basis = pay more for perp = cost at entry → red
    const fundSign  = r.short_funding_8h >= 0 ? '+' : '';
    const fundCls   = r.short_funding_8h >= 0 ? 'rate-pos' : 'rate-neg';
    const lhealth = _exHealth[r.spot_exchange] || {};
    const shealth = _exHealth[r.short_exchange] || {};
    const ldot = `<span class="ex-status-dot ${lhealth.klass || ''}" title="${r.spot_exchange}"></span>`;
    const sdot = `<span class="ex-status-dot ${shealth.klass || ''}" title="${r.short_exchange}"></span>`;
    const spotDetailUrl = `/arb?type=spot-short&symbol=${esc(r.symbol)}&long=${esc(r.spot_exchange)}&short=${esc(r.short_exchange)}`;
    return `<tr data-long-ex="${r.spot_exchange}_spot" data-short-ex="${r.short_exchange}" data-symbol="${r.symbol}" data-kind="spot" style="cursor:pointer" onclick="window.open('${spotDetailUrl}','_blank')">
      <td class="td-symbol"><a href="${spotDetailUrl}" target="_blank" onclick="event.stopPropagation()" style="color:inherit;text-decoration:none;border-bottom:1px dotted var(--text3)">${esc(r.symbol)}</a></td>
      <td>
        <div class="arb-pair">
          <div class="arb-ex-rate">
            <span class="arb-label">spot</span>
            ${window.EX.chip(r.spot_exchange)}${ldot}
          </div>
          <div style="font-size:11px;color:var(--text3);margin-left:36px">${fmtPrice(r.spot_price)} · <span style="font-family:var(--mono)">${fmtVol(r.spot_volume_usd)}</span></div>
        </div>
      </td>
      <td>
        <div class="arb-pair">
          <div class="arb-ex-rate">
            <span class="arb-label">short</span>
            ${fmtExRate(r.short_exchange, r.funding_rate, r.symbol)}${sdot}
          </div>
          <div style="font-size:11px;color:var(--text3);margin-left:36px">${fmtPrice(r.perp_price)} · <span style="font-family:var(--mono)">${fmtVol(r.perp_volume_usd)}</span></div>
        </div>
      </td>
      <td class="td-inout">${_fmtIO(r.in_pct)}</td>
      <td class="td-inout">${_fmtIO(r.out_pct)}</td>
      <td class="td-gross"><span class="${fundCls}">${fundSign}${r.short_funding_8h.toFixed(4)}%</span><br><span style="font-size:10px;color:var(--text3);font-weight:400">per 8h</span></td>
      <td class="td-fees">
        <span title="spot rt: ${r.fee_spot.toFixed(3)}% + perp rt: ${r.fee_perp.toFixed(3)}%">−${r.total_fees.toFixed(4)}%</span>
      </td>
      <td><span class="td-net ${netCls}">${netSign}${r.net_profit.toFixed(4)}%</span></td>
      <td style="display:flex;gap:4px;align-items:center">
        <a href="/arb?type=spot&symbol=${esc(r.symbol)}&long=${esc(r.spot_exchange)}&short=${esc(r.short_exchange)}" target="_blank" class="arb-detail-btn" title="Open detail" onclick="event.stopPropagation()">↗</a>
      </td>
    </tr>`;
  }).join('');
  renderPager('pager-spot', _pageSP, _spotFiltered.length, 'goPageSP');
  renderSpotCards();
  // in/out comes baked into row data — no fetch needed.
}

function renderSpotCards() {
  const wrap = document.getElementById('cards-spot');
  if (!wrap) return;
  if (!_spotFiltered.length) {
    wrap.innerHTML = `<div class="empty-msg-card"><div class="empty-spinner"></div><div class="empty-title">${_spotRows.length ? 'No matches' : 'No Spot/Short data yet'}</div><div class="empty-sub">${_spotRows.length ? 'Adjust filters above.' : 'First paint usually <5s.'}</div></div>`;
    return;
  }
  const start = _pageSP * PAGE_SIZE;
  const page  = _spotFiltered.slice(start, start + PAGE_SIZE);
  wrap.innerHTML = page.map(r => {
    const netCls   = r.net_profit > 0 ? 'net-pos' : 'net-neg';
    const netSign  = r.net_profit >= 0 ? '+' : '';
    const basisCls = r.basis_pct >= 0 ? 'rate-neg' : 'rate-pos';
    const basisSign = r.basis_pct >= 0 ? '+' : '';
    const fundCls  = r.short_funding_8h >= 0 ? 'rate-pos' : 'rate-neg';
    const fundSign = r.short_funding_8h >= 0 ? '+' : '';
    const aprCls   = r.net_apr > 0 ? 'rate-pos' : r.net_apr < 0 ? 'rate-neg' : 'rate-zero';
    const aprSign  = r.net_apr >= 0 ? '+' : '';
    const key = `spot|${r.symbol}|${r.spot_exchange}|${r.short_exchange}`;
    const isOpen = _openArbKey === key;
    return `
    <div class="card${isOpen?' open':''}" onclick="toggleCard(this)" data-key="${key}">
      <div class="card-head">
        <span class="type-pill tp-spot" style="margin-right:6px">SPOT</span>
        ${symbolLink(r.symbol, r.spot_exchange)}
        <div class="card-badges">
          ${exBadge(r.spot_exchange, r.symbol)}
          <span style="color:var(--text3);font-size:11px">→</span>
          ${exBadge(r.short_exchange, r.symbol)}
        </div>
        <div class="card-right">
          <span class="card-net ${netCls}">${netSign}${r.net_profit.toFixed(4)}%</span>
          <span style="font-size:10px;color:var(--text3);font-family:var(--mono)">net / 8h</span>
        </div>
        <span class="card-chevron">▼</span>
      </div>
      <div class="card-body">
        <div class="card-row">
          <span class="card-lbl">Spot</span>
          <div class="card-exrow">
            <div class="card-exrow-item">${exBadge(r.spot_exchange, r.symbol)}</div>
            <div style="font-size:11px;color:var(--text3)">${fmtPrice(r.spot_price)} · ${fmtVol(r.spot_volume_usd)}</div>
          </div>
        </div>
        <div class="card-row">
          <span class="card-lbl">Short</span>
          <div class="card-exrow">
            <div class="card-exrow-item">${exBadge(r.short_exchange, r.symbol)}</div>
            <div style="font-size:11px;color:var(--text3)">${fmtPrice(r.perp_price)} · ${fmtVol(r.perp_volume_usd)}</div>
          </div>
        </div>
        <div class="card-row">
          <span class="card-lbl">Funding / 8h</span>
          <span class="card-val ${fundCls}">${fundSign}${r.short_funding_8h.toFixed(4)}%</span>
        </div>
        <div class="card-row">
          <span class="card-lbl">Fees</span>
          <span class="card-val" style="color:var(--text3)">−${r.total_fees.toFixed(4)}%</span>
        </div>
        <div class="card-row">
          <span class="card-lbl">Net APR</span>
          <span class="card-val ${aprCls}">${aprSign}${r.net_apr.toFixed(2)}%</span>
        </div>
        <div style="margin-top:12px">
          <a href="/arb?type=spot-short&symbol=${esc(r.symbol)}&long=${esc(r.spot_exchange)}&short=${esc(r.short_exchange)}" target="_blank" onclick="event.stopPropagation()" style="display:inline-flex;align-items:center;gap:6px;padding:7px 14px;border-radius:8px;background:var(--surface3);color:var(--text2);font-size:12px;font-weight:600;text-decoration:none">↗ Open detail</a>
        </div>
      </div>
    </div>`;
  }).join('');
}

function goPageSP(p) { _pageSP = p; renderSpot(); }

// ── Funding Arb ──────────────────────────────────────────────────────────────
// Pure-funding play: positive short funding, basis ignored. Reuses /screener/
// spot-short data — backend already emits same-venue rows (no spotEx==perpEx
// skip in spot.go). Filter to funding_rate > 0, sort by net_funding_8h.
let _faFiltered = [];
let _pageFA = 0;
let _faSort = { col: 'short_funding_8h', dir: 'desc' };
let _faTimer = null;

async function loadFundingArb() {
  // Reuse the spot data path so we don't double-poll.
  await loadSpot();
  applyFA();
  clearInterval(_faTimer);
  _faTimer = setInterval(() => {
    if (document.hidden) return;
    if (_mode === 'funding-arb') { loadSpot().then(applyFA); }
  }, 1000);
}

function sortFA(col) {
  if (_faSort.col === col) _faSort.dir = _faSort.dir === 'desc' ? 'asc' : 'desc';
  else { _faSort.col = col; _faSort.dir = 'desc'; }
  document.querySelectorAll('#tbl-fa th[data-facol]').forEach(th => {
    const arrow = th.querySelector('.sort-arrow');
    if (th.dataset.facol === col) { th.classList.add('sorted'); if (arrow) arrow.textContent = _faSort.dir === 'desc' ? '↓' : '↑'; }
    else { th.classList.remove('sorted'); if (arrow) arrow.textContent = '↕'; }
  });
  applyFA();
}

function applyFA(keepPage = false) {
  const q = (document.getElementById('search').value || '').trim().toUpperCase();
  const minVol = parseFloat(document.getElementById('f-min-vol').value) || 0;
  // Funding arb keeps positive-funding rows only — that's the whole point.
  // Same-venue rows ARE allowed (purpose of this tab). Compute net_funding_8h
  // and funding_apr on the fly so we can sort by them.
  _faFiltered = _spotRows
    .filter(r => {
      if (q && !r.symbol.toUpperCase().includes(q)) return false;
      if (_exDisabled.has(r.spot_exchange) || _exDisabled.has(r.short_exchange)) return false;
      if (_hiddenTokens.has(r.symbol)) return false;
      if ((r.short_funding_8h || 0) <= 0) return false;
      if (minVol && (Math.min(r.spot_volume_usd || 0, r.perp_volume_usd || 0) < minVol)) return false;
      return true;
    })
    .map(r => ({
      ...r,
      net_funding_8h: (r.short_funding_8h || 0) - (r.total_fees || 0),
      // 3 settlements/day × 365 days = 1095. Approx for 8h-cycle venues.
      funding_apr: (r.short_funding_8h || 0) * 3 * 365,
      same_venue: r.spot_exchange === r.short_exchange,
    }));
  const dir = _faSort.dir === 'desc' ? -1 : 1;
  _faFiltered.sort((a, b) => {
    let av = a[_faSort.col], bv = b[_faSort.col];
    if (typeof av === 'string') return av.localeCompare(bv) * dir;
    if (av == null) av = dir === -1 ? -Infinity : Infinity;
    if (bv == null) bv = dir === -1 ? -Infinity : Infinity;
    return (av - bv) * dir;
  });
  if (!keepPage) _pageFA = 0;
  renderFA();
}

function renderFA() {
  const tbody = document.getElementById('tbody-fa');
  if (!tbody) return;
  const start = _pageFA * PAGE_SIZE;
  const page = _faFiltered.slice(start, start + PAGE_SIZE);
  if (!_faFiltered.length) {
    tbody.innerHTML = _emptyRow({
      kind: 'empty',
      title: _spotRows.length ? 'No positive-funding pairs match' : 'No funding-arb data yet',
      sub: _spotRows.length
        ? 'Try lowering the minimum spread or fee floor above.'
        : 'Funding arb recomputes every 500ms across active funding cycles.',
      colspan: 9,
    });
    renderPager('pager-fa', _pageFA, _faFiltered.length, 'goPageFA');
    renderFACards();
    return;
  }
  tbody.innerHTML = page.map(r => {
    const netCls = r.net_funding_8h > 0 ? 'net-pos' : 'net-neg';
    const netSign = r.net_funding_8h >= 0 ? '+' : '';
    const aprCls = r.funding_apr > 0 ? 'rate-pos' : 'rate-neg';
    const basisAbs = Math.abs(r.basis_pct || 0);
    const basisCls = basisAbs < 0.5 ? 'rate-pos' : (basisAbs < 2 ? 'rate-zero' : 'rate-neg');
    const lhealth = _exHealth[r.spot_exchange] || {};
    const shealth = _exHealth[r.short_exchange] || {};
    const ldot = `<span class="ex-status-dot ${lhealth.klass || ''}" title="${r.spot_exchange}"></span>`;
    const sdot = `<span class="ex-status-dot ${shealth.klass || ''}" title="${r.short_exchange}"></span>`;
    const sameBadge = r.same_venue ? '<span style="background:rgba(26,255,171,0.15);color:#1AFFAB;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:700;margin-left:6px">SAME</span>' : '';
    const detailUrl = `/arb?type=spot-short&symbol=${esc(r.symbol)}&long=${esc(r.spot_exchange)}&short=${esc(r.short_exchange)}`;
    return `<tr style="cursor:pointer" onclick="window.open('${detailUrl}','_blank')">
      <td class="td-symbol"><a href="${detailUrl}" target="_blank" onclick="event.stopPropagation()" style="color:inherit;text-decoration:none;border-bottom:1px dotted var(--text3)">${esc(r.symbol)}</a>${sameBadge}</td>
      <td>
        <div class="arb-pair">
          <div class="arb-ex-rate"><span class="arb-label">spot</span>${window.EX.chip(r.spot_exchange)}${ldot}</div>
          <div style="font-size:11px;color:var(--text3);margin-left:36px">${fmtPrice(r.spot_price)} · <span style="font-family:var(--mono)">${fmtVol(r.spot_volume_usd)}</span></div>
        </div>
      </td>
      <td>
        <div class="arb-pair">
          <div class="arb-ex-rate"><span class="arb-label">short</span>${window.EX.chip(r.short_exchange)}${sdot}</div>
          <div style="font-size:11px;color:var(--text3);margin-left:36px">${fmtPrice(r.perp_price)} · <span style="font-family:var(--mono)">${fmtVol(r.perp_volume_usd)}</span></div>
        </div>
      </td>
      <td class="td-gross"><span class="rate-pos">+${r.short_funding_8h.toFixed(4)}%</span><br><span style="font-size:10px;color:var(--text3);font-weight:400">per 8h</span></td>
      <td><span class="${aprCls}" style="font-weight:700">+${r.funding_apr.toFixed(2)}%</span><br><span style="font-size:10px;color:var(--text3);font-weight:400">APR</span></td>
      <td class="${basisCls}">${(r.basis_pct >= 0 ? '+' : '')}${r.basis_pct.toFixed(4)}%</td>
      <td class="td-fees">−${r.total_fees.toFixed(4)}%</td>
      <td><span class="td-net ${netCls}">${netSign}${r.net_funding_8h.toFixed(4)}%</span></td>
      <td><a href="${detailUrl}" target="_blank" class="arb-detail-btn" title="Open detail" onclick="event.stopPropagation()">↗</a></td>
    </tr>`;
  }).join('');
  renderPager('pager-fa', _pageFA, _faFiltered.length, 'goPageFA');
  renderFACards();
}

function renderFACards() {
  const wrap = document.getElementById('cards-fa');
  if (!wrap) return;
  if (!_faFiltered.length) {
    wrap.innerHTML = `<div class="empty-msg-card"><div class="empty-spinner"></div><div class="empty-title">${_spotRows.length ? 'No matches' : 'No funding-arb data yet'}</div><div class="empty-sub">${_spotRows.length ? 'Lower the spread floor above.' : 'First paint within ~5s.'}</div></div>`;
    return;
  }
  const start = _pageFA * PAGE_SIZE;
  const page = _faFiltered.slice(start, start + PAGE_SIZE);
  wrap.innerHTML = page.map(r => {
    const netCls = r.net_funding_8h > 0 ? 'net-pos' : 'net-neg';
    const netSign = r.net_funding_8h >= 0 ? '+' : '';
    const sameBadge = r.same_venue ? '<span style="background:rgba(26,255,171,0.15);color:#1AFFAB;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:700;margin-left:6px">SAME</span>' : '';
    return `
    <div class="card">
      <div class="card-head">
        <span class="type-pill tp-spot" style="margin-right:6px">FA</span>
        ${symbolLink(r.symbol, r.spot_exchange)}${sameBadge}
        <div class="card-badges">${exBadge(r.spot_exchange, r.symbol)}<span style="color:var(--text3);font-size:11px">→</span>${exBadge(r.short_exchange, r.symbol)}</div>
        <div class="card-right">
          <span class="card-net ${netCls}">${netSign}${r.net_funding_8h.toFixed(4)}%</span>
          <span style="font-size:10px;color:var(--text3);font-family:var(--mono)">net funding /8h</span>
        </div>
      </div>
      <div class="card-row">
        <span>Funding /8h</span><span class="rate-pos" style="font-weight:700">+${r.short_funding_8h.toFixed(4)}%</span>
      </div>
      <div class="card-row">
        <span>APR</span><span class="rate-pos" style="font-weight:700">+${r.funding_apr.toFixed(2)}%</span>
      </div>
      <div class="card-row">
        <span>Basis</span><span>${(r.basis_pct >= 0 ? '+' : '')}${r.basis_pct.toFixed(4)}%</span>
      </div>
      <div class="card-row">
        <span>Fees</span><span>−${r.total_fees.toFixed(4)}%</span>
      </div>
    </div>`;
  }).join('');
}

function goPageFA(p) { _pageFA = p; renderFA(); }

// ── All (combined) ───────────────────────────────────────────────────────────
let _allRows = [];
let _allTimer = null;
async function loadAll() {
  try {
    const r = await Auth.apiFetch('/screener/all-arbitrage');
    if (!r.ok) throw new Error('all ' + r.status);
    const j = await r.json();
    const incoming = j.opportunities || [];
    if (incoming.length === 0 && _allRows.length > 0) {
      console.debug('[all] kept last snapshot — server returned 0 opps');
    } else {
      _allRows = incoming;
    }
    renderAll();
  } catch (e) {
    if (!_allRows.length) {
      document.getElementById('tbody-all').innerHTML = _emptyRow({kind:'error',title:'Failed to load combined feed',sub:(e.message||'Network error').slice(0,200),colspan:8,retryFn:'loadAll()'});
    }
  }
  clearInterval(_allTimer);
  _allTimer = setInterval(() => { if (document.hidden) return; if (_mode === 'all') loadAll(); }, 2000);
}

let _allTypeFilter = 'all';
function setAllType(t, btn) {
  _allTypeFilter = t;
  document.querySelectorAll('.all-type-chip').forEach(c => c.classList.toggle('active', c === btn));
  renderAll();
}
function _countAll() {
  const c = { futures: 0, spot_short: 0, dex_short: 0 };
  for (const r of _allRows) {
    const t = r.type || 'futures';
    if (c[t] != null) c[t]++;
  }
  const el = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
  el('all-cnt-all', _allRows.length);
  el('all-cnt-fut', c.futures);
  el('all-cnt-spot', c.spot_short);
  el('all-cnt-dex', c.dex_short);
}
function renderAll() {
  _countAll();
  const q = (document.getElementById('search').value || '').trim().toUpperCase();
  const minVol = parseFloat(document.getElementById('f-min-vol').value) || 0;
  const rows = _allRows.filter(r => {
    const rType = r.type || 'futures';
    if (_allTypeFilter !== 'all' && rType !== _allTypeFilter) return false;
    if (q && !r.symbol.toUpperCase().includes(q)) return false;
    if (_hiddenTokens.has(r.symbol)) return false;
    const longEx =
      r.type === 'spot_short' ? r.spot_exchange :
      r.type === 'dex_short'  ? null :
                                r.long_exchange;
    const shortEx = r.short_exchange;
    if (longEx && _exDisabled.has(longEx)) return false;
    if (_exDisabled.has(shortEx)) return false;
    if (minVol > 0) {
      // Per-row leg-min: futures = min(long, short); spot/dex = min(long-side, perp).
      // Dex rows expose dex_volume_usd or dex_liquidity_usd as the long-side proxy.
      const longV = r.type === 'spot_short' ? (r.spot_volume_usd || 0)
                  : r.type === 'dex_short'  ? (r.dex_volume_usd  || r.dex_liquidity_usd || 0)
                  :                           (r.long_volume     || 0);
      const shortV = r.type === 'spot_short' ? (r.perp_volume_usd || 0)
                   : r.type === 'dex_short'  ? (r.perp_volume_usd || 0)
                   :                           (r.short_volume    || 0);
      if (Math.min(longV, shortV) < minVol) return false;
    }
    // Drop rows without a live orderbook on at least the short leg —
    // an arb you can't enter isn't an opportunity. Same rule the
    // spot/dex tabs apply, just not previously enforced on the
    // combined All view. Without it LIT-style cross-listings with
    // 28% mark spread (Binance spot 0.74 vs perp 0.95, no spot book
    // subscribed) sat at the top of the list as fake top opps.
    if (r.in_pct === null || r.in_pct === undefined) return false;
    return true;
  });
  const tb = document.getElementById('tbody-all');
  if (!rows.length) {
    tb.innerHTML = `<tr><td colspan="8" class="empty-msg">No opportunities found.</td></tr>`;
    renderAllCards(rows);
    return;
  }
  tb.innerHTML = rows.slice(0, 300).map(r => {
    const isSpot = r.type === 'spot_short';
    const isDex  = r.type === 'dex_short';
    const shortEx = r.short_exchange;

    const gross = (isSpot || isDex) ? (r.gross || 0) : ((r.gross_funding || 0) + (r.price_spread || 0));
    const net   = r.net_profit || 0;
    const apr   = r.net_apr || 0;

    const grossSign = gross >= 0 ? '+' : '';
    const netCls    = net > 0 ? 'net-pos' : 'net-neg';
    const netSign   = net >= 0 ? '+' : '';

    let longCell;
    if (isDex) {
      const dexName  = (r.dex_name  || 'DEX').toUpperCase();
      const dexChain = (r.dex_chain || '').toUpperCase();
      longCell = `<span class="ex-chip"><span class="ex-dot" style="background:#A78BFA"></span><span class="ex-name">${dexName}<span style="color:var(--text3);font-weight:500"> · ${dexChain}</span></span></span>`;
    } else if (isSpot) {
      longCell = window.EX.chip(r.spot_exchange);
    } else {
      longCell = window.EX.chip(r.long_exchange);
    }

    const typeChip = isDex
      ? `<span class="type-pill tp-dex">DEX/SHORT</span>`
      : isSpot
      ? `<span class="type-pill tp-spot">SPOT/SHORT</span>`
      : `<span class="type-pill tp-ls">LONG/SHORT</span>`;

    const detailUrl = isDex
      ? `/arb?type=dex-short&symbol=${esc(r.symbol)}&chain=${esc(r.dex_chain||'')}&long=${esc(r.dex_name||'')}&short=${esc(shortEx)}&addr=${esc(r.dex_base_address||'')}&pair=${esc((r.dex_pair_url||'').split('/').pop())}`
      : isSpot
      ? `/arb?type=spot-short&symbol=${esc(r.symbol)}&long=${esc(r.spot_exchange)}&short=${esc(shortEx)}`
      : `/arb?type=long-short&symbol=${esc(r.symbol)}&long=${esc(r.long_exchange)}&short=${esc(shortEx)}`;

    return `<tr style="cursor:pointer" onclick="window.open('${detailUrl}','_blank')">
      <td>${typeChip}</td>
      <td class="td-symbol"><a href="${detailUrl}" target="_blank" onclick="event.stopPropagation()" style="color:inherit;text-decoration:none;border-bottom:1px dotted var(--text3)">${esc(r.symbol)}</a></td>
      <td>${longCell}</td>
      <td>${window.EX.chip(shortEx)}</td>
      <td class="td-gross">${grossSign}${gross.toFixed(4)}%</td>
      <td><span class="td-net ${netCls}">${netSign}${net.toFixed(4)}%</span></td>
      <td>${fmtApr(apr)}</td>
      <td><a href="${detailUrl}" target="_blank" class="arb-detail-btn" onclick="event.stopPropagation()" title="Open detail">↗</a></td>
    </tr>`;
  }).join('');
  renderAllCards(rows);
}

function renderAllCards(rows) {
  const wrap = document.getElementById('cards-all');
  if (!wrap) return;
  if (!rows || !rows.length) {
    wrap.innerHTML = '<div class="empty-msg">No opportunities found.</div>';
    return;
  }
  wrap.innerHTML = rows.slice(0, 300).map(r => {
    const isSpot = r.type === 'spot_short';
    const isDex  = r.type === 'dex_short';
    const shortEx = r.short_exchange;
    const gross = (isSpot || isDex) ? (r.gross || 0) : ((r.gross_funding || 0) + (r.price_spread || 0));
    const net   = r.net_profit || 0;
    const apr   = r.net_apr || 0;
    const grossSign = gross >= 0 ? '+' : '';
    const netCls    = net > 0 ? 'net-pos' : 'net-neg';
    const netSign   = net >= 0 ? '+' : '';
    const aprCls    = apr > 0 ? 'rate-pos' : apr < 0 ? 'rate-neg' : 'rate-zero';
    const aprSign   = apr >= 0 ? '+' : '';

    let longCell;
    if (isDex) {
      const dexName  = (r.dex_name  || 'DEX').toUpperCase();
      const dexChain = (r.dex_chain || '').toUpperCase();
      longCell = `<span class="ex-chip"><span class="ex-dot" style="background:#A78BFA"></span><span class="ex-name">${dexName}${dexChain ? ` <span style="color:var(--text3);font-weight:500">· ${dexChain}</span>` : ''}</span></span>`;
    } else if (isSpot) {
      longCell = window.EX.chip(r.spot_exchange);
    } else {
      longCell = window.EX.chip(r.long_exchange);
    }

    const typeChip = isDex
      ? `<span class="type-pill tp-dex">DEX/SHORT</span>`
      : isSpot
      ? `<span class="type-pill tp-spot">SPOT/SHORT</span>`
      : `<span class="type-pill tp-ls">LONG/SHORT</span>`;

    const detailUrl = isDex
      ? `/arb?type=dex-short&symbol=${esc(r.symbol)}&chain=${esc(r.dex_chain||'')}&long=${esc(r.dex_name||'')}&short=${esc(shortEx)}&addr=${esc(r.dex_base_address||'')}&pair=${esc((r.dex_pair_url||'').split('/').pop())}`
      : isSpot
      ? `/arb?type=spot-short&symbol=${esc(r.symbol)}&long=${esc(r.spot_exchange)}&short=${esc(shortEx)}`
      : `/arb?type=long-short&symbol=${esc(r.symbol)}&long=${esc(r.long_exchange)}&short=${esc(shortEx)}`;

    const longLbl = isDex ? 'DEX' : isSpot ? 'Spot' : 'Long';
    const key = `all|${r.type || 'futures'}|${r.symbol}|${r.long_exchange || r.spot_exchange || r.dex_name}|${shortEx}`;
    const isOpen = _openArbKey === key;
    return `
    <div class="card${isOpen?' open':''}" onclick="toggleCard(this)" data-key="${key}">
      <div class="card-head">
        ${typeChip}
        ${symbolLink(r.symbol, shortEx)}
        <div class="card-badges">
          ${longCell}
          <span style="color:var(--text3);font-size:11px">→</span>
          ${window.EX.chip(shortEx)}
        </div>
        <div class="card-right">
          <span class="card-net ${netCls}">${netSign}${net.toFixed(4)}%</span>
          <span style="font-size:10px;color:var(--text3);font-family:var(--mono)">net / 8h</span>
        </div>
        <span class="card-chevron">▼</span>
      </div>
      <div class="card-body">
        <div class="card-row">
          <span class="card-lbl">${longLbl}</span>
          <span>${longCell}</span>
        </div>
        <div class="card-row">
          <span class="card-lbl">Short</span>
          <span>${window.EX.chip(shortEx)}</span>
        </div>
        <div class="card-row">
          <span class="card-lbl">Gross</span>
          <span class="card-val">${grossSign}${gross.toFixed(4)}%</span>
        </div>
        <div class="card-row">
          <span class="card-lbl">Net APR</span>
          <span class="card-val ${aprCls}">${aprSign}${apr.toFixed(2)}%</span>
        </div>
        <div style="margin-top:12px">
          <a href="${detailUrl}" target="_blank" onclick="event.stopPropagation()" style="display:inline-flex;align-items:center;gap:6px;padding:7px 14px;border-radius:8px;background:var(--surface3);color:var(--text2);font-size:12px;font-weight:600;text-decoration:none">↗ Open detail</a>
        </div>
      </div>
    </div>`;
  }).join('');
}

function toggleValidOnly() {
  _validOnly = !_validOnly;
  ['f-valid-toggle','f-valid-toggle-mob'].forEach(id => { const el = document.getElementById(id); if (el) el.classList.toggle('active', _validOnly); });
  ['f-valid-dot','f-valid-dot-mob'].forEach(id => { const el = document.getElementById(id); if (el) el.style.opacity = _validOnly ? '1' : '0.4'; });
  applyArb();
}

function toggleCrossOnly() {
  _crossOnly = !_crossOnly;
  ['f-cross-toggle','f-cross-toggle-mob'].forEach(id => { const el = document.getElementById(id); if (el) el.classList.toggle('active', _crossOnly); });
  ['f-cross-dot','f-cross-dot-mob'].forEach(id => { const el = document.getElementById(id); if (el) el.style.opacity = _crossOnly ? '1' : '0.4'; });
  applyFilter();
}

// ── WebSocket helpers ──────────────────────────────────────────────────────────
function _wsUrl(path) {
  // Token is sent via the first frame after onopen — keeping it out of the
  // URL stops nginx from logging session JWTs.
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${location.host}/api/screener/ws/${path}`;
}

function setWsStatus(state) {
  const states = {
    live:         ['ws-dot live',  'Live'],
    connecting:   ['ws-dot',       'Connecting…'],
    reconnecting: ['ws-dot error', 'Reconnecting…'],
    error:        ['ws-dot error', 'Disconnected'],
  };
  const [cls, text] = states[state] || states.error;
  ['ws-dot','ws-dot-mob'].forEach(id => { const el = document.getElementById(id); if (el) el.className = cls; });
  ['ws-status','ws-status-mob'].forEach(id => { const el = document.getElementById(id); if (el) el.textContent = text; });
}

function _makeWs({ path, onMessage, retryRef, pingRef, retryTimerRef, onOpen }) {
  const state = { retry: retryRef.val, ping: null, retryTimer: null };

  function connect() {
    const ws = new WebSocket(_wsUrl(path));

    ws.onopen = () => {
      // First-frame auth — required by the server within 5 s of accept.
      try { ws.send(JSON.stringify({ auth: Auth.getToken() })); } catch {}
      retryRef.val = 0;
      clearInterval(pingRef.val);
      pingRef.val = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) ws.send('ping');
      }, 20000);
      if (onOpen) onOpen();
    };

    ws.onmessage = (ev) => {
      if (ev.data === 'pong') return;
      try { onMessage(JSON.parse(ev.data)); } catch (_) {}
    };

    ws.onclose = (ev) => {
      clearInterval(pingRef.val);
      if (ev.code === 4001) { return; }
      if (_Idle.shouldStayClosed()) return;  // idle-killed → stay closed
      const delay = Math.min(2000 * Math.pow(2, retryRef.val), 30000);
      retryRef.val++;
      clearTimeout(retryTimerRef.val);
      retryTimerRef.val = setTimeout(connect, delay);
    };

    ws.onerror = () => ws.close();
    return ws;
  }

  return connect;
}

// ── Throttle control (user-configurable refresh cadence) ─────────────────────
// Throttle mode: live|3|10|15|30. Old values (60 / pause) from before the
// mode-set trim get auto-migrated to their nearest neighbour so users don't
// end up stuck on a removed option.
let _throttle = (() => {
  // 3s default + 'live' option удалён из dropdown. Live режим пересортировал
  // 1000 строк на каждом WS-кадре (~500ms), пегая CPU. 3s feels real-time
  // и держит cost под контролем. Юзеры с сохранённым 'live' из старой
  // версии migrate'аются на '3'.
  const saved = localStorage.getItem('screener-throttle') || '3';
  const valid = new Set(['3', '10', '15', '30']);
  if (valid.has(saved)) return saved;
  // Legacy migration — 'live'/'pause'/'60' all collapse to '3' or '30'.
  if (saved === 'live') return '3';
  if (saved === 'pause' || saved === '60') return '30';
  return '3';
})();
let _pendingFunding = null;  // latest unrendered funding payload
let _pendingArb = null;      // latest unrendered arb payload
let _throttleTimer = null;

const THROTTLE_OPTS = [
  { mode: '3',     label: '3s',    sub: 'every 3s' },
  { mode: '10',    label: '10s',   sub: 'every 10s' },
  { mode: '15',    label: '15s',   sub: 'every 15s' },
  { mode: '30',    label: '30s',   sub: 'every 30s' },
];

function _throttleBtnLabel(mode) {
  return (THROTTLE_OPTS.find(o => o.mode === mode) || THROTTLE_OPTS[0]).label;
}

function renderThrottle(containerId) {
  const wrap = document.getElementById(containerId);
  if (!wrap) return;
  const curLbl = _throttleBtnLabel(_throttle);
  wrap.innerHTML = `
    <button class="throttle-btn" data-mode="${_throttle}" type="button">
      <span class="tr-ico"></span>
      <span class="tr-lbl">${curLbl}</span>
      <svg class="tr-chev" viewBox="0 0 10 6" fill="none">
        <path d="M1 1l4 4 4-4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
      </svg>
    </button>
    <div class="throttle-menu">
      ${THROTTLE_OPTS.map(o => `
        <div class="throttle-opt${o.mode === _throttle ? ' active' : ''}" data-mode="${o.mode}">
          <span class="opt-dot"></span>
          <span class="opt-lbl">${o.label}</span>
          <span class="opt-sub">${o.sub}</span>
          <svg class="opt-check" viewBox="0 0 12 12" fill="none">
            <path d="M2 6l3 3 5-6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </div>
      `).join('')}
    </div>
  `;
  const btn = wrap.querySelector('.throttle-btn');
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const was = wrap.classList.contains('open');
    document.querySelectorAll('.throttle.open').forEach(el => el.classList.remove('open'));
    btn.classList.remove('open');
    if (!was) { wrap.classList.add('open'); btn.classList.add('open'); }
  });
  wrap.querySelectorAll('.throttle-opt').forEach(opt => {
    opt.addEventListener('click', (e) => {
      e.stopPropagation();
      setThrottle(opt.dataset.mode);
      wrap.classList.remove('open');
      btn.classList.remove('open');
    });
  });
}

document.addEventListener('click', () => {
  document.querySelectorAll('.throttle.open').forEach(el => {
    el.classList.remove('open');
    const b = el.querySelector('.throttle-btn'); if (b) b.classList.remove('open');
  });
});

function setThrottle(mode) {
  // 'live' removed from THROTTLE_OPTS. Stale localStorage values fall
  // through to '3' via the loader at the top of this file.
  if (mode === 'live') mode = '3';
  _throttle = mode;
  localStorage.setItem('screener-throttle', mode);
  renderThrottle('throttle-desk');
  renderThrottle('throttle-mob');
  clearInterval(_throttleTimer); _throttleTimer = null;
  const ms = parseInt(mode) * 1000;
  _throttleTimer = setInterval(() => { if (document.hidden) return; flushPending(); }, ms);
}

// Map-keyed source of truth for funding rows — mirrors _arbRowsByKey so we
// can apply diff patches (added / updated / removed) in-place rather than
// re-rendering from scratch. Key = "exchange:symbol".
const _rowsByKey = new Map();

function _applyFundingPayload(d) {
  if (d && d.type === 'diff') {
    if (Array.isArray(d.added))   for (const r of d.added)   _rowsByKey.set(`${r.exchange}:${r.symbol}`, r);
    if (Array.isArray(d.updated)) for (const r of d.updated) _rowsByKey.set(`${r.exchange}:${r.symbol}`, r);
    if (Array.isArray(d.removed)) for (const k of d.removed) _rowsByKey.delete(`${k[0]}:${k[1]}`);
  } else {
    // snapshot OR legacy full-rows payload
    _rowsByKey.clear();
    for (const r of (d.rows || [])) {
      if (r && r.exchange && r.symbol) _rowsByKey.set(`${r.exchange}:${r.symbol}`, r);
    }
  }
  _rows = Array.from(_rowsByKey.values());
}

function flushPending() {
  let applied = false;
  if (_pendingFunding) {
    const d = _pendingFunding; _pendingFunding = null;
    _applyFundingPayload(d);
    renderStats({ rows: _rows, ts: d.ts });
    applyFilter(true);
    applied = true;
  }
  if (_pendingArb) {
    const d = _pendingArb; _pendingArb = null;
    _applyArbPayload(d);
    applyArb(true);
    if (_mode === 'arb' && d.ts) {
      const ts = new Date(d.ts * 1000).toLocaleTimeString();
      const bar = document.getElementById('stats-bar');
      if (bar) {
        const items = bar.querySelectorAll('.stat-item');
        if (items.length >= 3) items[2].innerHTML = `Updated at <b>${ts}</b>`;
      }
    }
    applied = true;
  }
  if (applied) {
    ['ws-dot','ws-dot-mob'].forEach(id => {
      const el = document.getElementById(id);
      if (!el || !el.classList.contains('live')) return;
      el.classList.remove('tick');
      void el.offsetWidth; // restart animation
      el.classList.add('tick');
    });
  }
}

// Apply pending on load (in case user reloaded with a saved throttle pref)
window.addEventListener('load', () => setThrottle(_throttle));

// Wake-up flush: when the tab becomes visible again, drain whatever WS frames
// accumulated while hidden. Without this the user sees stale numbers until
// the next throttle tick (up to 30s on the slowest throttle setting).
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) requestAnimationFrame(() => { try { flushPending(); } catch (_) {} });
});

// ── Funding REST poller ────────────────────────────────────────────────────────
// Replaced /ws/funding (which pushed a ~90 KB gzip full snapshot every 200 ms,
// burning ~27 MB/min on every page load regardless of which tab was open).
// Now: lazy 3s REST polling, active only while Funding Rates tab is visible
// or no other table has data yet (so the top stats bar can still show
// "X contracts / Y exchanges" once on first paint).
const FUNDING_POLL_MS = 3000;

async function _pollFunding(forceFirst = false) {
  try {
    const res = await Auth.apiFetch('/screener/funding');
    if (!res.ok) return;
    const data = await res.json();
    _pendingFunding = data;
    flushPending();
    setWsStatus('live');
  } catch (_) {
    if (forceFirst) setWsStatus('connecting');
  }
}

function _startFundingPoll() {
  if (_fundingPollTimer) return;
  _pollFunding(true);
  _fundingPollTimer = setInterval(() => {
    if (document.hidden) return;
    if (_mode !== 'funding' && _rows.length) return;  // stop polling once we have a snapshot and user moved away
    _pollFunding(false);
  }, FUNDING_POLL_MS);
}

function _stopFundingPoll() {
  if (_fundingPollTimer) { clearInterval(_fundingPollTimer); _fundingPollTimer = null; }
}

// Refresh button on the funding header retains its label — fires an
// immediate fetch and (re-)starts the poll loop if needed.
function reconnectWs() {
  _stopFundingPoll();
  _startFundingPoll();
}

function connectWs() { _startFundingPoll(); }

// /ws/book live In/Out updates removed — basis-only screener policy.
// Detail page (/arb) maintains its own /ws/book subscription independently.


// ── Arbitrage WebSocket ────────────────────────────────────────────────────────
const _retryA      = { val: 0 };
const _pingA       = { val: null };
const _retryTimerA = { val: null };

const _connectArb = _makeWs({
  path: 'arb',
  retryRef: _retryA, pingRef: _pingA, retryTimerRef: _retryTimerA,
  onMessage: (data) => {
    _pendingArb = data;
    // Live mode removed; flush always goes through setInterval (3-30s).
  },
});

// Register the arb WS + funding poller with the idle tracker so they
// pause when the tab is left unattended. Re-open hooks call connectWs()
// + _startFundingPoll() respectively when user returns.
_Idle.onWake({
  close: () => {
    if (_wsArb && _wsArb.readyState <= 1) try { _wsArb.close(4000, 'idle'); } catch (_) {}
    _stopFundingPoll();
  },
  open: () => {
    if (!_wsArb || _wsArb.readyState === WebSocket.CLOSED) _wsArb = _connectArb();
    if (_mode === 'funding') _startFundingPoll();
  },
});

async function loadArb() {
  _inOutFirstSorted.arb = false;  // fresh dataset → re-sort once on next live tick
  if (_arbRows.length) {
    applyArb();
  } else {
    document.getElementById('tbody-arb').innerHTML =
      '<tr><td colspan="10" class="empty-msg"><span class="spinner"></span>Computing arbitrage…</td></tr>';
    document.getElementById('cards-arb').innerHTML =
      '<div class="empty-msg"><span class="spinner"></span>Computing…</div>';
  }
  if (!_wsArb || _wsArb.readyState === WebSocket.CLOSED) {
    _wsArb = _connectArb();
  }
  // Always fetch fresh arb data when switching to this tab
  try {
    const res = await Auth.apiFetch('/screener/long-short');
    if (res.ok) {
      const data = await res.json();
      _applyArbPayload(data);
      applyArb(true);
    }
  } catch (_) {}
}

// ── funding filter + sort ──────────────────────────────────────────────────────
function applyFilter(keepPage = false) {
  const q = document.getElementById('search').value.trim().toUpperCase();
  const minApr = parseFloat(document.getElementById('f-min-apr').value) || 0;
  const minVol = parseFloat(document.getElementById('f-min-vol').value) || 0;
  _filtered = _rows.filter(r => {
    if (_exDisabled.has(r.exchange)) return false;
    if (_hiddenTokens.has(r.symbol)) return false;
    if (q && !r.symbol.includes(q)) return false;
    if (minApr > 0 && Math.abs(r.apr) < minApr) return false;
    if (minVol > 0 && (r.volume_usd || 0) < minVol) return false;
    if (_crossOnly && !r.cross_listed) return false;
    return true;
  });
  if (!keepPage) _pageF = 0;
  sortFRows(keepPage);
}

function sortBy(col) {
  if (_sortF.col === col) _sortF.asc = !_sortF.asc;
  else { _sortF.col = col; _sortF.asc = col === 'symbol' || col === 'exchange'; }
  document.querySelectorAll('#tbl-funding thead th').forEach(th => {
    const c = th.dataset.col;
    th.classList.toggle('sorted', c === _sortF.col);
    const a = th.querySelector('.sort-arrow');
    if (a) a.textContent = c === _sortF.col ? (_sortF.asc ? '↑' : '↓') : '↕';
  });
  sortFRows();
}

function sortFRows(keepPage = false) {
  const col = _sortF.col;
  _filtered.sort((a, b) => {
    let va = a[col], vb = b[col];
    if (col === 'symbol' || col === 'exchange') {
      va = String(va); vb = String(vb);
      return _sortF.asc ? va.localeCompare(vb) : vb.localeCompare(va);
    }
    if (col === 'apr') { va = Math.abs(va); vb = Math.abs(vb); }
    return _sortF.asc ? va - vb : vb - va;
  });
  if (!keepPage) _pageF = 0;
  renderFunding();
}

// ── arbitrage filter + sort ────────────────────────────────────────────────────
function setMinVol(v, btn){
  const desk = document.getElementById('f-min-vol');
  const mob  = document.getElementById('f-min-vol-mob');
  const val = v > 0 ? String(v) : '';
  if (desk) desk.value = val;
  if (mob)  mob.value  = val;
  document.querySelectorAll('.vol-chip').forEach(el => {
    el.classList.toggle('is-active', Number(el.dataset.v) === Number(v));
  });
  _reapplyCurrentMode();
}

function applyArb(keepPage = false) {
  const q = document.getElementById('search').value.trim().toUpperCase();
  const minNet   = parseFloat(document.getElementById('f-min-net').value)   || -Infinity;
  // Empty field must mean "no filter", not "ignore negative gross" — that
  // dropped every spread-driven opp (e.g. RAVE bybit, where funding goes
  // against the trade but the 4% price spread pays).
  const fgRaw = document.getElementById('f-min-gross').value;
  const minGross = fgRaw === '' ? -Infinity : (parseFloat(fgRaw) || -Infinity);
  // "Gross + Spread" — income before fees. Positive means either funding
  // or spread (or both) are on our side.
  const gsRaw = document.getElementById('f-min-gs').value;
  const minGS = gsRaw === '' ? -Infinity : (parseFloat(gsRaw) || -Infinity);
  const minVol   = parseFloat(document.getElementById('f-min-vol').value)   || 0;

  _filteredArb = _arbRows.filter(r => {
    if (_exDisabled.has(r.long_exchange) || _exDisabled.has(r.short_exchange)) return false;
    if (_hiddenTokens.has(r.symbol)) return false;
    if (q && !r.symbol.includes(q)) return false;
    if (r.net_profit < minNet)   return false;
    if (r.gross_funding < minGross) return false;
    if ((r.gross_funding + r.price_spread) < minGS) return false;
    if (minVol > 0) {
      const vMin = Math.min(r.long_volume || 0, r.short_volume || 0);
      if (vMin < minVol) return false;
    }
    // Always drop the reverse-direction leg (valid_price=false): rows
    // where buying long is at a HIGHER price than selling short — the
    // basis goes against the trade and the row is essentially noise.
    // Each opp is computed both ways by the backend; only the
    // positive-basis direction is actionable. Previously this was an
    // opt-in (_validOnly toggle); now mandatory for the screener.
    if (!r.valid_price) return false;
    // Hide rows whose In/Out has been positively determined as null
    // (the orderbook isn't available on one of the venues). Rows still
    // warming (in_pct === undefined — fetch hasn't returned yet) stay
    // visible with the "…" placeholder so first-paint isn't blank.
    // Once the next /in-out tick lands, this filter will pull them in
    // or out depending on whether data resolved.
    if (r.in_pct === null || r.out_pct === null) return false;
    return true;
  });
  if (!keepPage) _pageA = 0;
  sortARows(keepPage);
}

function sortArb(col) {
  if (_sortA.col === col) _sortA.asc = !_sortA.asc;
  else { _sortA.col = col; _sortA.asc = col === 'symbol' || col === 'long_exchange' || col === 'short_exchange'; }
  document.querySelectorAll('#tbl-arb thead th').forEach(th => {
    const c = th.dataset.acol;
    if (!c) return;
    th.classList.toggle('sorted', c === _sortA.col);
    const a = th.querySelector('.sort-arrow');
    if (a) a.textContent = c === _sortA.col ? (_sortA.asc ? '↑' : '↓') : '↕';
  });
  sortARows();
}

(function () {
  const loggedIn = Auth.isLoggedIn();

  const bnavAuth = document.getElementById('bnav-auth');

  if (!loggedIn && bnavAuth) {
    bnavAuth.href = '/login';
    bnavAuth.innerHTML = `
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
        <path d="M13 3h4a1 1 0 011 1v12a1 1 0 01-1 1h-4M8 14l4-4-4-4M2 10h10"
          stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
      </svg>
      Sign In
    `;
  }
})();

function sortARows(keepPage = false) {
  const col = _sortA.col;
  // For desc: nulls sink to -Infinity. For asc: nulls float to +Infinity.
  // Either way, rows without an in_pct (no live orderbook) end up at the
  // bottom so the top of the table is always the actionable rows.
  const NULL_DESC = -Infinity, NULL_ASC = Infinity;
  _filteredArb.sort((a, b) => {
    let va = a[col], vb = b[col];
    if (typeof va === 'string') return _sortA.asc ? va.localeCompare(vb) : vb.localeCompare(va);
    if (va == null) va = _sortA.asc ? NULL_ASC : NULL_DESC;
    if (vb == null) vb = _sortA.asc ? NULL_ASC : NULL_DESC;
    return _sortA.asc ? va - vb : vb - va;
  });
  if (!keepPage) _pageA = 0;
  renderArb();
}

// ── render stats ───────────────────────────────────────────────────────────────
function renderStats(data) {
  const total = (data.rows || []).length;
  const exCount = new Set((data.rows || []).map(r => r.exchange)).size;
  const ts = data.ts ? new Date(data.ts * 1000).toLocaleTimeString() : '—';
  const statsHtml = `
    <div class="stat-item"><b>${total}</b> contracts tracked</div>
    <div class="stat-item"><b>${exCount}</b> exchanges</div>
    <div class="stat-item">Updated at <b>${ts}</b></div>
  `;
  const statsMobHtml = `
    <div class="stat-item"><b>${total}</b> contracts</div>
    <div class="stat-item">at <b>${ts}</b></div>
  `;
  document.getElementById('stats-bar').innerHTML = statsHtml;
  const mob = document.getElementById('stats-bar-mob'); if (mob) mob.innerHTML = statsMobHtml;
  // sync ws status text into stats bar on mobile (dot/button stay in desktop block)
  const lbl = document.getElementById('ws-status'); if (lbl) lbl.textContent = lbl.textContent;
}

// ── format helpers ─────────────────────────────────────────────────────────────
// Delegated to /formatters.js (window.FMT). The module exports the same
// helpers used inline here; keeping these names as thin aliases avoids
// rewriting ~80 callsites in this file.
const esc          = window.FMT.esc;
const fmtPrice     = window.FMT.price;
const fmtRate      = window.FMT.rate;
const fmtApr       = window.FMT.apr;
const fmtCountdown = window.FMT.countdown;
const fmtVol       = window.FMT.volume;
const fmtPct       = window.FMT.pct;

// Render an In/Out cell value baked into the row by the Go arb
// compute (futures.go / spot.go / dex.go). Backend returns null when
// the orderbook isn't subscribed yet — those rows are filtered out
// upstream so we shouldn't normally hit the null branch here, but
// keep a graceful fallback just in case.
function _fmtIO(v) {
  if (v == null || typeof v !== 'number') return '<span class="io-na">—</span>';
  const sign = v >= 0 ? '+' : '';
  const cls  = v > 0 ? 'rate-pos' : v < 0 ? 'rate-neg' : 'rate-zero';
  return `<span class="${cls}" style="font-family:var(--mono);font-size:11px">${sign}${v.toFixed(4)}%</span>`;
}

function fmtExRate(ex, rate_pct, symbol) {
  const sign = rate_pct >= 0 ? '+' : '';
  const cls  = rate_pct > 0 ? 'rate-pos' : rate_pct < 0 ? 'rate-neg' : 'rate-zero';
  return `${exBadge(ex, symbol)}
          <span class="td-rate ${cls}" style="font-size:12px">${sign}${rate_pct.toFixed(4)}%</span>
          <span style="font-size:10px;color:var(--text3)">/8h</span>`;
}

// ── pager helper ───────────────────────────────────────────────────────────────
const _pgSizes = [10, 15, 25, 50];
function _pgSizeBtns() {
  return `<div class="pg-size-wrap" style="display:flex;gap:4px;align-items:center">
    <span style="font-size:11px;color:var(--text3);margin-right:2px">Rows:</span>
    ${_pgSizes.map(n => `<button class="pager-btn${PAGE_SIZE===n?' pager-btn-active':''}" style="padding:4px 9px;font-size:12px" onclick="setPageSize(${n})">${n}</button>`).join('')}
  </div>`;
}

function renderPager(id, page, total, goFn) {
  const el = document.getElementById(id);
  if (!el) return;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const nav = pages > 1 ? `
    <button class="pager-btn" onclick="${goFn}(${page - 1})" ${page === 0 ? 'disabled' : ''}>← Prev</button>
    <span class="pager-info">${page + 1} / ${pages}</span>
    <button class="pager-btn" onclick="${goFn}(${page + 1})" ${page >= pages - 1 ? 'disabled' : ''}>Next →</button>` : '';
  el.innerHTML = `<div style="display:flex;align-items:center;justify-content:space-between;width:100%;gap:12px">
    <div style="display:flex;align-items:center;gap:8px">${nav}</div>
    ${_pgSizeBtns()}
  </div>`;
}

function goPageF(n) {
  const pages = Math.ceil(_filtered.length / PAGE_SIZE);
  _pageF = Math.max(0, Math.min(n, pages - 1));
  renderFunding();
}
function goPageA(n) {
  const pages = Math.ceil(_filteredArb.length / PAGE_SIZE);
  _pageA = Math.max(0, Math.min(n, pages - 1));
  renderArb();
}

// ── render funding ─────────────────────────────────────────────────────────────
function renderFunding() {
  const tbody = document.getElementById('tbody-funding');
  const start = _pageF * PAGE_SIZE;
  const page  = _filtered.slice(start, start + PAGE_SIZE);
  if (!_filtered.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty-msg">No contracts match your filter</td></tr>';
  } else {
    tbody.innerHTML = page.map(r => `
      <tr>
        <td class="td-symbol">${symbolLink(r.symbol, r.exchange)}</td>
        <td>${exBadge(r.exchange, r.symbol)}</td>
        <td class="td-price">${fmtPrice(r.price)}</td>
        <td>${fmtRate(r.rate, r.interval_h)}</td>
        <td>${fmtApr(r.apr)}</td>
        <td class="td-vol" style="font-family:var(--mono);font-size:12px;color:var(--text2)">${fmtVol(r.volume_usd)}</td>
        <td class="td-next">${fmtCountdown(r.next_ts)}</td>
      </tr>
    `).join('');
  }
  renderPager('pager-funding', _pageF, _filtered.length, 'goPageF');
  renderFundingCards();
}

// ── render arbitrage ───────────────────────────────────────────────────────────
function _arbRowCells(r) {
  const netCls   = r.net_profit > 0 ? 'net-pos' : 'net-neg';
  const netSign  = r.net_profit >= 0 ? '+' : '';
  const grossSign = r.gross_funding >= 0 ? '+' : '';
  const validBadge = r.valid_price
    ? '<span class="badge-valid">✓ valid</span>'
    : '<span class="badge-invalid">⚠ spread</span>';
  const lhealth = _exHealth[r.long_exchange] || {};
  const shealth = _exHealth[r.short_exchange] || {};
  const ldot = `<span class="ex-status-dot ${lhealth.klass || ''}" title="${r.long_exchange} · ${lhealth.age_s != null ? lhealth.age_s.toFixed(1)+'s' : 'no data'}"></span>`;
  const sdot = `<span class="ex-status-dot ${shealth.klass || ''}" title="${r.short_exchange} · ${shealth.age_s != null ? shealth.age_s.toFixed(1)+'s' : 'no data'}"></span>`;
  const lsDetailUrl = `/arb?type=long-short&symbol=${esc(r.symbol)}&long=${esc(r.long_exchange)}&short=${esc(r.short_exchange)}`;
  return `
    <td class="td-symbol"><a href="${lsDetailUrl}" target="_blank" onclick="event.stopPropagation()" style="color:inherit;text-decoration:none;border-bottom:1px dotted var(--text3)">${esc(r.symbol)}</a></td>
    <td data-cell="long" data-v="">
      <div class="arb-pair"><div class="arb-ex-rate"><span class="arb-label">long</span>${fmtExRate(r.long_exchange, r.long_rate, r.symbol)}${ldot}</div>
      <div style="font-size:11px;color:var(--text3);margin-left:36px">${fmtPrice(r.long_price)} · <span style="font-family:var(--mono)">${fmtVol(r.long_volume)}</span></div></div>
    </td>
    <td data-cell="short" data-v="">
      <div class="arb-pair"><div class="arb-ex-rate"><span class="arb-label">short</span>${fmtExRate(r.short_exchange, r.short_rate, r.symbol)}${sdot}</div>
      <div style="font-size:11px;color:var(--text3);margin-left:36px">${fmtPrice(r.short_price)} · <span style="font-family:var(--mono)">${fmtVol(r.short_volume)}</span></div></div>
    </td>
    <td class="td-inout" data-cell="in"  data-v="">${_fmtIO(r.in_pct)}</td>
    <td class="td-inout" data-cell="out" data-v="">${_fmtIO(r.out_pct)}</td>
    <td class="td-gross" data-cell="gross" data-v="">${grossSign}${r.gross_funding.toFixed(4)}%<br><span style="font-size:10px;color:var(--text3);font-weight:400">per 8h</span></td>
    <td class="td-fees"><span title="long: ${r.fee_long}% + short: ${r.fee_short}%">−${r.total_fees.toFixed(4)}%</span></td>
    <td data-cell="net"><span class="td-net ${netCls}" data-v="${r.net_profit}">${netSign}${r.net_profit.toFixed(4)}%</span></td>
    <td class="td-status">${validBadge}</td>
    <td style="display:flex;gap:4px;align-items:center">
      ${_starBtn(r.symbol, r.long_exchange, r.short_exchange)}
      <a href="/arb?symbol=${esc(r.symbol)}&long=${esc(r.long_exchange)}&short=${esc(r.short_exchange)}" target="_blank" class="arb-detail-btn" title="Open detail page" onmouseenter="_arbPrefetch(this.href)" onclick="event.stopPropagation()">↗</a>
    </td>`;
}

function _makeArbTr(r) {
  const lsDetailUrl = `/arb?type=long-short&symbol=${esc(r.symbol)}&long=${esc(r.long_exchange)}&short=${esc(r.short_exchange)}`;
  const tr = document.createElement('tr');
  tr.dataset.rowKey = _arbKey(r);
  tr.dataset.longEx  = r.long_exchange;
  tr.dataset.shortEx = r.short_exchange;
  tr.dataset.symbol  = r.symbol;
  tr.style.cursor = 'pointer';
  tr.onclick = () => window.open(lsDetailUrl, '_blank');
  tr.innerHTML = _arbRowCells(r);
  return tr;
}

function _patchArbTr(tr, r) {
  const lhealth = _exHealth[r.long_exchange] || {};
  const shealth = _exHealth[r.short_exchange] || {};
  const ldot = `<span class="ex-status-dot ${lhealth.klass || ''}" title="${r.long_exchange} · ${lhealth.age_s != null ? lhealth.age_s.toFixed(1)+'s' : 'no data'}"></span>`;
  const sdot = `<span class="ex-status-dot ${shealth.klass || ''}" title="${r.short_exchange} · ${shealth.age_s != null ? shealth.age_s.toFixed(1)+'s' : 'no data'}"></span>`;

  function setText(cell, html, key) {
    if (!cell || cell.dataset.v === key) return;
    cell.innerHTML = html;
    cell.dataset.v = key;
  }

  const longKey = `${r.long_rate}|${r.long_price}|${r.long_volume}|${lhealth.klass || ''}`;
  setText(tr.querySelector('[data-cell="long"]'),
    `<div class="arb-pair"><div class="arb-ex-rate"><span class="arb-label">long</span>${fmtExRate(r.long_exchange, r.long_rate, r.symbol)}${ldot}</div><div style="font-size:11px;color:var(--text3);margin-left:36px">${fmtPrice(r.long_price)} · <span style="font-family:var(--mono)">${fmtVol(r.long_volume)}</span></div></div>`,
    longKey);

  const shortKey = `${r.short_rate}|${r.short_price}|${r.short_volume}|${shealth.klass || ''}`;
  setText(tr.querySelector('[data-cell="short"]'),
    `<div class="arb-pair"><div class="arb-ex-rate"><span class="arb-label">short</span>${fmtExRate(r.short_exchange, r.short_rate, r.symbol)}${sdot}</div><div style="font-size:11px;color:var(--text3);margin-left:36px">${fmtPrice(r.short_price)} · <span style="font-family:var(--mono)">${fmtVol(r.short_volume)}</span></div></div>`,
    shortKey);

  const inHtml = _fmtIO(r.in_pct);
  setText(tr.querySelector('[data-cell="in"]'), inHtml, inHtml);

  const outHtml = _fmtIO(r.out_pct);
  setText(tr.querySelector('[data-cell="out"]'), outHtml, outHtml);

  const grossVal = r.gross_funding.toFixed(4);
  setText(tr.querySelector('[data-cell="gross"]'),
    `${r.gross_funding >= 0 ? '+' : ''}${grossVal}%<br><span style="font-size:10px;color:var(--text3);font-weight:400">per 8h</span>`,
    grossVal);

  const netCell = tr.querySelector('[data-cell="net"]');
  if (netCell) {
    const span = netCell.querySelector('.td-net');
    if (span) {
      const netVal = r.net_profit.toFixed(4);
      if (span.dataset.v !== netVal) {
        const prev = parseFloat(span.dataset.v);
        span.dataset.v = netVal;
        span.textContent = (r.net_profit >= 0 ? '+' : '') + netVal + '%';
        span.className = `td-net ${r.net_profit > 0 ? 'net-pos' : 'net-neg'}`;
        if (!isNaN(prev)) {
          span.classList.remove('cell-flash-up', 'cell-flash-down');
          void span.offsetWidth;
          span.classList.add(r.net_profit > prev ? 'cell-flash-up' : 'cell-flash-down');
        }
      }
    }
  }
}

function renderArb() {
  const tbody = document.getElementById('tbody-arb');
  const start = _pageA * PAGE_SIZE;
  const page  = _filteredArb.slice(start, start + PAGE_SIZE);
  if (!_filteredArb.length) {
    tbody.innerHTML = _emptyRow({
      kind: 'empty',
      title: _arbRows.length ? 'No opportunities match your filter' : 'No Long/Short data yet',
      sub: _arbRows.length
        ? 'Try widening the spread or fee range above.'
        : 'Long/Short arb recomputes every 200ms — first paint within seconds.',
      colspan: 10,
    });
    renderPager('pager-arb', _pageA, _filteredArb.length, 'goPageA');
    renderArbCards();
    return;
  }
  // Keyed reconciliation — only add/remove/move rows that actually changed position,
  // patch cells in-place for everything else. Eliminates full-tbody innerHTML thrash.
  // Remove any non-keyed rows first (spinner/empty-state rows from loadArb) so they
  // don't linger below the newly inserted data rows.
  for (const tr of [...tbody.querySelectorAll('tr:not([data-row-key])')]) tr.remove();
  const byKey = new Map();
  for (const tr of tbody.querySelectorAll('tr[data-row-key]')) byKey.set(tr.dataset.rowKey, tr);
  const wanted = new Set(page.map(r => _arbKey(r)));
  for (const [k, tr] of byKey) { if (!wanted.has(k)) tr.remove(); }
  for (let i = 0; i < page.length; i++) {
    const r   = page[i];
    const key = _arbKey(r);
    let tr = byKey.get(key);
    if (!tr) {
      tr = _makeArbTr(r);
      tr.classList.add('arb-row-enter');
    } else {
      _patchArbTr(tr, r);
    }
    const ref = tbody.children[i];
    if (ref !== tr) tbody.insertBefore(tr, ref || null);
  }
  renderPager('pager-arb', _pageA, _filteredArb.length, 'goPageA');
  renderArbCards();
  // in/out comes baked into row data — no fetch needed.
}

// ── mobile sort bar ───────────────────────────────────────────────────────────
const SORT_CHIPS_F = [
  { col: 'apr',        label: 'APR' },
  { col: 'rate',       label: 'Rate' },
  { col: 'volume_usd', label: 'Volume' },
  { col: 'symbol',     label: 'Token' },
  { col: 'exchange',   label: 'Exchange' },
  { col: 'price',    label: 'Price' },
  { col: 'next_ts',  label: 'Next' },
];
const SORT_CHIPS_A = [
  { col: 'in_pct',        label: 'In' },
  { col: 'out_pct',       label: 'Out' },
  { col: 'net_profit',    label: 'Net profit' },
  { col: 'net_apr',       label: 'Net APR' },
  { col: 'gross_funding', label: 'Gross' },
  { col: 'symbol',        label: 'Token' },
  { col: 'total_fees',    label: 'Fees' },
];

function buildSortChips() {
  const wf = document.getElementById('sort-chips-funding');
  const wa = document.getElementById('sort-chips-arb');
  wf.innerHTML = SORT_CHIPS_F.map(c => `
    <button class="sort-chip${_sortF.col === c.col ? ' active' : ''}"
            id="sc-f-${c.col}" onclick="mobileSortF('${c.col}')">
      ${c.label}<span class="sc-arr" id="sc-f-arr-${c.col}">${_sortF.col === c.col ? (_sortF.asc ? '↑' : '↓') : ''}</span>
    </button>`).join('');
  wa.innerHTML = SORT_CHIPS_A.map(c => `
    <button class="sort-chip${_sortA.col === c.col ? ' active' : ''}"
            id="sc-a-${c.col}" onclick="mobileSortA('${c.col}')">
      ${c.label}<span class="sc-arr" id="sc-a-arr-${c.col}">${_sortA.col === c.col ? (_sortA.asc ? '↑' : '↓') : ''}</span>
    </button>`).join('');
}

function mobileSortF(col) {
  if (_sortF.col === col) _sortF.asc = !_sortF.asc;
  else { _sortF.col = col; _sortF.asc = col === 'symbol' || col === 'exchange'; }
  // sync desktop headers
  document.querySelectorAll('#tbl-funding thead th').forEach(th => {
    const c = th.dataset.col;
    th.classList.toggle('sorted', c === _sortF.col);
    const a = th.querySelector('.sort-arrow');
    if (a) a.textContent = c === _sortF.col ? (_sortF.asc ? '↑' : '↓') : '↕';
  });
  // update chips
  SORT_CHIPS_F.forEach(c => {
    const chip = document.getElementById(`sc-f-${c.col}`);
    const arr  = document.getElementById(`sc-f-arr-${c.col}`);
    if (!chip) return;
    chip.classList.toggle('active', c.col === _sortF.col);
    arr.textContent = c.col === _sortF.col ? (_sortF.asc ? '↑' : '↓') : '';
  });
  sortFRows();
}

function mobileSortA(col) {
  if (_sortA.col === col) _sortA.asc = !_sortA.asc;
  else { _sortA.col = col; _sortA.asc = col === 'symbol' || col === 'long_exchange' || col === 'short_exchange'; }
  // sync desktop headers
  document.querySelectorAll('#tbl-arb thead th').forEach(th => {
    const c = th.dataset.acol;
    if (!c) return;
    th.classList.toggle('sorted', c === _sortA.col);
    const a = th.querySelector('.sort-arrow');
    if (a) a.textContent = c === _sortA.col ? (_sortA.asc ? '↑' : '↓') : '↕';
  });
  // update chips
  SORT_CHIPS_A.forEach(c => {
    const chip = document.getElementById(`sc-a-${c.col}`);
    const arr  = document.getElementById(`sc-a-arr-${c.col}`);
    if (!chip) return;
    chip.classList.toggle('active', c.col === _sortA.col);
    arr.textContent = c.col === _sortA.col ? (_sortA.asc ? '↑' : '↓') : '';
  });
  sortARows();
}

// ── mobile cards ──────────────────────────────────────────────────────────────
function setPageSize(n) {
  PAGE_SIZE = n;
  _pageF = 0; _pageA = 0;
  _mode === 'funding' ? renderFunding() : renderArb();
}

function toggleCard(el) {
  const wasOpen = el.classList.contains('open');
  el.classList.toggle('open');
  const key = el.dataset.key;
  if (!key) return;
  if (el.closest('#cards-funding')) {
    _openFundingKey = wasOpen ? null : key;
  } else {
    _openArbKey = wasOpen ? null : key;
  }
}

function renderFundingCards() {
  const wrap = document.getElementById('cards-funding');
  if (!_filtered.length) {
    wrap.innerHTML = '<div class="empty-msg">No contracts match your filter</div>';
    return;
  }
  const start = _pageF * PAGE_SIZE;
  const page  = _filtered.slice(start, start + PAGE_SIZE);
  wrap.innerHTML = page.map((r, i) => {
    const absI = start + i;
    const aprSign = r.apr >= 0 ? '+' : '';
    const aprCls  = r.apr > 0 ? 'rate-pos' : r.apr < 0 ? 'rate-neg' : 'rate-zero';
    const ratePct = (r.rate * 100).toFixed(4);
    const rateSign = r.rate >= 0 ? '+' : '';
    const rateCls  = r.rate > 0 ? 'rate-pos' : r.rate < 0 ? 'rate-neg' : 'rate-zero';
    const intLbl = r.interval_h === 1 ? '1h' : `${r.interval_h}h`;
    const key = `${r.symbol}|${r.exchange}`;
    const isOpen = _openFundingKey === key;
    return `
    <div class="card${isOpen?' open':''}" onclick="toggleCard(this)" data-fi="${absI}" data-key="${key}">
      <div class="card-head">
        ${symbolLink(r.symbol, r.exchange)}
        <div class="card-badges">
          ${exBadge(r.exchange, r.symbol)}
        </div>
        <div class="card-right">
          <span class="card-apr ${aprCls}">${aprSign}${Math.abs(r.apr).toFixed(2)}% APR</span>
          <span class="card-next" id="cn-f-${absI}">${fmtCountdown(r.next_ts)}</span>
        </div>
        <span class="card-chevron">▼</span>
      </div>
      <div class="card-body">
        <div class="card-row">
          <span class="card-lbl">Price</span>
          <span class="card-val">${fmtPrice(r.price)}</span>
        </div>
        <div class="card-row">
          <span class="card-lbl">Funding / ${intLbl}</span>
          <span class="card-val ${rateCls}">${rateSign}${ratePct}%</span>
        </div>
        <div class="card-row">
          <span class="card-lbl">Annual APR</span>
          <span class="card-val ${aprCls}">${aprSign}${Math.abs(r.apr).toFixed(2)}%</span>
        </div>
        <div class="card-row">
          <span class="card-lbl">Volume 24h</span>
          <span class="card-val">${fmtVol(r.volume_usd)}</span>
        </div>
        <div class="card-row">
          <span class="card-lbl">Next funding</span>
          <span class="card-val" id="cn-fb-${absI}">${fmtCountdown(r.next_ts)}</span>
        </div>
      </div>
    </div>`;
  }).join('');
}

function renderArbCards() {
  const wrap = document.getElementById('cards-arb');
  if (!_filteredArb.length) {
    wrap.innerHTML = `<div class="empty-msg-card"><div class="empty-spinner"></div><div class="empty-title">${_arbRows.length ? 'No matches' : 'No Long/Short data yet'}</div><div class="empty-sub">${_arbRows.length ? 'Adjust filters above.' : 'First paint within seconds.'}</div></div>`;
    return;
  }
  const start = _pageA * PAGE_SIZE;
  const page  = _filteredArb.slice(start, start + PAGE_SIZE);
  wrap.innerHTML = page.map((r, i) => {
    const netCls   = r.net_profit > 0 ? 'net-pos' : 'net-neg';
    const netSign  = r.net_profit >= 0 ? '+' : '';
    const grossSign = r.gross_funding >= 0 ? '+' : '';
    const longRateCls  = r.long_rate  > 0 ? 'rate-pos' : r.long_rate  < 0 ? 'rate-neg' : 'rate-zero';
    const shortRateCls = r.short_rate > 0 ? 'rate-pos' : r.short_rate < 0 ? 'rate-neg' : 'rate-zero';
    const validBadge = r.valid_price
      ? '<span class="badge-valid">✓ valid</span>'
      : '<span class="badge-invalid">⚠ spread</span>';
    const aprCls = r.net_apr > 0 ? 'rate-pos' : r.net_apr < 0 ? 'rate-neg' : 'rate-zero';
    const aprSign = r.net_apr >= 0 ? '+' : '';
    const key = `${r.symbol}|${r.long_exchange}|${r.short_exchange}`;
    const isOpen = _openArbKey === key;
    return `
    <div class="card${isOpen?' open':''}" onclick="toggleCard(this)" data-key="${key}">
      <div class="card-head">
        ${symbolLink(r.symbol, r.long_exchange)}
        <div class="card-badges">
          ${exBadge(r.long_exchange, r.symbol)}
          <span style="color:var(--text3);font-size:11px">→</span>
          ${exBadge(r.short_exchange, r.symbol)}
        </div>
        <div class="card-right">
          <span class="card-net ${netCls}">${netSign}${r.net_profit.toFixed(4)}%</span>
          <span style="font-size:10px;color:var(--text3);font-family:var(--mono)">net / 8h</span>
        </div>
        <span class="card-chevron">▼</span>
      </div>
      <div class="card-body">
        <div class="card-row">
          <span class="card-lbl">Long</span>
          <div class="card-exrow">
            <div class="card-exrow-item">
              ${exBadge(r.long_exchange, r.symbol)}
              <span class="card-val ${longRateCls}">${r.long_rate >= 0 ? '+' : ''}${r.long_rate.toFixed(4)}%</span>
            </div>
            <div style="font-size:11px;color:var(--text3)">${fmtPrice(r.long_price)} · ${fmtVol(r.long_volume)}</div>
          </div>
        </div>
        <div class="card-row">
          <span class="card-lbl">Short</span>
          <div class="card-exrow">
            <div class="card-exrow-item">
              ${exBadge(r.short_exchange, r.symbol)}
              <span class="card-val ${shortRateCls}">${r.short_rate >= 0 ? '+' : ''}${r.short_rate.toFixed(4)}%</span>
            </div>
            <div style="font-size:11px;color:var(--text3)">${fmtPrice(r.short_price)} · ${fmtVol(r.short_volume)}</div>
          </div>
        </div>
        <div class="card-row">
          <span class="card-lbl">Funding / 8h</span>
          <span class="card-val td-gross">${grossSign}${r.gross_funding.toFixed(4)}%</span>
        </div>
        <div class="card-row">
          <span class="card-lbl">Fees</span>
          <span class="card-val" style="color:var(--text3)">−${r.total_fees.toFixed(4)}%</span>
        </div>
        <div class="card-row">
          <span class="card-lbl">Net APR</span>
          <span class="card-val ${aprCls}">${aprSign}${r.net_apr.toFixed(2)}%</span>
        </div>
        <div class="card-row">
          <span class="card-lbl">Status</span>
          <span>${validBadge}</span>
        </div>
        <div style="margin-top:12px">
          <a href="/arb?symbol=${esc(r.symbol)}&long=${esc(r.long_exchange)}&short=${esc(r.short_exchange)}" target="_blank" onclick="event.stopPropagation()" style="display:inline-flex;align-items:center;gap:6px;padding:7px 14px;border-radius:8px;background:var(--surface3);color:var(--text2);font-size:12px;font-weight:600;text-decoration:none;transition:background .15s,color .15s;" onmouseover="this.style.color='var(--green)'" onmouseout="this.style.color='var(--text2)'">
            ↗ Open detail
          </a>
        </div>
      </div>
    </div>`;
  }).join('');
}

// ── tick countdowns ────────────────────────────────────────────────────────────
function tickCountdowns() {
  if (_mode === 'funding') {
    const start = _pageF * PAGE_SIZE;
    document.querySelectorAll('.td-next').forEach((td, i) => {
      const r = _filtered[start + i];
      if (r) td.innerHTML = fmtCountdown(r.next_ts);
    });
    const end = Math.min(start + PAGE_SIZE, _filtered.length);
    for (let i = start; i < end; i++) {
      const r = _filtered[i];
      const t = fmtCountdown(r.next_ts);
      const a = document.getElementById(`cn-f-${i}`);
      const b = document.getElementById(`cn-fb-${i}`);
      if (a) a.innerHTML = t;
      if (b) b.innerHTML = t;
    }
  }
}

// ── avatar ────────────────────────────────────────────────────────────────────
(async () => {
  // Anonymous viewers don't have a /auth/me to fetch — skip the call so
  // the (now non-redirecting) 401 doesn't hit the network at all.
  if (!Auth.isLoggedIn()) return;
  try {
    const me = await Auth.apiFetch('/auth/me').then(r => r.json());
    const el = document.getElementById('nav-avatar');
    if (el && me.username) el.textContent = me.username[0].toUpperCase();
  } catch {}
})();

// ── bootstrap ─────────────────────────────────────────────────────────────────
// Force mode from URL even on bfcache restore
switchMode(_mode);
window.addEventListener('pageshow', (e) => { if (e.persisted) switchMode(_mode); });
buildExDrop();
buildMobExChips();
buildSortChips();
renderHiddenChips();
startExchangeHealthPoll();
document.getElementById('search').addEventListener('input', () => { _reapplyCurrentMode(); });
document.getElementById('f-min-apr').addEventListener('input', () => applyFilter());
document.getElementById('f-min-net').addEventListener('input', () => _reapplyCurrentMode());
document.getElementById('f-min-gross').addEventListener('input', () => _reapplyCurrentMode());
document.getElementById('f-min-vol')?.addEventListener('input', () => _reapplyCurrentMode());
// Mark "Any" chip active on load
document.querySelectorAll('.vol-chip[data-v="0"]').forEach(el => el.classList.add('is-active'));

// Init cross-only dot state
['f-cross-dot','f-cross-dot-mob'].forEach(id => { const el = document.getElementById(id); if (el) el.style.opacity = _crossOnly ? '1' : '0.4'; });

// Panel toggle initial position (legacy; panel is hidden in new layout)
const _pt = document.getElementById('panel-toggle'); if (_pt) _pt.style.left = '260px';

setInterval(tickCountdowns, 1000);

// ── Live In/Out columns ─────────────────────────────────────────────────────
// Fetches top-of-book entry/exit basis for currently visible screener rows.
// Throttled per-mode: only the active section is queried; cap 64 rows/cycle.
// Backend endpoint /screener/in-out batches the orderbook calls.
const _inOutCache = new Map();   // key → {in, out, ts}
let _inOutTimer = null;
// Per-mode rotating offset so refreshInOut walks all `_arbRows`/spot/dex
// keys over multiple ticks, not just the same first 256 forever.
const _inOutRotateOffset = { 'tbody-arb': 0, 'tbody-spot': 0, 'tbody-dex': 0 };

function _activeInOutSection() {
  if (_mode === 'arb')  return 'tbody-arb';
  if (_mode === 'spot') return 'tbody-spot';
  if (_mode === 'dex')  return 'tbody-dex';
  return null;
}

function _applyInOutCells(payload) {
  const tbId = _activeInOutSection();
  if (!tbId) return;
  const tbody = document.getElementById(tbId);
  if (!tbody) return;
  for (const td of tbody.querySelectorAll('td.td-inout[data-io-key]')) {
    const key = td.getAttribute('data-io-key');
    const slot = payload[key];
    if (!slot) continue;
    const which = td.getAttribute('data-io');
    const v = slot[which];
    if (v == null) {
      td.innerHTML = '<span class="io-na">—</span>';
      continue;
    }
    const sign = v >= 0 ? '+' : '';
    const cls  = v > 0 ? 'rate-pos' : v < 0 ? 'rate-neg' : 'rate-zero';
    td.innerHTML = `<span class="${cls}" style="font-family:var(--mono);font-size:11px">${sign}${v.toFixed(4)}%</span>`;
  }
}

// Augment the underlying row arrays with the latest in/out so the sort-
// comparator sees them. Re-sorts the active table only if its current
// sort-col is one of in_pct/out_pct — otherwise we only update the cells.
function _writeInOutOntoRows(payload) {
  // Build sym|long|short → values map for fast lookup.
  // Key shape: type:SYM:longEx:shortEx — e.g. "futures:BTC:binance:bybit".
  const matchArb = _mode === 'arb';
  const matchSpot = _mode === 'spot';
  const matchDex = _mode === 'dex';
  let arr = null;
  let typPrefix = '';
  if (matchArb)  { arr = _arbRows;  typPrefix = 'futures:'; }
  if (matchSpot) { arr = _spotRows; typPrefix = 'spot:';    }
  if (matchDex)  { arr = _dexRows;  typPrefix = 'dex:';     }
  if (!arr) return;
  for (const r of arr) {
    let key;
    if (matchArb)  key = `${typPrefix}${r.symbol}:${r.long_exchange}:${r.short_exchange}`;
    if (matchSpot) key = `${typPrefix}${r.symbol}:${r.spot_exchange}:${r.short_exchange}`;
    if (matchDex)  key = `${typPrefix}${r.symbol}:dex:${r.short_exchange}`;
    const slot = payload[key];
    if (!slot) continue;
    r.in_pct  = slot.in;
    r.out_pct = slot.out;
  }
  // Re-apply the filter so rows whose in/out just resolved enter the
  // visible set, and rows whose values went null drop out. keepPage=true
  // so the user doesn't get yanked back to page 1 every 3s.
  if (matchArb)       applyArb(true);
  else if (matchSpot) applySpot(true);
  else if (matchDex)  applyDex(true);
}

async function refreshInOut() {
  const tbId = _activeInOutSection();
  if (!tbId) return;

  // Collect keys from ALL rows in the active mode's data array — not
  // just visible — so the user-touch path warms books for the full
  // top-N tracked set. Rows without in/out get filtered out of the
  // displayed table by applyArb/applySpot/applyDex, but still need
  // their books warmed so they can become visible in subsequent ticks.
  let allKeys = [];
  if (tbId === 'tbody-arb') {
    allKeys = _arbRows.map(r => `futures:${r.symbol}:${r.long_exchange}:${r.short_exchange}`);
  } else if (tbId === 'tbody-spot') {
    allKeys = _spotRows.map(r => `spot:${r.symbol}:${r.spot_exchange}:${r.short_exchange}`);
  } else if (tbId === 'tbody-dex') {
    allKeys = _dexRows.map(r => `dex:${r.symbol}:dex:${r.short_exchange}`);
  }
  if (!allKeys.length) return;
  // Dedupe.
  const dedup = Array.from(new Set(allKeys));
  // Rotate through the full set in chunks of 256, advancing the
  // offset each tick. With 1000 rows / 256 per call / 3s tick the
  // full set is covered in ~12s, fast enough for the user to see
  // In/Out filling in continuously after page load.
  const off = _inOutRotateOffset[tbId] || 0;
  const chunk = 256;
  let items;
  if (dedup.length <= chunk) {
    items = dedup;
  } else {
    items = [];
    for (let i = 0; i < chunk; i++) {
      items.push(dedup[(off + i) % dedup.length]);
    }
    _inOutRotateOffset[tbId] = (off + chunk) % dedup.length;
  }
  // Apply cached values immediately so paging-back doesn't show "…".
  const cached = {};
  const fresh = [];
  const now = Date.now();
  for (const k of items) {
    const c = _inOutCache.get(k);
    if (c && now - c.ts < 6000) cached[k] = c;
    else fresh.push(k);
  }
  if (Object.keys(cached).length) {
    _applyInOutCells(cached);
    _writeInOutOntoRows(cached);
  }
  if (!fresh.length) return;
  try {
    // POST instead of GET — query string with 256 keys × ~40 chars each
    // pushes past nginx's ~8 KB URL limit and returns 414. JSON body
    // has no such cap.
    const r = await Auth.apiFetch('/screener/in-out', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items: fresh }),
    });
    if (!r.ok) return;
    const j = await r.json();
    for (const k of Object.keys(j)) {
      _inOutCache.set(k, { in: j[k].in, out: j[k].out, ts: Date.now() });
    }
    _applyInOutCells(j);
    _writeInOutOntoRows(j);
    _pingInOutPulse();
  } catch {}
}

// Tiny visual "fresh tick" pulse on the In column header — accompanies the
// 3s cadence so the user sees the data is live without spamming a counter.
function _pingInOutPulse() {
  for (const el of document.querySelectorAll('.io-fresh-pulse')) {
    el.classList.remove('on');
    // force reflow → re-trigger the keyframe
    void el.offsetWidth;
    el.classList.add('on');
  }
}

// /screener/in-out polling retired: in_pct/out_pct are now baked into
// each row by the Go arb compute (futures.go / spot.go / dex.go) and
// arrive in the /screener/long-short etc. payloads directly. The
// _inOutTimer / refreshInOut machinery stays defined for back-compat
// if any other code path ever needs an ad-hoc fetch, but nothing
// fires it on a schedule any more.
// (was: setInterval(refreshInOut, 3000) — caused 256-key POST batches
// every 3 s × N users which saturated apps / disk under load.)
void _inOutTimer; // silence "unused" — declared higher up.

// ── Initial REST load (instant, before WS connects) ───────────────────────────
(async function preload() {
  try {
    // Show skeleton while loading
    document.getElementById('tbody-funding').innerHTML =
      '<tr><td colspan="7" class="empty-msg"><span class="spinner"></span>Loading…</td></tr>';

    const [fundingRes, arbRes] = await Promise.all([
      Auth.apiFetch('/screener/funding'),
      Auth.apiFetch('/screener/long-short'),
    ]);
    if (fundingRes.ok) {
      const data = await fundingRes.json();
      _rows = data.rows || [];
      renderStats(data);
      if (_mode === 'funding') applyFilter(true);
    }
    if (arbRes.ok) {
      const data = await arbRes.json();
      _applyArbPayload(data);
      if (_mode === 'arb') applyArb();
    }
  } catch (_) {}
  connectWs();
  loadWatchlist();
  startLiveOpsPollers();
})();

// ═══════════════════════════════════════════════════════════════════════
//  ALPHA MODE · LIVE OPS SIDEBAR · WATCHLIST
// ═══════════════════════════════════════════════════════════════════════
let _alphaRows = [];
let _watchlist = {};     // key = "SYM|LONG>SHORT" → id
let _loOpen = false;
let _loTimers = {};

function _wlKey(sym, l, s) { return `${sym}|${l}>${s}`; }
function _isWatched(sym, l, s) { return !!_watchlist[_wlKey(sym, l, s)]; }

async function loadWatchlist() {
  // Watchlist is a per-user feature — anonymous visitors skip the call.
  if (!IS_AUTHED) return;
  try {
    const r = await Auth.apiFetch('/screener/watchlist');
    if (!r.ok) return;
    const rows = await r.json();
    _watchlist = {};
    for (const x of rows) _watchlist[_wlKey(x.symbol, x.long_exchange, x.short_exchange)] = x.id;
    _updateWlBadge();
    renderAllStars();
  } catch (_) {}
}

function _updateWlBadge() {
  const n = Object.keys(_watchlist).length;
  const badge = document.getElementById('wl-count-badge');
  const cnt = document.getElementById('wl-count');
  if (!badge || !cnt) return;
  cnt.textContent = n;
  badge.style.display = n > 0 ? '' : 'none';
}

async function toggleWatch(ev, sym, l, s) {
  ev.stopPropagation(); ev.preventDefault();
  const key = _wlKey(sym, l, s);
  const id = _watchlist[key];
  try {
    if (id) {
      const r = await Auth.apiFetch(`/screener/watchlist/${id}`, { method: 'DELETE' });
      if (r.ok) { delete _watchlist[key]; toast('Removed from watchlist', 'info'); }
    } else {
      const r = await Auth.apiFetch('/screener/watchlist', { method: 'POST',
        body: JSON.stringify({ symbol: sym, long_exchange: l, short_exchange: s }) });
      if (r.ok) { const j = await r.json(); _watchlist[key] = j.id; toast('Added to watchlist', 'success'); }
    }
    _updateWlBadge();
    renderAllStars();
  } catch (e) { toast('Watchlist failed', 'error'); }
}

function renderAllStars() {
  document.querySelectorAll('[data-wl-key]').forEach(el => {
    const on = !!_watchlist[el.dataset.wlKey];
    el.classList.toggle('on', on);
    el.innerHTML = on
      ? '<svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor"><path d="M8 1.5L9.7 6l4.8.4-3.7 3.3 1.1 4.8L8 11.9 4.1 14.5l1.1-4.8L1.5 6.4 6.3 6z"/></svg>'
      : '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35"><path d="M8 1.5L9.7 6l4.8.4-3.7 3.3 1.1 4.8L8 11.9 4.1 14.5l1.1-4.8L1.5 6.4 6.3 6z"/></svg>';
  });
}

function _starBtn(sym, l, s) {
  const key = _wlKey(sym, l, s);
  const on = !!_watchlist[key];
  const svg = on
    ? '<svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor"><path d="M8 1.5L9.7 6l4.8.4-3.7 3.3 1.1 4.8L8 11.9 4.1 14.5l1.1-4.8L1.5 6.4 6.3 6z"/></svg>'
    : '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35"><path d="M8 1.5L9.7 6l4.8.4-3.7 3.3 1.1 4.8L8 11.9 4.1 14.5l1.1-4.8L1.5 6.4 6.3 6z"/></svg>';
  return `<button class="star-btn ${on ? 'on' : ''}" data-wl-key="${key}" title="Watchlist" onclick="toggleWatch(event, '${sym}', '${l}', '${s}')">${svg}</button>`;
}

function _alphaColor(score) {
  // 0..40=red, 40..70=yellow, 70..100=green
  const s = Math.max(0, Math.min(100, score));
  if (s < 40) { const t = s / 40; return `rgba(248,113,113,${0.55 + t * 0.3})`; }
  if (s < 70) { return 'rgba(229,192,123,0.72)'; }
  return `rgba(26,255,171,${0.55 + (s - 70) / 30 * 0.35})`;
}

async function loadAlpha() {
  const tbody = document.getElementById('tbody-alpha');
  try {
    const r = await Auth.apiFetch('/screener/alpha');
    if (!r.ok) throw new Error('http');
    const data = await r.json();
    _alphaRows = (data.opportunities || []).slice(0, 120);
    renderAlpha();
  } catch (_) {
    if (tbody) tbody.innerHTML = '<tr><td colspan="7" class="empty-msg">Failed to load alpha</td></tr>';
  }
}

function renderAlpha() {
  const tbody = document.getElementById('tbody-alpha');
  const cards = document.getElementById('cards-alpha');
  if (!tbody) return;
  if (!_alphaRows.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty-msg">No opportunities</td></tr>';
    if (cards) cards.innerHTML = '';
    return;
  }
  const fmt = (n, d = 4) => (n == null ? '—' : Number(n).toFixed(d));
  const fmtVol = (v) => { if (!v) return '—'; if (v > 1e9) return (v / 1e9).toFixed(2) + 'B'; if (v > 1e6) return (v / 1e6).toFixed(2) + 'M'; return (v / 1e3).toFixed(1) + 'K'; };

  tbody.innerHTML = _alphaRows.map((o, i) => {
    const score = o.alpha_score || 0;
    const color = _alphaColor(score);
    const vol = Math.min(o.long_volume || 0, o.short_volume || 0);
    const netClass = (o.net_profit || 0) > 0 ? 'rate-pos' : 'rate-neg';
    return `
    <tr>
      <td class="td-rank">${o.alpha_rank || (i+1)}</td>
      <td><span class="sym-badge mono">${o.symbol}</span></td>
      <td><span class="ex-pill" style="--ex-color:var(--green)">${o.long_exchange}</span><span style="color:var(--text3);margin:0 4px">→</span><span class="ex-pill" style="--ex-color:var(--red)">${o.short_exchange}</span></td>
      <td><div class="alpha-bar-wrap" style="width:140px"><div class="alpha-bar-fill" style="width:${score}%;background:${color}"></div><div class="alpha-bar-num">${score.toFixed(1)}</div></div></td>
      <td class="mono ${netClass}">${fmt(o.net_profit, 3)}%</td>
      <td class="mono" style="color:var(--text2)">${fmtVol(vol)}</td>
      <td class="td-actions">
        ${_starBtn(o.symbol, o.long_exchange, o.short_exchange)}
        <a class="act-btn" href="/arb?symbol=${o.symbol}&long=${o.long_exchange}&short=${o.short_exchange}" title="Open arb detail">
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M3 9l6-6M5 3h4v4"/></svg>
        </a>
      </td>
    </tr>`;
  }).join('');
}

// ── Live Ops sidebar ──────────────────────────────────────────────────────
function toggleLiveOps(force) {
  _loOpen = typeof force === 'boolean' ? force : !_loOpen;
  document.getElementById('lo-sidebar').classList.toggle('open', _loOpen);
  document.getElementById('lo-backdrop').classList.toggle('open', _loOpen);
  if (_loOpen) { refreshHealth(); refreshAnomalies(); refreshLeaderboard(); }
}

async function refreshHealth() {
  try {
    const r = await Auth.apiFetch('/screener/health?window_min=60');
    if (!r.ok) return;
    const rows = await r.json();
    const el = document.getElementById('lo-health-list');
    if (!el) return;
    if (!rows.length) { el.innerHTML = '<div style="color:var(--text3);font-size:11px">No data yet</div>'; return; }
    el.innerHTML = rows.map(h => `
      <div class="health-row">
        <span class="health-dot ${h.status}"></span>
        <span class="health-name">${h.exchange}</span>
        <span class="health-lat">${h.latency_p50_ms || h.last_latency_ms || '—'}ms</span>
        <span class="health-rate">${h.success_rate_pct.toFixed(0)}%</span>
      </div>`).join('');
  } catch (_) {}
}

async function refreshAnomalies() {
  try {
    const r = await Auth.apiFetch('/screener/anomalies?hours=24&limit=10');
    if (!r.ok) return;
    const rows = await r.json();
    const el = document.getElementById('lo-anom-list');
    if (!el) return;
    if (!rows.length) { el.innerHTML = '<div style="color:var(--text3);font-size:11px">No anomalies in last 24h</div>'; return; }
    el.innerHTML = rows.map(a => `
      <div class="anom-row" onclick="location.href='/arb?symbol=${a.symbol}&long=${a.long_exchange}&short=${a.short_exchange}'">
        <span class="anom-sym">${a.symbol}</span>
        <span class="anom-pair">${a.long_exchange}→${a.short_exchange}</span>
        <span class="anom-spread ${a.spread_pct >= 0 ? 'rate-pos' : 'rate-neg'}">${a.spread_pct.toFixed(3)}%</span>
        <span class="anom-z">z=${a.z_score.toFixed(1)}</span>
      </div>`).join('');
  } catch (_) {}
}

async function refreshLeaderboard() {
  try {
    const r = await Auth.apiFetch('/screener/leaderboard?hours=24&limit=10');
    if (!r.ok) return;
    const rows = await r.json();
    const el = document.getElementById('lo-lb-list');
    if (!el) return;
    if (!rows.length) { el.innerHTML = '<div style="color:var(--text3);font-size:11px">Collecting data…</div>'; return; }
    el.innerHTML = rows.map((l, i) => `
      <div class="lb-row" onclick="location.href='/arb?symbol=${l.symbol}&long=${l.long_exchange}&short=${l.short_exchange}'">
        <span class="lb-rank">${i+1}</span>
        <span class="lb-sym">${l.symbol}</span>
        <span class="lb-pair">${l.long_exchange}→${l.short_exchange}</span>
        <span class="lb-alpha">${l.avg_alpha.toFixed(1)}</span>
      </div>`).join('');
  } catch (_) {}
}

function startLiveOpsPollers() {
  // prime once so sidebar shows fresh data on first open
  refreshHealth();
  _loTimers.health = setInterval(() => { if (document.hidden) return; refreshHealth(); }, 30000);
  _loTimers.anom = setInterval(() => { if (document.hidden) return; refreshAnomalies(); }, 60000);
  _loTimers.lb = setInterval(() => { if (document.hidden) return; refreshLeaderboard(); }, 60000);
}

// Prefetch arb detail page + warm per-pair backend caches on row hover
const _arbPrefetchSeen = new Set();
function _arbPrefetch(url) {
  if (!url || _arbPrefetchSeen.has(url)) return;
  _arbPrefetchSeen.add(url);
  // Browser-side prefetch (HTML + static assets)
  const link = document.createElement('link');
  link.rel = 'prefetch';
  link.href = url;
  link.as = 'document';
  document.head.appendChild(link);
  // Server-side cache warm (no-await fire-and-forget)
  try {
    const u = new URL(url, location.origin);
    const sym = u.searchParams.get('symbol');
    const long = u.searchParams.get('long');
    const short = u.searchParams.get('short');
    if (sym && long && short) {
      const q = `symbol=${sym}&long_ex=${long}&short_ex=${short}`;
      Auth.apiFetch(`/screener/arb-price-history?${q}`).catch(() => {});
      Auth.apiFetch(`/screener/arb-history?${q}`).catch(() => {});
      Auth.apiFetch(`/screener/open-interest?${q}`).catch(() => {});
    }
  } catch (_) {}
}

// Global hover-prefetch: any <a> pointing to /arb gets pre-warmed when the
// user hovers it. Single delegated listener — no need to wire onmouseenter
// on every link. ~100ms head start before click means /arb feels instant.
document.addEventListener('mouseover', (e) => {
  const a = e.target.closest && e.target.closest('a[href^="/arb?"]');
  if (a && a.href) _arbPrefetch(a.href);
}, { passive: true });
