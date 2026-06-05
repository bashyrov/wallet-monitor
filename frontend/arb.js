/* Extracted from frontend/arb.html — see _SOURCE_CHANGELOG below.
   Loaded once via <script src="/arb.js" defer>; cached separately from
   the HTML shell so pair navigation reuses the JS body. 2026-05-14. */

/* ── arb.html block #1 ─────────────────────────────────────────── */
// Arb detail is public. User-specific sections (positions / balances /
// orders / trade card) just render empty for anonymous visitors — apiFetch
// will redirect to /login on the first 401 from a user-only endpoint.
// IS_AUTHED is kept for downstream JS that gates UI on it.
const IS_AUTHED = Auth.isLoggedIn();

// ── WS idle-disconnect ──────────────────────────────────────────────
// Если юзер открыл /arb и забыл вкладку, WS соединения продолжают
// получать данные → жжём CPU + растёт memory от accumulated message
// buffers. После 5 мин неактивности закрываем все WS, при возврате
// (любое событие mouse/scroll/keyboard/touch + visibilitychange) —
// реконнект. Server-side тоже выигрывает — меньше broadcast'ов
// клиентам которые не смотрят.
const _Idle = (() => {
  const IDLE_MS = 5 * 60 * 1000;  // 5 минут
  let _lastActivity = Date.now();
  let _closed = false;
  const _wakers = [];

  function track() { _lastActivity = Date.now(); if (_closed) wakeUp(); }
  function isIdle() { return Date.now() - _lastActivity > IDLE_MS; }
  function shouldStayClosed() { return _closed; }
  function onWake(fn) { _wakers.push(fn); }
  function closeAll() {
    _closed = true;
    // Trigger registered close-fns (each WS owner provides one).
    // Owners typically just call `try { ws.close(4000, 'idle'); }`
    // Their onClose listener bails on reconnect because _closed=true.
    for (const w of _wakers) {
      try { w.close && w.close(); } catch (_) {}
    }
  }
  function wakeUp() {
    if (!_closed) return;
    _closed = false;
    for (const w of _wakers) {
      try { w.open && w.open(); } catch (_) {}
    }
  }

  // Track activity — passive listeners to avoid scroll-perf penalty.
  ['mousemove', 'mousedown', 'keydown', 'scroll', 'touchstart', 'wheel']
    .forEach(ev => document.addEventListener(ev, track, { passive: true }));
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) track();
  });

  // Periodic check — once per minute is enough.
  setInterval(() => {
    if (document.hidden) return;
    if (isIdle() && !_closed) {
      try { console.debug('[idle] closing WS — no activity for 5 min'); } catch (_) {}
      closeAll();
    }
  }, 60_000);

  return { track, isIdle, shouldStayClosed, onWake, closeAll, wakeUp };
})();

// ── Prefetch helper for pair-switching links ─────────────────────────
// When the user hovers a "switch pair" UI element (search popover, swap
// button, pair-card link) we eagerly prefetch the next /arb HTML +
// warm backend caches. By the time they click, the page is in browser
// cache and the per-pair API responses are ready.
const _arbPrefetchSeen = new Set();
function _arbPrefetch(url) {
  if (!url || _arbPrefetchSeen.has(url)) return;
  _arbPrefetchSeen.add(url);
  const link = document.createElement('link');
  link.rel = 'prefetch';
  link.href = url;
  link.as = 'document';
  document.head.appendChild(link);
  try {
    const u = new URL(url, location.origin);
    const sym = u.searchParams.get('symbol');
    const lng = u.searchParams.get('long');
    const sht = u.searchParams.get('short');
    if (sym && lng && sht) {
      const q = `symbol=${sym}&long_ex=${lng}&short_ex=${sht}`;
      Auth.apiFetch(`/screener/arb-price-history?${q}`).catch(() => {});
      Auth.apiFetch(`/screener/open-interest?${q}`).catch(() => {});
    }
  } catch (_) {}
}
// Delegated mouseover — covers swap-pair buttons, search popover items,
// any future <a href="/arb?..."> additions without per-element wiring.
document.addEventListener('mouseover', (e) => {
  const a = e.target.closest && e.target.closest('a[href^="/arb?"]');
  if (a && a.href) _arbPrefetch(a.href);
}, { passive: true });

/* ── arb.html block #2 ─────────────────────────────────────────── */
// Single source of truth lives in /exchanges.js — the local consts are just
// thin aliases so existing call sites keep working.
const EX_LABEL = (window.EX && window.EX.labels) || {binance:'Binance',bybit:'Bybit',okx:'OKX',gate:'Gate',kucoin:'KuCoin',mexc:'MEXC',bitget:'Bitget',hyperliquid:'Hyperliquid',aster:'Aster',ethereal:'Ethereal',whitebit:'WhiteBIT',bingx:'BingX',backpack:'Backpack',lighter:'Lighter',paradex:'Paradex'};
const EX_COLOR = (window.EX && window.EX.colors) || {binance:'#F0B90B',bybit:'#F0842D',okx:'#C8C8C8',gate:'#17C684',kucoin:'#09BA86',mexc:'#17D854',bitget:'#00D2C8',hyperliquid:'#64B4FF',aster:'#8A63D2',ethereal:'#C864C8',whitebit:'#2DCCCD',bingx:'#1DB8F2',backpack:'#4ADE80',lighter:'#A78BFA',paradex:'#FF6A6A'};

const _p=new URLSearchParams(location.search);
const SYM=(_p.get('symbol')||'').toUpperCase();
const LONG=(_p.get('long')||'').toLowerCase();
const SHORT=(_p.get('short')||'').toLowerCase();
// type: 'long-short' (default, CEX perp ⇄ CEX perp), 'spot' (CEX spot long, CEX perp short),
//       'dex' (DEX pool long via DexScreener, CEX perp short).
// Canonical URL param is `type=long-short / spot-short / dex-short`. Legacy
// 'futures' and 'spot' / 'dex' short forms are accepted and normalised.
const _RAW_TYPE = (_p.get('type') || 'long-short').toLowerCase();
const _TYPE_ALIAS = {
  'futures': 'long-short', 'arb': 'long-short', 'arbitrage': 'long-short', 'long-short': 'long-short', 'longshort': 'long-short',
  'spot': 'spot', 'spot-short': 'spot', 'spotshort': 'spot',
  'dex':  'dex',  'dex-short':  'dex',  'dexshort':  'dex',
};
// Internal TYPE keeps the old short-form ('futures'/'spot'/'dex') so the rest
// of the page code doesn't need to change.
const TYPE = (() => {
  const canon = _TYPE_ALIAS[_RAW_TYPE] || 'long-short';
  return canon === 'long-short' ? 'futures' : canon;
})();
const DEX_CHAIN=(_p.get('chain')||'').toLowerCase();  // only meaningful for type=dex
const DEX_ADDR=(_p.get('addr')||'').toLowerCase();    // only meaningful for type=dex
const DEX_PAIR=(_p.get('pair')||'').toLowerCase();    // only meaningful for type=dex
if(!SYM||!LONG||!SHORT) document.body.innerHTML='<div style="padding:40px;color:var(--text3);font-family:Inter,sans-serif">Missing params. <a href="/screener" style="color:var(--green)">Back</a></div>';

// Render the Balance cell for a /trade/balances row:
// total prominently, with spot / fut breakdown underneath when both are
// non-null. If this row matches the current pair's leg, highlight the slice
// that's actually usable for THIS pair/mode:
//   long-short (TYPE=futures): both legs use futures
//   spot-short (TYPE=spot):    long leg uses spot, short leg uses futures
//   dex-short  (TYPE=dex):     long leg has no balance (DEX), short uses futures
// Venues not in the current pair just show the breakdown un-highlighted.
function _renderBalCell(w) {
  if (w.error) {
    return `<span style="color:var(--red);font-size:10.5px" title="${_walletBalErrAttr(w.error)}">${_walletBalErrLabel(w.error)}</span>`;
  }
  const fmt = v => (v == null ? '—' : `$${Number(v).toFixed(2)}`);
  const total = w.balance_usdt;
  const sp = w.spot_usdt, fu = w.futures_usdt;
  if (total == null) return '<span style="color:var(--text3)">—</span>';
  // Which slice highlights for THIS row?
  let hi = null;  // 'spot' | 'fut' | null
  if (w.exchange === LONG)  hi = (TYPE === 'spot') ? 'spot' : 'fut';
  if (w.exchange === SHORT) hi = 'fut';
  const breakdown = (sp != null && fu != null && (sp > 0 || fu > 0))
    ? `<div style="font-size:9.5px;color:var(--text3);margin-top:1px;line-height:1.2">
         <span style="${hi==='spot'?'color:var(--green);font-weight:600':''}">sp ${fmt(sp)}</span>
         <span style="opacity:0.5">·</span>
         <span style="${hi==='fut' ?'color:var(--green);font-weight:600':''}">fu ${fmt(fu)}</span>
       </div>`
    : '';
  return `<div style="font-weight:600">${fmt(total)}</div>${breakdown}`;
}

// Type-gate: full futures terminal only makes sense for type=futures.
// Spot/Short and DEX/Short need their own detail layouts (coming in next
// iteration). For now show a clean placeholder card so the button
// navigation works end-to-end and users aren't dropped into a broken
// terminal painted with dashes.
if (TYPE === 'spot' || TYPE === 'dex') {
  const SRC = TYPE === 'spot' ? '/screener/spot-short' : '/screener/dex-short';
  const BACK = '/screener?mode=' + TYPE;
  const IS_DEX = TYPE === 'dex';
  const LONG_LABEL_PLAIN = IS_DEX ? 'DEX' : 'SPOT';

  // Reuse the futures arb terminal's CSS — .infobar / .hero-block / .ex-card /
  // .metric-block / .workspace / .col-left / .col-books / .col-info are all
  // already defined in the page <style>. We only add a tiny CSS tail for the
  // iframe container + the pair-specific single-book view.
  document.body.className = ''; // reset
  document.body.classList.add('pt-type-' + TYPE);
  document.body.innerHTML = `
    <style>
      .pt-iframe{flex:1;border:0;background:var(--bg);min-height:0;}
      /* DEX has only one order book → let col-left breathe */
      body.pt-type-dex .col-books{flex:0 0 340px;}
      body.pt-type-dex .col-left{flex:1;}
      body.pt-type-spot .col-left{flex:0 0 46%;}
      body.pt-type-spot .col-books{flex:1;}
    </style>

    <header class="topbar"><app-navbar page="arb"></app-navbar></header>

    <div class="infobar">
      <div class="hero-block">
        ${IS_DEX
          ? `<span class="hero-sym">${SYM}</span>`
          : `<span class="hero-sym" onclick="openSymbolPopover(this)" title="Change symbol">${SYM}</span>`}
        <span class="live-dot"></span>
        <div class="hero-exs">
          ${IS_DEX
            ? `<span class="hero-ex"><span class="dot" style="background:#A78BFA"></span><span>${LONG_LABEL_PLAIN}</span></span>`
            : `<span class="hero-ex" onclick="openExPopover(this,'long')" title="Change spot venue"><span class="dot dot-${LONG}"></span><span>SPOT · ${EX_LABEL[LONG]||LONG}</span></span>`}
          <span class="hero-swap" style="opacity:.5;cursor:default">⇄</span>
          <span class="hero-ex" onclick="openExPopover(this,'short')" title="Change short exchange"><span class="dot dot-${SHORT}"></span><span>${EX_LABEL[SHORT]||SHORT}</span></span>
        </div>
      </div>
      <div class="ap-pop" id="ap-pop">
        <div class="ap-search"><input id="ap-search-input" type="text" placeholder="Search…" autocomplete="off"/></div>
        <div class="ap-list" id="ap-list"></div>
      </div>

      <div class="ex-card">
        <div class="ex-card-hdr">
          <span class="tb-ex-dot" style="background:${IS_DEX ? '#A78BFA' : 'var(--green)'}"></span>
          ${IS_DEX ? 'DEX' : 'SPOT'} · <span id="pt-long-name" style="color:var(--text);font-weight:600">—</span>
        </div>
        <div class="ex-card-row">
          <span class="cell"><span class="lbl">Price</span><span class="val" id="pt-px-long">—</span></span>
          ${IS_DEX
            ? '<span class="cell"><span class="lbl">Chain</span><span class="val muted" id="pt-chain">—</span></span>'
            : '<span class="cell"><span class="lbl">Vol 24h</span><span class="val muted" id="pt-sv">—</span></span>'}
        </div>
        <div class="ex-card-row">
          ${IS_DEX
            ? '<span class="cell"><span class="lbl">Liquidity</span><span class="val muted" id="pt-liq">—</span></span><span class="cell"><span class="lbl">Vol 24h</span><span class="val muted" id="pt-dv">—</span></span>'
            : '<span class="cell"><span class="lbl">Exchange</span><span class="val muted" id="pt-spot-exlbl">—</span></span>'}
        </div>
      </div>

      <div class="ex-card">
        <div class="ex-card-hdr"><span class="tb-ex-dot" style="background:var(--red)"></span>SHORT · <span id="pt-short-name" style="color:var(--text);font-weight:600">${EX_LABEL[SHORT]||SHORT}</span></div>
        <div class="ex-card-row">
          <span class="cell"><span class="lbl">Fund</span><span class="val" id="pt-fund">—</span></span>
          <span class="cell"><span class="lbl">Ivl</span><span class="val muted" id="pt-ivl">—</span></span>
          <span class="cell"><span class="lbl">Next</span><span class="val muted" id="pt-next">—</span></span>
        </div>
        <div class="ex-card-row">
          <span class="cell"><span class="lbl">Price</span><span class="val muted" id="pt-px-short">—</span></span>
          <span class="cell"><span class="lbl">Vol</span><span class="val muted" id="pt-pv">—</span></span>
        </div>
      </div>

      <div class="metric-block accent">
        <span class="ib-label">Live Spread</span>
        <span class="metric-val" id="pt-live-spread">—</span>
      </div>
      <div class="metric-block">
        <span class="ib-label" title="Funding the short leg receives per 8h window">Funding / 8h</span>
        <span class="metric-val" id="pt-funding-8h">—</span>
      </div>
      <div class="metric-block">
        <span class="ib-label">Net / 8h</span>
        <span class="metric-val" id="pt-net">—</span>
      </div>
      <div class="metric-block">
        <span class="ib-label">APR</span>
        <span class="metric-val" id="pt-apr">—</span>
      </div>
    </div>

    <div class="workspace">
      <div class="left-stack" style="flex:1;display:flex;flex-direction:column;min-height:0">
        <div class="main top-row" style="flex:1;min-height:0">

          <!-- LEFT: chart area (iframe or spot info) -->
          <div class="col-left">
            <div class="chart-tabs">
              <div class="chart-tab active">${IS_DEX ? 'DexScreener' : 'Entry / Exit'}</div>
            </div>
            ${IS_DEX
              ? `<iframe id="pt-dex-frame" class="pt-iframe" src="about:blank" loading="lazy" allow="clipboard-write; fullscreen" allowfullscreen></iframe>`
              : `<div id="pt-ee-chart" style="flex:1;min-height:0;background:transparent;position:relative">
                   <div style="position:absolute;top:8px;left:10px;z-index:5;display:flex;align-items:center;gap:10px;font-size:10px;font-family:var(--mono);color:var(--text3)">
                     <span style="display:flex;align-items:center;gap:4px">
                       <span style="display:inline-flex;gap:1px"><span style="width:3px;height:8px;background:#1AFFAB;border-radius:1px"></span><span style="width:3px;height:8px;background:#F87171;border-radius:1px"></span></span>
                       Basis %
                     </span>
                     <span style="color:var(--text3)">·</span>
                     <span id="pt-ee-count">0 bars</span>
                   </div>
                   <div id="pt-ee-empty" style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--text3);font-size:12px;pointer-events:none">Collecting order-book ticks…</div>
                 </div>`}
          </div>

          <!-- CENTER: order books (futures-style .book-panel for all types) -->
          <div class="col-books">
            <button type="button" class="books-toggle" onclick="_toggleBooksCol(this)" aria-expanded="false">
              <span>Show order books</span>
              <span class="bt-chevron">▾</span>
            </button>
            <div class="books-row">
              ${!IS_DEX ? `
              <!-- LONG (SPOT) book — CEX spot venue -->
              <div class="book-panel" data-side="long">
                <div class="book-header">
                  <span class="book-ex-name" id="pt-book-long-name">${EX_LABEL[LONG]||LONG}</span>
                  <span class="book-price-hdr" id="pt-book-long-age">—</span>
                </div>
                <div class="book-cols"><span class="book-col-lbl">Price</span><span class="book-col-lbl">Amount</span><span class="book-col-lbl">Total</span></div>
                <div class="book-rows">
                  <div class="book-asks" id="pt-asks-long"></div>
                  <div class="book-mid"><span class="book-mid-arrow" id="pt-mid-arrow-long">·</span><span class="book-mid-price" id="pt-mid-long">—</span></div>
                  <div class="book-bids" id="pt-bids-long"></div>
                </div>
              </div>` : ''}
              <!-- SHORT (PERP) book — always present -->
              <div class="book-panel" data-side="short">
                <div class="book-header">
                  <span class="book-ex-name" id="pt-book-short-name">${EX_LABEL[SHORT]||SHORT}</span>
                  <span class="book-price-hdr" id="pt-book-short-age">—</span>
                </div>
                <div class="book-cols"><span class="book-col-lbl">Price</span><span class="book-col-lbl">Amount</span><span class="book-col-lbl">Total</span></div>
                <div class="book-rows">
                  <div class="book-asks" id="pt-asks-short"></div>
                  <div class="book-mid"><span class="book-mid-arrow" id="pt-mid-arrow-short">·</span><span class="book-mid-price" id="pt-mid-short">—</span></div>
                  <div class="book-bids" id="pt-bids-short"></div>
                </div>
              </div>
            </div>
          </div>

        </div><!-- /.main.top-row -->

    <!-- ── Bottom panel: Positions / Active Triggers / Orders / P&L / Balances.
         Lives INSIDE .left-stack (same as the futures layout) so .col-info
         on the right is sibling of left-stack and spans the full workspace
         height. When this section was outside .workspace the right panel
         got squished by acc-block's vertical bite out of the body height. -->
    <section class="acc-block" id="acc-block">
      <div class="acc-tabs" role="tablist">
        <div class="acc-tab is-active" data-pane="positions" onclick="accSwitch(this)" role="tab">Positions <span class="acc-count" id="acc-cnt-positions">0</span></div>
        <div class="acc-tab" data-pane="triggers" onclick="accSwitch(this)" role="tab">Active Triggers <span class="acc-count" id="acc-cnt-triggers">0</span></div>
        <div class="acc-tab" data-pane="orders"   onclick="accSwitch(this)" role="tab">Order History <span class="acc-count" id="acc-cnt-orders">0</span></div>
        <div class="acc-tab" data-pane="pnl"      onclick="accSwitch(this)" role="tab">P&amp;L</div>
        <div class="acc-tab" data-pane="balances" onclick="accSwitch(this)" role="tab">Balances <span class="acc-count" id="acc-cnt-balances">0</span></div>
        <span class="acc-spacer"></span>
        <div class="acc-keyinfo" id="acc-keyinfo">
          <span class="pill ro" title="Read-only keys"><span class="pill-dot"></span>Read-only <span class="mono" id="acc-ro-count">0</span></span>
          <span class="pill tr" title="Trading keys"><span class="pill-dot"></span>Trade <span class="mono" id="acc-tr-count">0</span></span>
          <button type="button" class="pill" style="background:none;cursor:pointer;font-family:inherit" title="Manage API keys" onclick="openKeysPopup()">Keys ⚙</button>
        </div>
      </div>
      <div class="acc-body">
        <div class="acc-pane is-active" id="acc-pane-positions">
          <table class="acc-table">
            <thead><tr><th>Symbol</th><th>Exchange</th><th>Side</th><th class="num">Size</th><th class="num">Entry</th><th class="num">Mark</th><th class="num">Funding P&amp;L</th><th class="num">uPnL</th><th class="num">uPnL %</th><th></th></tr></thead>
            <tbody id="acc-positions-body"></tbody>
          </table>
          <div class="acc-empty" id="acc-positions-empty">
            <div class="acc-empty-icon"><svg width="22" height="22" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="12" height="12" rx="2"/><path d="M2 9h12M8 2v12"/></svg></div>
            <h4>No open positions</h4>
            <p>Connect a read-only API key to see exchange positions here.</p>
            <a href="/portfolio" class="acc-empty-cta">Manage API keys</a>
          </div>
        </div>
        <div class="acc-pane" id="acc-pane-triggers">
          <table class="acc-table">
            <thead><tr>
              <th>Type</th><th>Pair</th><th>Long → Short</th>
              <th class="num">Trigger</th><th class="num">Filled</th>
              <th class="num">Qty</th><th>Mode</th><th>Status</th>
              <th>Created</th><th></th>
            </tr></thead>
            <tbody id="acc-triggers-body"></tbody>
          </table>
          <div class="acc-empty" id="acc-triggers-empty">
            <div class="acc-empty-icon"><svg width="22" height="22" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6"/><path d="M8 5v3l2 1"/></svg></div>
            <h4>No active triggers</h4>
            <p>Place a trigger from the Live Trading panel — it'll fire when spread crosses your target.</p>
          </div>
        </div>
        <div class="acc-pane" id="acc-pane-orders">
          <table class="acc-table">
            <thead><tr><th style="width:24px"></th><th>Time</th><th>Action</th><th>Symbol</th><th>Exchange</th><th>Side</th><th class="num">Qty</th><th class="num">Price</th><th>Status</th></tr></thead>
            <tbody id="acc-orders-body"></tbody>
          </table>
          <div class="acc-empty" id="acc-orders-empty">
            <h4>No orders yet</h4>
            <p>Order history shows up here after you place trades.</p>
          </div>
        </div>
        <div class="acc-pane" id="acc-pane-pnl">
          <table class="acc-table">
            <thead><tr><th>Pair</th><th>Status</th><th class="num">Entry %</th><th class="num">Exit %</th><th class="num">Long $</th><th class="num">Short $</th><th class="num">Funding $</th><th class="num">Total $</th><th>Duration</th></tr></thead>
            <tbody id="acc-pnl-body"></tbody>
          </table>
          <div class="acc-empty" id="acc-pnl-empty">
            <h4>No P&amp;L yet</h4>
            <p>Closed pairs show up here with combined leg + funding P&amp;L.</p>
          </div>
        </div>
        <div class="acc-pane" id="acc-pane-balances">
          <table class="acc-table">
            <thead><tr><th>Exchange</th><th>Account</th><th>Purpose</th><th class="num">Balance</th><th class="num">Reserved</th><th class="num">Available</th></tr></thead>
            <tbody id="acc-balances-body"></tbody>
          </table>
          <div class="acc-empty" id="acc-balances-empty">
            <h4>No connected exchanges</h4>
            <p>Add API keys in Portfolio to see live balances here.</p>
            <a href="/portfolio" class="acc-empty-cta">Manage API keys</a>
          </div>
        </div>
      </div>
    </section>

      </div><!-- /.left-stack -->

          <!-- RIGHT: Trade panel — sibling of .left-stack so it spans full height -->
          <div class="col-info">
            <div class="col-info-title">Execute trade</div>
            <div style="flex:1;overflow-y:auto;display:flex;flex-direction:column">

              ${IS_DEX
                ? `<!-- DEX leg keeps the simple wallet-handoff card — we never sign DEX swaps -->
                   <div class="trade-leg" id="pt-trade-long">
                     <div class="trade-leg-head">
                       <span class="trade-leg-badge trade-leg-long" style="color:#A78BFA;background:rgba(167,139,250,.12);border-color:rgba(167,139,250,.35)">DEX</span>
                       <span class="trade-leg-ex" id="pt-trade-long-venue">—</span>
                       <span class="trade-leg-status missing">manual</span>
                     </div>
                     <div class="trade-leg-body">
                       <div class="trade-price-row">
                         <span class="trade-lbl">Last</span>
                         <span class="trade-last mono" id="pt-trade-long-px">—</span>
                         <span class="trade-spacer"></span>
                         <span class="trade-lbl">Liq</span>
                         <span class="trade-bal mono" id="pt-trade-long-meta">—</span>
                       </div>
                       <div class="trade-info-rows">
                         <div class="trade-info-row"><span>Fees (round-trip)</span><span class="mono" id="pt-long-fees">—</span></div>
                         <div class="trade-info-row"><span>Basis at entry</span><span class="mono" id="pt-long-basis">—</span></div>
                       </div>
                       <a id="pt-dex-swap" class="trade-submit trade-long-btn" href="#" target="_blank" rel="noopener" style="background:linear-gradient(135deg,#A78BFA 0%,#8B5CF6 100%);color:#0B0B0E;text-align:center;text-decoration:none;display:block">Open swap on DEX →</a>
                       <div style="font-size:10.5px;color:var(--text3);line-height:1.45;text-align:center;margin-top:4px">Swap runs in your wallet (MetaMask / Phantom) via DexScreener.</div>
                     </div>
                   </div>`
                : `<!-- Spot/short reuses the unified lt-panel from the futures layout — same triggers, TP/SL, portion size, schedule, account picker, all of it.
                       ltInit() auto-derives pair_kind='spot_short' from URL ?type= so the long-leg margin row is hidden and leverage caps to the perp-leg max. -->
                   <section class="info-card lt-panel" id="lt-panel">
                     <div class="lt-tabs" role="tablist">
                       <button class="lt-tab is-active" data-mode="open"  onclick="ltSwitchMode('open')"  role="tab">Open</button>
                       <button class="lt-tab"            data-mode="close" onclick="ltSwitchMode('close')" role="tab">Close</button>
                     </div>
                     <div class="lt-balances">
                       <div class="lt-bal-card" id="lt-bal-long">
                         <div class="lt-bal-label-row">
                           <div class="lt-bal-label" id="lt-bal-long-label">SPOT · —</div>
                           <button type="button" class="lt-keys-btn" onclick="ltOpenKeys('long')" title="Pick account / API key">
                             <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="2.5"/><path d="M8 1.5v2M8 12.5v2M14.5 8h-2M3.5 8h-2M12.6 3.4l-1.4 1.4M4.8 11.2l-1.4 1.4M12.6 12.6l-1.4-1.4M4.8 4.8L3.4 3.4"/></svg>
                             Keys
                           </button>
                         </div>
                         <div class="lt-bal-row"><span class="lt-bal-k">Account</span><span class="lt-bal-v" id="lt-bal-long-name" style="font-size:10px;color:var(--text3)">—</span></div>
                         <div class="lt-bal-row"><span class="lt-bal-k">Wallet</span><span class="lt-bal-v" id="lt-bal-long-total">—</span></div>
                         <div class="lt-bal-row"><span class="lt-bal-k">Available</span><span class="lt-bal-v" id="lt-bal-long-avail">—</span></div>
                       </div>
                       <div class="lt-bal-card" id="lt-bal-short">
                         <div class="lt-bal-label-row">
                           <div class="lt-bal-label" id="lt-bal-short-label">SHORT · —</div>
                           <button type="button" class="lt-keys-btn" onclick="ltOpenKeys('short')" title="Pick account / API key">
                             <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="2.5"/><path d="M8 1.5v2M8 12.5v2M14.5 8h-2M3.5 8h-2M12.6 3.4l-1.4 1.4M4.8 11.2l-1.4 1.4M12.6 12.6l-1.4-1.4M4.8 4.8L3.4 3.4"/></svg>
                             Keys
                           </button>
                         </div>
                         <div class="lt-bal-row"><span class="lt-bal-k">Account</span><span class="lt-bal-v" id="lt-bal-short-name" style="font-size:10px;color:var(--text3)">—</span></div>
                         <div class="lt-bal-row"><span class="lt-bal-k">Wallet</span><span class="lt-bal-v" id="lt-bal-short-total">—</span></div>
                         <div class="lt-bal-row"><span class="lt-bal-k">Available</span><span class="lt-bal-v" id="lt-bal-short-avail">—</span></div>
                       </div>
                     </div>
                     <div class="lt-keys-pop hidden" id="lt-keys-pop" onclick="if(event.target===this)ltCloseKeys()">
                       <div class="lt-keys-modal" id="lt-keys-modal">
                         <div class="lt-keys-head">
                           <span id="lt-keys-title">Select account</span>
                           <button type="button" class="lt-keys-x" onclick="ltCloseKeys()">×</button>
                         </div>
                         <div class="lt-keys-body" id="lt-keys-body"></div>
                         <div class="lt-keys-foot">
                           <a href="/portfolio#wallets" target="_blank" rel="noopener" class="lt-keys-add">+ Add a key</a>
                         </div>
                       </div>
                     </div>
                     <div class="lt-row" id="lt-margin-row">
                       <label class="lt-lbl">Margin</label>
                       <div class="lt-seg">
                         <button type="button" class="lt-seg-btn is-active" data-v="isolated" onclick="ltSetMargin('isolated')">Isolated</button>
                         <button type="button" class="lt-seg-btn" data-v="cross" onclick="ltSetMargin('cross')">Cross</button>
                       </div>
                       <span class="lt-lbl" id="lt-lev-lbl" style="margin-left:14px">Leverage</span>
                       <select class="lt-sel" id="lt-leverage" onchange="ltRecalc()">
                         <option value="1">1x</option><option value="2">2x</option><option value="3" selected>3x</option>
                         <option value="5">5x</option><option value="10">10x</option><option value="20">20x</option>
                         <option value="50">50x</option><option value="100">100x</option>
                       </select>
                     </div>
                     <div class="lt-row">
                       <label class="lt-lbl">Trigger Spread</label>
                       <div class="lt-input-wrap">
                         <input id="lt-trig" class="lt-input" type="number" step="0.001" placeholder="Last" oninput="ltCheckImmediate()">
                         <span class="lt-suffix">%</span>
                       </div>
                     </div>
                     <div class="lt-warn" id="lt-warn" style="display:none"></div>
                     <div class="lt-row">
                       <label class="lt-lbl">Quantity</label>
                       <div class="lt-input-wrap">
                         <input id="lt-qty" class="lt-input" type="number" step="0.001" placeholder="0.00"
                                oninput="ltOnQtyInput()" onblur="ltOnQtyBlur()">
                         <div class="lt-unit-toggle">
                           <button type="button" class="lt-unit-btn is-active" data-unit="token" onclick="ltSetUnit('token')" id="lt-unit-token">—</button>
                           <button type="button" class="lt-unit-btn"           data-unit="usdt"  onclick="ltSetUnit('usdt')"  id="lt-unit-usdt">USDT</button>
                         </div>
                       </div>
                     </div>
                     <div class="lt-qty-hint" id="lt-qty-hint"></div>
                     <div class="lt-alloc">
                       <div class="lt-alloc-head">
                         <span>% Allocation</span>
                         <span class="mono" id="lt-alloc-pct">0%</span>
                       </div>
                       <div class="lt-alloc-bar">
                         <input type="range" id="lt-alloc-slider" min="0" max="100" value="0" oninput="ltOnAlloc(event)">
                         <div class="lt-alloc-marks">
                           <span>0%</span><span>25%</span><span>50%</span><span>75%</span><span>100%</span>
                         </div>
                       </div>
                     </div>
                     <div class="lt-summary">
                       <div class="lt-sum-row"><span>Position Value</span><span class="mono" id="lt-pos-value">0.00 USDT</span></div>
                       <div class="lt-sum-row"><span>Margin Used</span><span class="mono" id="lt-margin-used">0.00 USDT</span></div>
                       <div class="lt-sum-row"><span>Effective spread @ size</span><span class="mono" id="lt-eff-spread">—</span></div>
                     </div>
                     <details class="lt-fold">
                       <summary><input type="checkbox" id="lt-portion-on" onchange="ltOnPortionToggle(event)"> Portion Size</summary>
                       <div class="lt-fold-body">
                         <div class="lt-row">
                           <label class="lt-lbl">Portion</label>
                           <div class="lt-input-wrap">
                             <input id="lt-portion" class="lt-input" type="number" step="0.001" placeholder="0.00">
                             <span class="lt-suffix" id="lt-portion-suffix">—</span>
                           </div>
                         </div>
                         <div class="lt-sum-row"><span>One Portion Cost</span><span class="mono" id="lt-portion-cost">0.00 USDT</span></div>
                         <label class="lt-flag"><input type="checkbox" id="lt-infinite"> Infinite Fill <span class="lt-flag-hint">re-arm after every fill until cancelled</span></label>
                       </div>
                     </details>
                     <details class="lt-fold">
                       <summary><input type="checkbox" id="lt-tp-on" onchange="ltOnTpToggle(event)"> Take Profit</summary>
                       <div class="lt-fold-body">
                         <div class="lt-row">
                           <label class="lt-lbl">Trigger Spread</label>
                           <div class="lt-input-wrap">
                             <input id="lt-tp" class="lt-input" type="number" step="0.001" placeholder="0.00">
                             <span class="lt-suffix">%</span>
                           </div>
                         </div>
                         <div class="lt-row">
                           <label class="lt-lbl">Portion (opt.)</label>
                           <div class="lt-input-wrap">
                             <input id="lt-tp-portion" class="lt-input" type="number" step="0.001" placeholder="full">
                             <span class="lt-suffix" id="lt-tp-portion-suffix">—</span>
                           </div>
                         </div>
                       </div>
                     </details>
                     <details class="lt-fold">
                       <summary><input type="checkbox" id="lt-sl-on" onchange="ltOnSlToggle(event)"> Stop Loss</summary>
                       <div class="lt-fold-body">
                         <div class="lt-row">
                           <label class="lt-lbl">Trigger Spread</label>
                           <div class="lt-input-wrap">
                             <input id="lt-sl" class="lt-input" type="number" step="0.001" placeholder="0.00">
                             <span class="lt-suffix">%</span>
                           </div>
                         </div>
                         <div class="lt-row">
                           <label class="lt-lbl">Portion (opt.)</label>
                           <div class="lt-input-wrap">
                             <input id="lt-sl-portion" class="lt-input" type="number" step="0.001" placeholder="full">
                             <span class="lt-suffix" id="lt-sl-portion-suffix">—</span>
                           </div>
                         </div>
                       </div>
                     </details>
                     <div class="lt-flags">
                       <label class="lt-flag"><input type="checkbox" id="lt-reduce"> Reduce Only</label>
                       <label class="lt-flag">
                         <input type="checkbox" id="lt-schedule-on" onchange="ltOnScheduleToggle(event)"> Start Time
                       </label>
                       <input id="lt-schedule" class="lt-input" type="datetime-local" style="display:none;flex:1;min-width:180px">
                     </div>
                     <button type="button" class="lt-submit" id="lt-submit" onclick="ltSubmit()">Place Trigger</button>
                     <div class="lt-err" id="lt-err" style="display:none"></div>
                   </section>`}

              <!-- Calculator / breakdown -->
              <div style="padding:10px;display:flex;flex-direction:column;gap:8px;border-top:1px solid var(--border);background:var(--surface2)">
                <div style="font-size:9px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;padding:0 2px">P&amp;L calculator · per 8h</div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--border);border-radius:6px;overflow:hidden">
                  <div style="background:var(--surface);padding:8px 10px">
                    <div style="font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;margin-bottom:3px">Basis</div>
                    <div class="mono" id="pt-rt-basis" style="font-size:12.5px;font-weight:600">—</div>
                  </div>
                  <div style="background:var(--surface);padding:8px 10px">
                    <div style="font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;margin-bottom:3px">Short funding</div>
                    <div class="mono" id="pt-rt-fund" style="font-size:12.5px;font-weight:600">—</div>
                  </div>
                  <div style="background:var(--surface);padding:8px 10px">
                    <div style="font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;margin-bottom:3px">Gross</div>
                    <div class="mono" id="pt-rt-gross" style="font-size:12.5px;font-weight:600">—</div>
                  </div>
                  <div style="background:var(--surface);padding:8px 10px">
                    <div style="font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;margin-bottom:3px">Fees</div>
                    <div class="mono" id="pt-rt-fees" style="font-size:12.5px;font-weight:600">—</div>
                    <div class="mono" id="pt-rt-fees-sub" style="font-size:9.5px;color:var(--text3);margin-top:2px">—</div>
                  </div>
                  <div style="background:linear-gradient(135deg,rgba(26,255,171,.07),transparent);padding:8px 10px;grid-column:1 / -1">
                    <div style="font-size:9px;color:var(--green);text-transform:uppercase;letter-spacing:.06em;margin-bottom:3px;font-weight:700">Net / 8h · APR</div>
                    <div style="display:flex;align-items:baseline;gap:6px">
                      <span class="mono" id="pt-rt-net" style="font-size:18px;font-weight:700;color:var(--green)">—</span>
                      <span class="mono" id="pt-rt-apr" style="font-size:11px;color:var(--text2)">—</span>
                    </div>
                  </div>
                </div>
                <div class="mono" id="pt-rt-fund-sub" style="font-size:9.5px;color:var(--text3);text-align:center">—</div>
              </div>
            </div>
          </div><!-- /col-info -->
    </div><!-- /.workspace -->
  `;

  // Pair-level state
  let _row = null;
  const $ = (id) => document.getElementById(id);
  const fmtPx = (p) => p == null ? '—' : p >= 1000 ? p.toLocaleString('en-US', {maximumFractionDigits: 2}) : p >= 1 ? p.toFixed(4) : p.toPrecision(4);
  const fmtPxUsd = (p) => p == null ? '—' : '$' + fmtPx(p);
  const fmtVol = (v) => {
    if (!v) return '—';
    if (v >= 1e9) return '$' + (v/1e9).toFixed(2) + 'B';
    if (v >= 1e6) return '$' + (v/1e6).toFixed(2) + 'M';
    if (v >= 1e3) return '$' + (v/1e3).toFixed(1) + 'K';
    return '$' + v.toFixed(0);
  };
  const setT = (id, t) => { const el = $(id); if (el) el.textContent = t; };
  const setCls = (id, cls) => { const el = $(id); if (el) el.className = cls; };

  function _paint(r) {
    if (!r) return;
    _row = r;
    const longLabelPlain = IS_DEX ? (r.dex_name||'').toUpperCase() : (EX_LABEL[r.spot_exchange] || r.spot_exchange);
    setT('pt-long-name', longLabelPlain);
    setT('pt-short-name', EX_LABEL[r.short_exchange] || r.short_exchange);
    setT('pt-px-long', fmtPxUsd(IS_DEX ? r.dex_price : r.spot_price));
    setT('pt-px-short', fmtPxUsd(r.perp_price));

    if (IS_DEX) {
      setT('pt-chain', (r.dex_chain||'').toUpperCase());
      setT('pt-liq', fmtVol(r.dex_liquidity_usd));
      setT('pt-dv', fmtVol(r.dex_volume_usd));
    } else {
      setT('pt-sv', fmtVol(r.spot_volume_usd));
      setT('pt-spot-exlbl', EX_LABEL[r.spot_exchange] || r.spot_exchange);
    }
    setT('pt-pv', fmtVol(r.perp_volume_usd));

    const basis = r.basis_pct || 0;
    const fund  = r.short_funding_8h || 0;
    const net   = r.net_profit || 0;
    const apr   = r.net_apr || 0;
    const gross = r.gross || 0;
    const ivl   = r.interval_h || 8;
    const fundNative = (r.funding_rate || 0) * 100; // native per-interval %

    // Infobar cards
    setT('pt-fund', `${fundNative >= 0 ? '+' : ''}${fundNative.toFixed(4)}%`);
    setCls('pt-fund', 'val ' + (fundNative >= 0 ? 'rate-pos' : 'rate-neg'));
    setT('pt-ivl', `/ ${ivl}h /`);

    // Funding / 8h — short_funding_8h is already 8h-normalised (the short
    // leg's rate over a full 8h window). Spot/dex: only the short leg
    // pays funding so we surface that directly.
    const f8h = (r.short_funding_8h != null) ? r.short_funding_8h
              : (r.gross_funding != null ? r.gross_funding : 0);
    setT('pt-funding-8h', `${f8h >= 0 ? '+' : ''}${f8h.toFixed(4)}%`);
    $('pt-funding-8h').style.color = f8h >= 0 ? 'var(--green)' : 'var(--red)';
    setT('pt-net',  `${net >= 0 ? '+' : ''}${net.toFixed(4)}%`);
    $('pt-net').style.color = net >= 0 ? 'var(--green)' : 'var(--red)';
    setT('pt-apr',  apr > 0 ? apr.toFixed(0) + '%' : '—');
    $('pt-apr').style.color = apr > 0 ? 'var(--green)' : 'var(--text2)';

    // LONG leg card (DEX or Spot)
    setT('pt-trade-long-venue', IS_DEX
      ? `${(r.dex_name||'').toUpperCase()} · ${(r.dex_chain||'').toUpperCase()}`
      : (EX_LABEL[r.spot_exchange] || r.spot_exchange || ''));
    setT('pt-trade-long-px', fmtPxUsd(IS_DEX ? r.dex_price : r.spot_price));
    // pt-trade-long-meta is DEX-only now (Liq); the spot leg shows Balance
    // in this slot and that's populated by _ptSpotFetchStatus.
    if (IS_DEX) setT('pt-trade-long-meta', fmtVol(r.dex_liquidity_usd));
    setT('pt-long-fees', `−${(IS_DEX ? (r.fee_dex||0) : (r.fee_spot||0)).toFixed(3)}%`);
    setT('pt-long-basis', `${basis >= 0 ? '+' : ''}${basis.toFixed(3)}%`);
    if (IS_DEX) {
      const swap = $('pt-dex-swap');
      if (swap && r.dex_pair_url) swap.href = r.dex_pair_url;
    }
    // Spot path: nothing to wire here — _ptSpot._submit reads LONG + SYM
    // directly when the user clicks Buy.
    // SHORT leg Last price (updates every refresh)
    setT('pt-last', fmtPx(r.perp_price));
    _ptRecalc();
    // Spot leg recalc — Buy button label needs the live price to flip
    // from "enter size" to "Buy SPOT · X TOKEN".
    // Spot-leg recalc no longer applies — lt-panel handles its own state.

    // Calculator cards — plain text + inline colour (CSS classes kept mono)
    const setCol = (id, text, isPos) => {
      const el = $(id);
      if (!el) return;
      el.textContent = text;
      el.style.color = isPos === null ? 'var(--text)' : (isPos ? 'var(--green)' : 'var(--red)');
    };
    setCol('pt-rt-basis', `${basis >= 0 ? '+' : ''}${basis.toFixed(4)}%`, basis >= 0);
    setCol('pt-rt-fund',  `${fund  >= 0 ? '+' : ''}${fund.toFixed(4)}%`,  fund  >= 0);
    setCol('pt-rt-gross', `${gross >= 0 ? '+' : ''}${gross.toFixed(4)}%`, gross >= 0);
    setCol('pt-rt-fees',  `−${(r.total_fees||0).toFixed(4)}%`, null);
    setCol('pt-rt-net',   `${net   >= 0 ? '+' : ''}${net.toFixed(4)}%`,   net   >= 0);
    setT('pt-rt-fund-sub', `native ${fundNative.toFixed(4)}% / ${ivl}h · basis from snapshots`);
    setT('pt-rt-fees-sub', IS_DEX
      ? `DEX rt ${(r.fee_dex||0).toFixed(3)}% + perp rt ${(r.fee_perp||0).toFixed(3)}%`
      : `spot rt ${(r.fee_spot||0).toFixed(3)}% + perp rt ${(r.fee_perp||0).toFixed(3)}%`);
    setT('pt-rt-apr', `APR ${apr > 0 ? apr.toFixed(1) + '%' : '—'}`);
    if (IS_DEX && r.dex_pair_url) {
      const ext = $('pt-ext-link');
      if (ext) {
        ext.href = r.dex_pair_url;
        ext.style.display = 'inline-flex';
        ext.innerHTML = `Open on DexScreener <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3h8v8M5 11L13 3"/></svg>`;
      }
    }

    // Next funding countdown — once per row fetch (stable closure)
    if (r.next_ts && !_paint._cdTs) {
      _paint._cdTs = r.next_ts;
      const tick = () => {
        const sec = Math.max(0, Math.floor((_paint._cdTs * 1000 - Date.now()) / 1000));
        const h = Math.floor(sec / 3600);
        const m = Math.floor((sec % 3600) / 60);
        const s = sec % 60;
        setT('pt-next', `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`);
      };
      tick();
      setInterval(tick, 1000);
    } else if (r.next_ts) {
      _paint._cdTs = r.next_ts;  // refresh underlying ts, countdown already ticking
    }
  }

  // DexScreener embed (DEX mode). `trades=0` hides the trades/txns panel
  // so the user sees just the price candlestick chart of the asset.
  // _setDexEmbed is also called from _refreshPair when we discover a pair
  // via DexScreener search fallback (URL didn't carry chain+pair).
  function _setDexEmbed(chain, pair) {
    if (!IS_DEX || !chain || !pair) return;
    const frame = $('pt-dex-frame');
    if (!frame) return;
    const url = `https://dexscreener.com/${chain}/${pair}?embed=1&theme=dark&trades=0&info=0`;
    if (frame.src !== url) frame.src = url;
  }
  _setDexEmbed(DEX_CHAIN, DEX_PAIR);

  // Live Entry/Exit chart for Spot / Short — candlestick series (5s buckets).
  // Each push aggregates (mid_short − mid_long) / mid_long × 100 into the
  // current bucket's OHLC. Backgrounds are transparent so the chart blends
  // into the surrounding panel — only the candles + axes are visible.
  let _eeChart = null, _eeSeries = null;
  const _eeBucketSec = 5;
  const _eeKey = `pt-ee:v2:${TYPE}:${SYM}:${LONG}:${SHORT}`;
  const _eeMax = 1200;  // ~100 minutes of 5s candles
  let _eeCandles = [];      // Array<{time, open, high, low, close}>
  let _eeCurBucket = null;  // current open candle (mutated on every push)
  function _eeInit() {
    if (IS_DEX) return;
    if (!window.LightweightCharts) {
      if (typeof _loadLightweightCharts === 'function') {
        _loadLightweightCharts().then(_eeInit).catch(()=>{});
      } else {
        setTimeout(_eeInit, 300);
      }
      return;
    }
    const el = $('pt-ee-chart');
    if (!el || _eeChart) return;
    const theme = document.body.classList.contains('light')
      ? {grid:'#EDEDED', text:'#1A1A1A', border:'#D0D0D0', up:'#006B3C', down:'#8B0000'}
      : {grid:'#1F1F28', text:'#9B9FAB', border:'#22222A', up:'#1AFFAB', down:'#F87171'};
    _eeChart = LightweightCharts.createChart(el, {
      layout: { background: {type:'solid', color:'rgba(0,0,0,0)'}, textColor: theme.text, fontSize: 10 },
      grid:   { vertLines: {color: theme.grid}, horzLines: {color: theme.grid} },
      rightPriceScale: { borderColor: theme.border, scaleMargins:{top:0.1, bottom:0.1} },
      timeScale: { borderColor: theme.border, timeVisible: true, secondsVisible: true, rightOffset: 4, barSpacing: 6 },
      crosshair: { mode: 1 },
      localization: { priceFormatter: v => (v>=0?'+':'')+(+v).toFixed(4)+'%' },
    });
    _eeSeries = _eeChart.addCandlestickSeries({
      upColor: theme.up, downColor: theme.down,
      borderUpColor: theme.up, borderDownColor: theme.down,
      wickUpColor: theme.up, wickDownColor: theme.down,
      priceFormat: { type:'price', precision: 4, minMove: 0.0001 },
    });
    const ro = new ResizeObserver(() => {
      _eeChart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
    });
    ro.observe(el);
    _eeChart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
    try {
      const raw = localStorage.getItem(_eeKey);
      if (raw) {
        const arr = JSON.parse(raw);
        if (Array.isArray(arr) && arr.length && arr[0].open !== undefined) {
          const cutoff = Math.floor(Date.now()/1000) - 1200;
          _eeCandles = arr.filter(c => c.time >= cutoff).slice(-_eeMax);
          if (_eeCandles.length) {
            _eeSeries.setData(_eeCandles);
            _eeCurBucket = _eeCandles[_eeCandles.length-1];
            const empty = $('pt-ee-empty'); if (empty) empty.style.display = 'none';
            setT('pt-ee-count', _eeCandles.length + ' bars');
          }
        }
      }
    } catch {}
    setInterval(() => {
      if (document.hidden) return;
      try { localStorage.setItem(_eeKey, JSON.stringify(_eeCandles.slice(-_eeMax))); } catch {}
    }, 3000);
  }
  _eeInit();
  function _eePush(spreadPct) {
    if (!_eeSeries) return;
    const v = +spreadPct;
    if (!isFinite(v)) return;
    const t = Math.floor(Date.now()/1000 / _eeBucketSec) * _eeBucketSec;
    if (!_eeCurBucket || _eeCurBucket.time !== t) {
      _eeCurBucket = { time: t, open: v, high: v, low: v, close: v };
      _eeCandles.push(_eeCurBucket);
      if (_eeCandles.length > _eeMax) _eeCandles.splice(0, _eeCandles.length - _eeMax);
    } else {
      if (v > _eeCurBucket.high) _eeCurBucket.high = v;
      if (v < _eeCurBucket.low)  _eeCurBucket.low  = v;
      _eeCurBucket.close = v;
    }
    _eeSeries.update(_eeCurBucket);
    const empty = $('pt-ee-empty');
    if (empty && empty.style.display !== 'none') empty.style.display = 'none';
    setT('pt-ee-count', _eeCandles.length + ' bars');
  }

  // Build the pair row from independent sources — top-200 opps cache often
  // doesn't contain our exact pair, so rely on primary endpoints directly.
  async function _fetchDexDirect() {
    // Path A: address known → /tokens/<addr> (deterministic)
    // Path B: no address → /search?q=<symbol> (best-effort, picks the
    //         highest-liquidity match on the requested chain). Without
    //         this fallback the DEX card stayed empty whenever the
    //         screener row didn't have dex_base_address populated.
    let pairs = [];
    try {
      if (DEX_ADDR) {
        const r = await fetch(`https://api.dexscreener.com/latest/dex/tokens/${DEX_ADDR}`, {cache: 'no-store'});
        if (r.ok) {
          const j = await r.json();
          pairs = j.pairs || [];
        }
      }
      if (pairs.length === 0 && SYM) {
        const r = await fetch(`https://api.dexscreener.com/latest/dex/search?q=${encodeURIComponent(SYM)}`, {cache: 'no-store'});
        if (r.ok) {
          const j = await r.json();
          pairs = j.pairs || [];
        }
      }
    } catch { return null; }
    if (!pairs.length) return null;
    try {
      const ACCEPTED = new Set(['USDC','USDT','DAI','BUSD','USDC.E','FDUSD','PYUSD','WETH','WBTC','WBNB','WSOL','SOL','ETH','BNB','MATIC','WMATIC']);
      const symU = (SYM || '').toUpperCase();
      let best = null, bestLiq = 0;
      for (const p of pairs) {
        if (DEX_CHAIN && (p.chainId || '') !== DEX_CHAIN) continue;
        const base = p.baseToken || {};
        // If we have an address (path A) require exact match; if we don't
        // (path B — symbol search) require base symbol to match instead.
        if (DEX_ADDR) {
          if ((base.address || '').toLowerCase() !== DEX_ADDR) continue;
        } else if (symU) {
          if ((base.symbol || '').toUpperCase() !== symU) continue;
        }
        const q = ((p.quoteToken || {}).symbol || '').toUpperCase();
        if (!ACCEPTED.has(q)) continue;
        const liq = +(((p.liquidity || {}).usd) || 0);
        const vol = +(((p.volume || {}).h24) || 0);
        const px  = +(p.priceUsd || 0);
        if (px <= 0) continue;
        if (liq > bestLiq) {
          bestLiq = liq;
          best = { price: px, liquidity_usd: liq, volume_usd: vol, dex: p.dexId || '', chain: p.chainId || '', url: p.url || '', pair_address: p.pairAddress || '' };
        }
      }
      return best;
    } catch { return null; }
  }

  async function _fetchPerpData() {
    try {
      const r = await Auth.apiFetch(`/screener/all-exchanges-funding?symbol=${SYM}`);
      if (!r.ok) return null;
      const j = await r.json();
      return (j.rates || []).find(x => x.exchange === SHORT) || null;
    } catch { return null; }
  }

  async function _fetchSpotData() {
    // Spot type: look up in /spot-arbitrage first (covers all pairs), then in
    // the long-side venue's funding row (no spot price endpoint exists today)
    try {
      const r = await Auth.apiFetch('/screener/spot-short');
      if (!r.ok) return null;
      const j = await r.json();
      return (j.opportunities || []).find(o =>
        o.symbol === SYM && o.spot_exchange === LONG && o.short_exchange === SHORT
      ) || null;
    } catch { return null; }
  }

  function _buildRow(dex, perp) {
    // Synthesize a row compatible with _paint() from DexScreener + perp feed.
    if (!dex || !perp) return null;
    const dexPx = +dex.price;
    const perpPx = +perp.price;
    const rate = +perp.rate;
    const ivl = +perp.interval_h || 8;
    // Short PnL from funding = signed rate (positive rate = longs pay shorts,
    // we receive; negative rate = shorts pay longs, we pay). See same fix in
    // backend/services/spot_arbitrage_service.py.
    const short_funding_8h = rate * (8 / ivl) * 100;
    const basis_pct = (perpPx - dexPx) / dexPx * 100;
    const gross = short_funding_8h + basis_pct;
    const PERP_FEE = { binance:.04, bybit:.055, okx:.05, gate:.05, kucoin:.06, mexc:.02, bitget:.06, hyperliquid:.035, aster:.05, bingx:.05, whitebit:.06 };
    const fee_perp = (PERP_FEE[SHORT] || .06) * 2;
    const fee_dex  = IS_DEX ? 0.8 : 0;
    const total_fees = fee_dex + fee_perp;
    const net_profit = gross - total_fees;
    const net_apr = net_profit > 0 ? net_profit * 3 * 365 : 0;
    return {
      type: IS_DEX ? 'dex_short' : 'spot_short',
      symbol: SYM,
      short_exchange: SHORT,
      perp_price: perpPx,
      perp_volume_usd: perp.volume_usd || 0,
      funding_rate: rate,
      interval_h: ivl,
      next_ts: perp.next_ts || 0,
      short_funding_8h,
      basis_pct,
      gross,
      fee_dex, fee_perp, fee_spot: 0, total_fees,
      net_profit, net_apr,
      // DEX-specific
      dex_chain: dex.chain || '',
      dex_name: dex.dex || '',
      dex_pair_url: dex.url || '',
      dex_pair_address: dex.pair_address || '',
      dex_price: IS_DEX ? dexPx : undefined,
      spot_price: IS_DEX ? undefined : dexPx,
      dex_liquidity_usd: dex.liquidity_usd || 0,
      dex_volume_usd: dex.volume_usd || 0,
      spot_volume_usd: dex.volume_usd || 0,
      spot_exchange: IS_DEX ? undefined : LONG,
    };
  }

  async function _refreshPair() {
    try {
      let row = null;
      if (IS_DEX) {
        const [dex, perp] = await Promise.all([_fetchDexDirect(), _fetchPerpData()]);
        row = _buildRow(dex, perp);
        // If we resolved chain+pair via search fallback (URL had no
        // pair param), load the embed iframe NOW that we know which
        // pair to chart.
        if (row && !DEX_PAIR && row.dex_chain && row.dex_pair_address) {
          _setDexEmbed(row.dex_chain, row.dex_pair_address);
        }
      } else {
        row = await _fetchSpotData();
        if (!row) {
          // Fallback: synthesize from all-exchanges-funding for both legs
          const perp = await _fetchPerpData();
          const longR = perp && SHORT !== LONG
            ? null // No direct spot endpoint, leave null
            : null;
          if (perp) {
            // Use perp price as proxy for spot when we truly have nothing else,
            // so the page isn't blank. Clearly degraded case.
            row = _buildRow({ price: perp.price, volume_usd: perp.volume_usd }, perp);
          }
        }
      }
      if (row) _paint(row);
    } catch (e) { console.error('[pair] refresh error:', e); }
  }
  _refreshPair();
  // Per-pair metadata refresh. DEX needs polling (no WS feed for
  // DexScreener pools); futures L/S has /ws/long-short feeding live
  // opp diffs every ~200ms server-side, so the 1s REST poll is
  // strictly redundant data — 5s on the WS-covered path saves 4×
  // the HTTP traffic over a session (each call hits 2 endpoints).
  setInterval(() => { if (document.hidden) return; _refreshPair(); }, IS_DEX ? 1000 : 5000);

  // ── Order-book polling (futures-style .book-row rendering) ────────────────
  let _obTicks = { long: 0, short: 0 };
  let _lastMidLong = 0, _lastMidShort = 0;
  let _lastAskLong = 0, _lastBidShort = 0;

  const _fmtP = (p) => {
    const n = +p;
    if (n >= 1000) return n.toLocaleString('en-US',{maximumFractionDigits:2});
    if (n >= 1)    return n.toFixed(4);
    return n.toPrecision(5);
  };
  const _fmtQ = (q) => {
    const n = +q;
    if (n >= 1e6) return (n/1e6).toFixed(2)+'M';
    if (n >= 1e3) return (n/1e3).toFixed(1)+'K';
    return n.toFixed(n < 1 ? 4 : 2);
  };
  function _renderBook(side, asksAll, bidsAll) {
    const asksEl = $('pt-asks-' + side);
    const bidsEl = $('pt-bids-' + side);
    if (!asksEl || !bidsEl) return;
    const N = 14;
    const asks = asksAll.slice(0, N).reverse();
    const bids = bidsAll.slice(0, N);
    const maxAsk = asks.reduce((a, b) => a + (+b[1]), 0);
    const maxBid = bids.reduce((a, b) => a + (+b[1]), 0);
    const rowHtml = (arr, kind, maxTot) => {
      let cum = 0;
      return arr.map(([p, q]) => {
        cum += +q;
        const pct = Math.min(100, (cum / Math.max(1e-9, maxTot)) * 100);
        const bgCol = kind === 'ask' ? 'var(--red)' : 'var(--green)';
        const pxCls = kind === 'ask' ? 'ask-price' : 'bid-price';
        return `<div class="book-row"><div class="book-row-bg" style="width:${pct.toFixed(1)}%;background:${bgCol}"></div><span class="${pxCls}">${_fmtP(p)}</span><span class="book-amount">${_fmtQ(q)}</span><span class="book-total">${_fmtQ(cum)}</span></div>`;
      }).join('');
    };
    asksEl.innerHTML = rowHtml(asks, 'ask', maxAsk);
    bidsEl.innerHTML = rowHtml(bids, 'bid', maxBid);

    if (asksAll.length && bidsAll.length) {
      const bestAsk = +asksAll[0][0];
      const bestBid = +bidsAll[0][0];
      const mid = (bestAsk + bestBid) / 2;
      const prev = side === 'long' ? _lastMidLong : _lastMidShort;
      const arrow = mid > prev ? '▲' : mid < prev ? '▼' : '·';
      const col = mid > prev ? 'var(--green)' : mid < prev ? 'var(--red)' : 'var(--text3)';
      setT('pt-mid-' + side, _fmtP(mid));
      const a = $('pt-mid-arrow-' + side);
      if (a) { a.textContent = arrow; a.style.color = col; }
      if (side === 'long') {
        _lastMidLong = mid;
        _lastAskLong = bestAsk;
      } else {
        _lastMidShort = mid;
        _lastBidShort = bestBid;
      }

      // Live spread = In (entry basis). Prefer locally-computed value from
      // fresh top-of-book prices — updates instantly on every WS frame.
      // Falls back to _row.in_pct (REST-polled, up to 1s stale) when the
      // long book hasn't been received yet (DEX mode or cold start).
      if (side === 'short') {
        let live = null;
        if (_lastAskLong > 0 && bestBid > 0) {
          live = (bestBid - _lastAskLong) / _lastAskLong * 100;
        } else if (_row) {
          live = (typeof _row.in_pct === 'number') ? _row.in_pct
               : (typeof _row.basis_pct === 'number' ? _row.basis_pct : null);
        }
        if (live !== null) {
          setT('pt-live-spread', `${live >= 0 ? '+' : ''}${live.toFixed(4)}%`);
          const el = $('pt-live-spread');
          if (el) el.style.color = live >= 0 ? 'var(--green)' : 'var(--red)';
          if (!IS_DEX) _eePush(live);
        }
      }
    }
    _obTicks[side]++;
    setT('pt-book-' + side + '-age', `live · ${_obTicks[side]} ticks`);
  }
  async function _refreshBook(side) {
    const isSpotSide = (side === 'long' && !IS_DEX);
    const ex = side === 'long' ? LONG : SHORT;
    const path = isSpotSide
      ? `/screener/orderbook-spot?exchange=${ex}&symbol=${SYM}&limit=200`
      : `/screener/orderbook?exchange=${ex}&symbol=${SYM}&limit=200`;
    try {
      const r = await Auth.apiFetch(path);
      if (!r.ok) {
        setT('pt-book-' + side + '-age', `error ${r.status}`);
        return;
      }
      const j = await r.json();
      if ((!j.asks || !j.asks.length) && (!j.bids || !j.bids.length)) {
        setT('pt-book-' + side + '-age', 'warming up…');
        return;
      }
      _renderBook(side, j.asks || [], j.bids || []);
    } catch (e) {
      console.error('[pair] book ' + side + ' error:', e);
      setT('pt-book-' + side + '-age', 'offline');
    }
  }
  // Perp book — always present. 300 ms polling keeps the on-screen book
  // within the 0.5 s freshness budget the screener is promising now.
  // _bookInflight gate in fetchBook() ensures a slow response doesn't
  // queue requests.
  // Initial REST fetch for fast first paint, then WS for live updates.
  // The 300/400ms polling kept as a fallback when WS is silent — `_lastWsAt`
  // gates the REST fetch so we only poll when the stream's actually dead.
  _refreshBook('short');
  if (!IS_DEX) _refreshBook('long');

  // /ws/book streams the same books.json that the screener WS broadcaster
  // serves — bid/ask deltas pushed at 100-200ms cadence. Replaces the old
  // 300ms REST polling with a live feed, dropping the orderbook latency
  // from ~300ms (poll interval + RTT) to ~100ms (WS push).
  let _ptBookWs = null, _ptLastWsAt = 0, _ptWsBackoff = 1000;
  // On spot pages the LONG side is a CEX *spot* venue — its books live
  // under '<ex>_spot:<symbol>' in books.json. Without the suffix the WS
  // server hands back the futures book (or empty for spot-only tokens
  // like PAXG / XAUT) and the ladder stays blank.
  const _ptBookPair = (side) => {
    const ex = (side === 'long' ? LONG : SHORT).toLowerCase();
    const isSpotLeg = (side === 'long' && TYPE === 'spot');
    return (isSpotLeg ? ex + '_spot' : ex) + ':' + SYM.toUpperCase();
  };
  function _openPtBookWs() {
    if (_ptBookWs && (_ptBookWs.readyState === 0 || _ptBookWs.readyState === 1)) return;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const token = Auth.getToken();
    if (!token) return;
    const url = `${proto}://${location.host}/api/screener/ws/book`;
    let ws;
    try { ws = new WebSocket(url); } catch (_) { return; }
    _ptBookWs = ws;
    ws.onopen = () => {
      _ptWsBackoff = 1000;
      try {
        ws.send(JSON.stringify({ auth: token }));
        const pairs = IS_DEX ? [_ptBookPair('short')] : [_ptBookPair('long'), _ptBookPair('short')];
        ws.send(JSON.stringify({ action: 'subscribe', pairs }));
      } catch (_) {}
    };
    ws.onmessage = (e) => {
      _ptLastWsAt = Date.now();
      let msg; try { msg = JSON.parse(e.data); } catch (_) { return; }
      const books = msg && msg.books;
      if (!books) return;
      const longPair = _ptBookPair('long'), shortPair = _ptBookPair('short');
      if (!IS_DEX && books[longPair]) {
        _renderBook('long', books[longPair].asks || [], books[longPair].bids || []);
      }
      if (books[shortPair]) {
        _renderBook('short', books[shortPair].asks || [], books[shortPair].bids || []);
      }
    };
    ws.onclose = () => {
      _ptBookWs = null;
      setTimeout(_openPtBookWs, Math.min(_ptWsBackoff, 10000));
      _ptWsBackoff = Math.min(_ptWsBackoff * 2, 10000);
    };
    ws.onerror = () => { try { ws.close(); } catch (_) {} };
  }
  _openPtBookWs();
  // REST fallback — fires only when WS has been silent >3s. Live stream
  // overrides at 100-200ms cadence; this kicks in only on disconnect.
  setInterval(() => {
    if (document.hidden) return;
    const silent = Date.now() - _ptLastWsAt > 3000;
    if (silent) _refreshBook('short');
    if (silent && !IS_DEX) _refreshBook('long');
  }, 1000);

  // ── Trade panel (short leg on CEX perp) ───────────────────────────────────
  const _pt = {
    wallet_id: null, status: 'missing', balance: null,
    leverage: 3, margin: 'isolated',
    unit: 'token', size: 0,
    lastPerp: 0,
  };
  function _ptSpotUrl(ex, sym) {
    const s = (sym||'').toUpperCase();
    const url = ({
      binance: `https://www.binance.com/en/trade/${s}_USDT`,
      bybit:   `https://www.bybit.com/en/trade/spot/${s}/USDT`,
      okx:     `https://www.okx.com/trade-spot/${s.toLowerCase()}-usdt`,
      gate:    `https://www.gate.com/trade/${s}_USDT`,
      kucoin:  `https://www.kucoin.com/trade/${s}-USDT`,
      mexc:    `https://www.mexc.com/exchange/${s}_USDT`,
      bitget:  `https://www.bitget.com/spot/${s}USDT`,
      bingx:   `https://bingx.com/spot/${s}USDT`,
    })[ex];
    return url || '#';
  }
  async function _ptFetchStatus() {
    try {
      const r = await Auth.apiFetch(`/trade/status?symbol=${SYM}&long_ex=${SHORT}&short_ex=${SHORT}`);
      if (!r.ok) return;
      const j = await r.json();
      const s = j.short || {};
      _pt.wallet_id = s.wallet_id || null;
      _pt.status = s.status || 'missing';
      _pt.balance = s.balance_usdt;
      const st = $('pt-trade-status');
      if (st) {
        if (_pt.wallet_id) {
          st.textContent = 'ok';
          st.className = 'trade-leg-status ok';
        } else {
          st.textContent = s.status === 'admin_blocked' ? 'blocked' : 'no keys';
          st.className = 'trade-leg-status ' + (s.status === 'admin_blocked' ? 'disabled' : 'missing');
        }
      }
      setT('pt-bal', _pt.balance != null ? (+_pt.balance).toFixed(2) + ' USDT' : '— USDT');
      _ptRecalc();
    } catch { /* ignore */ }
  }
  window._pt_setMM = function (v) {
    _pt.margin = v;
    $('pt-mm-iso').classList.toggle('is-active', v === 'isolated');
    $('pt-mm-cross').classList.toggle('is-active', v === 'cross');
    $('pt-mm-iso').style.background  = v === 'isolated' ? 'var(--surface3)' : 'var(--surface2)';
    $('pt-mm-iso').style.color       = v === 'isolated' ? 'var(--text)'     : 'var(--text3)';
    $('pt-mm-cross').style.background= v === 'cross'    ? 'var(--surface3)' : 'var(--surface2)';
    $('pt-mm-cross').style.color     = v === 'cross'    ? 'var(--text)'     : 'var(--text3)';
    _ptRecalc();
  };
  window._pt_lev = function (d) {
    _pt.leverage = Math.max(1, Math.min(100, _pt.leverage + d));
    setT('pt-lev-val', _pt.leverage + '×');
    _ptRecalc();
  };
  window._pt_setUnit = function (u) {
    if (u === _pt.unit) return;
    // Convert the value already in the size input so the user doesn't have
    // to retype after switching token↔USDT.
    const inp = $('pt-size');
    const cur = parseFloat((inp && inp.value) || '0') || 0;
    const px = _row ? (+_row.perp_price || 0) : 0;
    if (cur > 0 && px > 0 && inp) {
      if (_pt.unit === 'token' && u === 'usdt') inp.value = (cur * px).toFixed(2);
      else if (_pt.unit === 'usdt' && u === 'token') inp.value = (cur / px).toFixed(6);
      _pt.size = parseFloat(inp.value) || 0;
    }
    _pt.unit = u;
    $('pt-unit-tok').style.background = u === 'token' ? 'var(--surface3)' : 'transparent';
    $('pt-unit-tok').style.color      = u === 'token' ? 'var(--text)'     : 'var(--text3)';
    $('pt-unit-usd').style.background = u === 'usdt'  ? 'var(--surface3)' : 'transparent';
    $('pt-unit-usd').style.color      = u === 'usdt'  ? 'var(--text)'     : 'var(--text3)';
    if (inp) inp.placeholder = u === 'token' ? `Size · ${SYM}` : 'Size · USDT';
    _ptRecalc();
  };
  window._pt_updateCalc = function () {
    _pt.size = parseFloat(($('pt-size').value || '0'));
    _ptRecalc();
  };
  window._pt_slide = function (pct) {
    const p = Math.max(0, Math.min(100, +pct));
    $('pt-slider').value = p;
    const bal = +_pt.balance || 0;
    const px = +(_row && _row.perp_price) || 0;
    if (!bal || !px) return;
    const notional = bal * (p / 100) * _pt.leverage;
    const tokQty = notional / px;
    if (_pt.unit === 'token') {
      $('pt-size').value = tokQty > 0 ? (+tokQty.toFixed(6)).toString() : '';
    } else {
      $('pt-size').value = notional > 0 ? (+notional.toFixed(2)).toString() : '';
    }
    _pt.size = +($('pt-size').value || 0);
    _ptRecalc();
  };
  function _ptRecalc() {
    const px = _row ? (+_row.perp_price || 0) : 0;
    _pt.lastPerp = px;
    const sz = +_pt.size;
    let tokQty = 0, notional = 0;
    if (sz > 0 && px > 0) {
      if (_pt.unit === 'token') { tokQty = sz; notional = sz * px; }
      else { notional = sz; tokQty = sz / px; }
    }
    const margin = _pt.leverage > 0 ? notional / _pt.leverage : 0;
    setT('pt-notional', notional > 0 ? notional.toFixed(2) + ' USDT' : '—');
    setT('pt-margin', margin > 0 ? margin.toFixed(2) + ' USDT' : '—');
    const btn = $('pt-submit');
    if (btn) {
      const ok = !!_pt.wallet_id && tokQty > 0;
      btn.disabled = !ok;
      if (!_pt.wallet_id) btn.textContent = 'Open Short · add trade keys';
      else if (!tokQty)   btn.textContent = 'Open Short · enter size';
      else btn.textContent = `Open Short · ${tokQty.toFixed(6).replace(/0+$/,'').replace(/\.$/,'')} ${SYM}`;
      btn.style.opacity = ok ? '1' : '0.55';
    }
  }
  window._pt_submit = async function () {
    if (!_pt.wallet_id) return;
    const px = _pt.lastPerp;
    if (!px) return;
    const tokQty = _pt.unit === 'token' ? +_pt.size : (+_pt.size / px);
    if (tokQty <= 0) return;
    const btn = $('pt-submit');
    const err = $('pt-err');
    if (err) err.style.display = 'none';
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Opening…';
    try {
      const r = await Auth.apiFetch('/trade/open', {
        method: 'POST',
        body: JSON.stringify({
          wallet_id: _pt.wallet_id,
          symbol: SYM,
          side: 'sell',
          quantity: tokQty,
          leverage: _pt.leverage,
          margin_mode: _pt.margin,
        }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
      if (window.toast) toast(`Short opened · ${SYM} × ${tokQty.toFixed(6).replace(/0+$/,'').replace(/\.$/,'')}`, 'success');
      btn.textContent = '✓ Opened';
      setTimeout(() => { btn.textContent = original; }, 2000);
    } catch (e) {
      if (err) { err.textContent = String(e.message || e); err.style.display = 'block'; }
      btn.textContent = original;
    } finally {
      btn.disabled = false;
      _ptFetchStatus();
    }
  };

  _ptFetchStatus();
  // 15s → 5s: trade status (balance + key status) — trader должен видеть
  // свежий balance после внешних движений (deposit/withdraw на venue
  // напрямую). /trade/status server-side кеш дальше cap'ит cost.
  setInterval(() => { if (document.hidden) return; _ptFetchStatus(); }, 5000);

  // Spot/short trade UI is now driven by the unified lt-panel (rendered
  // inside the spot/dex body template). lt-panel auto-detects pair_kind
  // from URL ?type= in ltInit() and handles balances, leverage caps,
  // triggers, TP/SL, portion size, schedule — same code path as futures.
  // No spot-specific JS needed here.

  // ── Open positions list for spot/dex mode (Close + Share buttons) ──
  // Pull positions for the SHORT-leg exchange (perp). The long leg is
  // either a spot purchase (no API position concept) or a DEX hold,
  // neither queryable from /trade/positions, so we only surface the
  // perp leg here.
  let _ptPosLast = null, _ptPosEmpty = 0;
  async function _ptLoadOpenPositions(){
    let rows;
    try {
      const r = await Auth.apiFetch('/trade/positions?symbol=' + SYM);
      if (!r.ok) return;
      rows = await r.json();
    } catch { return; }
    rows = (rows || []).filter(r => (r.exchange || '').toLowerCase() === SHORT.toLowerCase()
                                   && (r.symbol || '').toUpperCase() === SYM.toUpperCase());
    const wrap = $('pt-positions-wrap');
    const list = $('pt-positions-list');
    if (!wrap || !list) return;
    if (!rows.length){
      _ptPosEmpty++;
      if (_ptPosLast && _ptPosEmpty < 3) return;  // sticky during reconnect
      _ptPosLast = [];
      wrap.style.display = 'none';
      return;
    }
    _ptPosEmpty = 0;
    _ptPosLast = rows;
    wrap.style.display = 'block';
    const html = rows.map(p => {
      const qty = Number(p.quantity || 0);
      const entry = Number(p.entry_price || 0);
      const mark = Number(p.mark_price || 0);
      const pnl = Number(p.unrealized_pnl_usd || 0);
      const pnlPct = (entry > 0 && qty > 0) ? (pnl / (entry * qty) * 100) : 0;
      const pnlCls = pnl >= 0 ? 'rate-pos' : 'rate-neg';
      const sideTxt = p.side === 'buy' ? 'LONG' : 'SHORT';
      const sideCol = p.side === 'buy' ? 'var(--green)' : 'var(--red)';
      const shareData = JSON.stringify({
        symbol: p.symbol, exchange: p.exchange, side: p.side,
        quantity: qty, entry_price: entry, mark_price: mark,
        leverage: Number(p.leverage || 1), margin_mode: p.margin_mode,
        unrealized_pnl_usd: pnl, pnl_pct: pnlPct,
      }).replace(/'/g, '&#39;');
      return `
        <div style="display:flex;align-items:center;gap:8px;padding:6px 8px;background:var(--surface);border:1px solid var(--border);border-radius:6px;font-size:11px">
          <span style="color:${sideCol};font-weight:700;font-size:10px">${sideTxt}</span>
          <span class="mono" style="color:var(--text2)">${qty.toFixed(4)} ${SYM}</span>
          <span class="mono ${pnlCls}" style="margin-left:auto">${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}</span>
          <button title="Share" data-share='${shareData}' onclick='_openShareFromBtn(this)'
                  style="background:transparent;border:1px solid var(--border);color:var(--green);padding:3px 7px;border-radius:5px;cursor:pointer;font-size:11px">↗</button>
          <button onclick="tradeClose(${p.wallet_id}, '${p.position_id || p.symbol}')"
                  style="background:transparent;border:1px solid var(--border);color:var(--text3);padding:3px 8px;border-radius:5px;cursor:pointer;font-size:10.5px;font-family:inherit">Close</button>
        </div>`;
    }).join('');
    if (typeof _renderIfChanged === 'function') _renderIfChanged('pt-positions-list', html);
    else list.innerHTML = html;
  }
  _ptLoadOpenPositions();
  // 8s → 3s: open positions list — trader следит за PnL/SL в реальном
  // времени. Server-side positions cache (15s TTL) cap'ит venue load.
  setInterval(() => { if (document.hidden) return; _ptLoadOpenPositions(); }, 3000);

  // ── Account block (Positions / Orders / P&L / Balances) ──────────────────
  window._ptAccSwitch = function (el) {
    const pane = el.dataset.pane;
    document.querySelectorAll('.acc-tab').forEach(t => t.classList.toggle('is-active', t === el));
    document.querySelectorAll('.acc-pane').forEach(p => p.classList.toggle('is-active', p.id === 'acc-pane-' + pane));
  };
  async function _ptLoadKeyCounts() {
    try {
      const r = await Auth.apiFetch('/wallets');
      if (!r.ok) return;
      const rows = await r.json();
      // Trading view counts only the screener-purpose keys, not the
      // portfolio-only ones — those don't go through trade adapters.
      const ex = rows.filter(w => w.wallet_type === 'exchange' && !w.is_archived
                                   && (w.purpose === 'screener' || w.purpose === 'both'));
      const tr = ex.filter(w => w.can_trade).length;
      const ro = ex.length - tr;
      const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
      set('acc-ro-count', ro);
      set('acc-tr-count', tr);
      set('acc-ro-count-pnl', ro);
      set('acc-tr-count-pnl', tr);
    } catch {}
  }
  // Pair-detect: same greedy USD-notional match the futures view uses.
  // Two positions on the same symbol on opposite sides whose notional
  // (qty × mark) is within 10 % of each other count as a paired arb leg
  // and render as a collapsible "⇆ PAIR" header — the spot/short flow
  // produces those by design (buy spot + short perp), so showing them
  // un-grouped misrepresents the trade.
  const _ptPairOpen = new Set();
  function _ptPairToggle(key) {
    if (_ptPairOpen.has(key)) _ptPairOpen.delete(key);
    else                      _ptPairOpen.add(key);
    _ptLoadOpenPositions();
  }
  window._ptPairToggle = _ptPairToggle;

  function _ptPairPositions(rows) {
    const tagged = rows.map((p, i) => ({
      p,
      key: p.position_id || `${p.exchange}:${p.symbol}:${i}`,
      notional: Math.abs(Number(p.quantity || 0) * Number(p.mark_price || 0)),
    }));
    const bySym = {};
    for (const t of tagged) (bySym[t.p.symbol] = bySym[t.p.symbol] || []).push(t);
    const pairs = [];
    const used = new Set();
    for (const group of Object.values(bySym)) {
      const longs  = group.filter(t => (t.p.side || '').toLowerCase() === 'buy');
      const shorts = group.filter(t => (t.p.side || '').toLowerCase() === 'sell');
      const cands = [];
      for (const l of longs) for (const s of shorts) {
        const maxN = Math.max(l.notional, s.notional);
        if (maxN <= 0) continue;
        const diffPct = Math.abs(l.notional - s.notional) / maxN;
        if (diffPct > 0.10) continue;
        cands.push({l, s, diffPct});
      }
      cands.sort((a, b) => a.diffPct - b.diffPct);
      for (const c of cands) {
        if (used.has(c.l.key) || used.has(c.s.key)) continue;
        used.add(c.l.key); used.add(c.s.key);
        pairs.push({symbol: c.l.p.symbol, long: c.l.p, short: c.s.p});
      }
    }
    const singles = tagged.filter(t => !used.has(t.key)).map(t => t.p);
    return {pairs, singles};
  }

  async function _ptLoadAccPositions() {
    try {
      const r = await Auth.apiFetch('/trade/positions');
      if (!r.ok) return;
      const j = await r.json();
      const arr = Array.isArray(j) ? j : (j.positions || []);
      const isStale = r.headers.get('x-positions-stale') === '1';
      const tb = document.getElementById('acc-positions-body');
      const em = document.getElementById('acc-positions-empty');
      const cnt = document.getElementById('acc-cnt-positions');
      const cntPnl = document.getElementById('acc-pos-count');
      // Show/clear stale badge next to the Positions tab count
      const staleEl = document.getElementById('acc-positions-stale');
      if (staleEl) staleEl.style.display = isStale ? '' : 'none';
      if (cnt) cnt.textContent = arr.length;
      if (cntPnl) cntPnl.textContent = arr.length;
      if (!arr.length) {
        if (tb) tb.innerHTML = '';
        if (em) em.style.display = '';
        return;
      }
      if (em) em.style.display = 'none';
      const fmtPx = (p) => p == null ? '—' : (+p).toFixed(6).replace(/0+$/,'').replace(/\.$/,'');
      const fmtPct = (v) => v == null ? '—' : `${v >= 0 ? '+' : ''}${(+v).toFixed(2)}%`;
      const fmtUsd = (v) => v == null ? '—' : `${v >= 0 ? '+' : ''}$${Math.abs(+v).toFixed(2)}`;
      let totalUpnl = 0;

      const rowFor = (p) => {
        const side = (p.side || '').toLowerCase();
        const sideCls = side === 'buy' ? 'pos' : 'neg';
        const sideTxt = side === 'buy' ? 'LONG' : 'SHORT';
        const upnl = +(p.unrealized_pnl_usd || 0);
        const upnlPct = p.entry_price && p.quantity ? (upnl / (p.entry_price * p.quantity) * 100) : null;
        return `<tr>
          <td>${p.symbol}</td>
          <td>${EX_LABEL[p.exchange]||p.exchange}</td>
          <td class="${sideCls}">${sideTxt}</td>
          <td class="num">${fmtPx(p.quantity)}</td>
          <td class="num">${fmtPx(p.entry_price)}</td>
          <td class="num">${fmtPx(p.mark_price)}</td>
          <td class="num ${(+p.funding_pnl_usd||0) >= 0 ? 'pos' : 'neg'}">${fmtUsd(p.funding_pnl_usd)}</td>
          <td class="num ${upnl >= 0 ? 'pos' : 'neg'}">${fmtUsd(upnl)}</td>
          <td class="num ${upnl >= 0 ? 'pos' : 'neg'}">${fmtPct(upnlPct)}</td>
          <td></td>
        </tr>`;
      };

      const {pairs, singles} = _ptPairPositions(arr);

      const pairHtml = pairs.map(pair => {
        const lp = +(pair.long.unrealized_pnl_usd || 0);
        const sp = +(pair.short.unrealized_pnl_usd || 0);
        const tPnl = lp + sp;
        const lf = +(pair.long.funding_pnl_usd || 0);
        const sf = +(pair.short.funding_pnl_usd || 0);
        const hasFunding = pair.long.funding_pnl_usd != null || pair.short.funding_pnl_usd != null;
        const tFund = hasFunding ? lf + sf : null;
        const lN = (+pair.long.quantity  || 0) * (+pair.long.mark_price  || 0);
        const sN = (+pair.short.quantity || 0) * (+pair.short.mark_price || 0);
        const legUsd = (lN + sN) / 2;
        const pnlCls = tPnl >= 0 ? 'pos' : 'neg';
        const fundCls = hasFunding && tFund >= 0 ? 'pos' : 'neg';
        const combinedPct = legUsd > 0 ? (tPnl / legUsd * 100) : 0;
        const lE = +pair.long.entry_price || 0;
        const sE = +pair.short.entry_price || 0;
        const entrySpread = (lE > 0 && sE > 0) ? ((sE - lE) / lE * 100) : null;
        const entrySpreadTxt = entrySpread != null
          ? `<span class="${entrySpread>=0?'pos':'neg'}">${entrySpread>=0?'+':''}${entrySpread.toFixed(4)}%</span>`
          : '<span style="color:var(--text3)">—</span>';
        const pairKey = `${pair.symbol}:${pair.long.exchange}:${pair.short.exchange}`;
        const isOpen = _ptPairOpen.has(pairKey);
        const caret = isOpen ? '▾' : '▸';
        totalUpnl += tPnl;
        const header = `
          <tr style="background:rgba(26,255,171,0.04);border-top:1px solid rgba(26,255,171,0.18);cursor:pointer;user-select:none"
              onclick="window._ptPairToggle('${pairKey}')">
            <td colspan="2" style="padding:8px 10px">
              <span style="color:var(--green);font-family:monospace;margin-right:6px">${caret}</span>
              <span style="color:var(--green);font-size:10px;font-weight:700;letter-spacing:0.04em">⇆ ${_pairModeLabel(pair)}</span>
              <span style="margin-left:6px;font-weight:600">${pair.symbol}</span>
              <span style="color:var(--text3);margin-left:10px;font-size:11px">${EX_LABEL[pair.long.exchange]||pair.long.exchange} ⇄ ${EX_LABEL[pair.short.exchange]||pair.short.exchange}</span>
            </td>
            <td colspan="2" class="num" style="color:var(--text2);font-size:11px">${legUsd.toFixed(2)} USDT / leg<br><span style="color:var(--text3);font-size:10px">entry spread ${entrySpreadTxt}</span></td>
            <td colspan="2" class="num" style="color:var(--text3);font-size:11px">Δ pair</td>
            <td class="num">${hasFunding ? `<span class="${fundCls}">${fmtUsd(tFund)}</span>` : '<span style="color:var(--text3)">—</span>'}</td>
            <td class="num ${pnlCls}" style="font-weight:700">${fmtUsd(tPnl)}</td>
            <td class="num ${pnlCls}">${combinedPct>=0?'+':''}${combinedPct.toFixed(2)}%</td>
            <td></td>
          </tr>`;
        const legs = isOpen ? (rowFor(pair.long) + rowFor(pair.short)) : '';
        return header + legs;
      }).join('');

      const singlesHtml = singles.map(p => {
        const upnl = +(p.unrealized_pnl_usd || 0);
        totalUpnl += upnl;
        return rowFor(p);
      }).join('');

      if (tb) tb.innerHTML = pairHtml + singlesHtml;
      const upnlEl = document.getElementById('acc-upnl');
      if (upnlEl) upnlEl.textContent = fmtUsd(totalUpnl);
    } catch (e) { console.error('[pair-acc] positions:', e); }
  }
  async function _ptLoadBalances() {
    try {
      const r = await Auth.apiFetch('/trade/balances');
      if (!r.ok) return;
      const rows = await r.json();
      const tb = document.getElementById('acc-balances-body');
      const em = document.getElementById('acc-balances-empty');
      const cnt = document.getElementById('acc-cnt-balances');
      if (cnt) cnt.textContent = rows.length;
      if (!rows.length) {
        if (tb) tb.innerHTML = '';
        if (em) em.style.display = '';
        return;
      }
      if (em) em.style.display = 'none';
      if (tb) tb.innerHTML = rows.map(w => {
        const keyType = w.can_trade
          ? '<span class="pill tr" style="font-size:10px"><span class="pill-dot"></span>Trade</span>'
          : '<span class="pill ro" style="font-size:10px"><span class="pill-dot"></span>Read-only</span>';
        return `<tr>
          <td>${EX_LABEL[w.exchange]||w.exchange}</td>
          <td>${w.name||''}</td>
          <td>${keyType}</td>
          <td class="num">${_renderBalCell(w)}</td>
        </tr>`;
      }).join('');
    } catch (e) { console.error('[pair-acc] balances:', e); }
  }
  async function _ptLoadOrders() {
    try {
      const r = await Auth.apiFetch('/trade/orders?limit=50');
      if (!r.ok) return;
      const rows = await r.json();
      _renderOrderHistory(rows);
    } catch (e) { console.error('[pair-acc] orders:', e); }
  }

  async function _ptLoadAcc() {
    const tasks = [_ptLoadKeyCounts(), _ptLoadAccPositions(), _ptLoadBalances(), _ptLoadOrders(), _refreshPairDecisions()];
    if (typeof accLoadTriggers === 'function') tasks.push(accLoadTriggers());
    await Promise.all(tasks);
    if (typeof accLoadPnl === 'function') accLoadPnl();
  }
  // One-time migration of legacy localStorage Sync entries on first load.
  if (typeof _migrateLegacyManualPairs === 'function') _migrateLegacyManualPairs();
  _ptLoadAcc();
  // 30s — backend is WS-fed by 11 user-stream adapters + reconcile worker.
  // 10s polling was needed when REST was authoritative; now it's just load.
  // 30s → 10s: account info (key validity, account name etc) — реже
  // меняется но 30s было слишком медленно после deposit/key rotation.
  setInterval(() => { if (document.hidden) return; _ptLoadAcc(); }, 10000);
  // Tighter cadence on triggers — new fires need to surface within seconds
  if (typeof accLoadTriggers === 'function') setInterval(() => { if (document.hidden) return; accLoadTriggers(); }, 5000);
  // Per-user push channel: instant refresh on trigger/position state change
  if (typeof _connectPositionsWS === 'function') _connectPositionsWS();

  throw new Error('_arb_type_gate');  // stop the rest of this futures-specific script
}

let _opp=null, _longPrices=[], _shortPrices=[], _longHist=[], _shortHist=[];
let _tf='1h', _infoPeriodH=1, _spreadBuilt=false, _fundBuilt=false;

// ── sessionStorage snapshot cache for instant paint on return ──
const _SS_KEY=`arb:${SYM}|${LONG}|${SHORT}`;
const _SS_TTL=30_000;   // 30s
function _ssLoad(){
  try{
    const raw=sessionStorage.getItem(_SS_KEY); if(!raw) return null;
    const j=JSON.parse(raw);
    if(Date.now()-j.ts>_SS_TTL) return null;
    return j.opp;
  }catch{return null;}
}
function _ssSave(o){try{sessionStorage.setItem(_SS_KEY,JSON.stringify({ts:Date.now(),opp:o}));}catch{}}

// ── Helpers ───────────────────────────────────────────────────────────────────
const setH=(id,html)=>{const e=document.getElementById(id);if(e)e.innerHTML=html;};
const setT=(id,txt)=>{const e=document.getElementById(id);if(e)e.textContent=txt;};
const sign=v=>v>=0?'+':'';
const fmtP=v=>{if(!v&&v!==0)return'—';if(v>=10000)return'$'+Math.round(v).toLocaleString('en-US');if(v>=1)return'$'+(+v).toFixed(4);return'$'+(+v).toFixed(6);};
const fmtV=v=>{if(!v)return'0';if(v>=1e9)return(v/1e9).toFixed(2)+'B';if(v>=1e6)return(v/1e6).toFixed(2)+'M';if(v>=1e3)return(v/1e3).toFixed(1)+'K';return(+v).toFixed(1);};
const fmtU=v=>'$'+(+v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const fmtDate=ts=>{const d=new Date(ts*1000);return d.toLocaleDateString('en-US',{month:'short',day:'numeric'})+' '+d.toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',hour12:false});};
const fmtDateFull=ts=>{const d=new Date(ts*1000);return d.toLocaleDateString('ru-RU')+', '+d.toLocaleTimeString('ru-RU');};
const fmtCD=ts=>{if(!ts)return'—';const s=ts-Math.floor(Date.now()/1000);if(s<=0)return'00:00:00';const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),ss=s%60;return`${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(ss).padStart(2,'0')}`;};
function fmtBookP(v){if(!v)return'—';if(v>=10000)return v.toLocaleString('en-US',{maximumFractionDigits:1});if(v>=1)return(+v).toFixed(4);return(+v).toFixed(6);}
// Classify a balance-fetch error string into a short, human label. The full
// message stays in the tooltip — this is just the inline tag so users see
// "IP block" / "bad key" instead of an opaque "err".
function _walletBalErrLabel(msg){
  if(!msg) return 'fail';
  const s = String(msg).toLowerCase();
  if(s.includes('whitelist') || s.includes('ip address') || s.includes('ip not') || s.includes('unmatched ip')) return 'IP block';
  if(s.includes('invalid api') || s.includes('api-key') || s.includes('api key') || s.includes('bad key')) return 'bad key';
  if(s.includes('signature') || s.includes('sign mismatch') || s.includes('bad signature')) return 'bad sig';
  if(s.includes('permission') || s.includes('insufficient') || s.includes('not allowed')) return 'no perm';
  if(s.includes('rate') || s.includes('too many') || s.includes('429')) return 'rate-limit';
  if(s.includes('timeout') || s.includes('timed out')) return 'timeout';
  if(s.includes('network') || s.includes('connect') || s.includes('econnrefused')) return 'no net';
  if(s.includes('expired') || s.includes('disabled')) return 'key off';
  return 'fail';
}
function _walletBalErrAttr(msg){return String(msg||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

// Mobile-only collapse for the order-book column. CSS hides .books-row by
// default at <=900px and reveals it when .col-books carries .is-open.
function _toggleBooksCol(btn){
  const col = btn.closest('.col-books');
  if (!col) return;
  const open = col.classList.toggle('is-open');
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  const lbl = btn.querySelector('span:first-child');
  if (lbl) lbl.textContent = open ? 'Hide order books' : 'Show order books';
}

// ── Tab switching ─────────────────────────────────────────────────────────────
function switchChartTab(name,el){
  document.querySelectorAll('.chart-tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(t=>t.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('pane-'+name).classList.add('active');
  if(name==='info') renderInfoStats();
  if(name==='allrates') loadAllRates();
  // Lazy-load chart data only when needed — saves ~300ms on cold init
  if(name==='pricehist') _ensurePriceHistory();
  if(name==='fundhist')  _ensureFundHistory();
  if(name==='overview')  _ensureFundHistory();    // Overview uses funding history tables
  if(name==='info')      _ensurePriceHistory();   // Info stats read prices
}
function setTf(el,tf){_tf=tf;document.querySelectorAll('.tf-btn').forEach(b=>b.classList.remove('active'));el.classList.add('active');loadPrices();}
function setInfoPeriod(h,el){_infoPeriodH=h;document.querySelectorAll('.info-period-btn').forEach(b=>b.classList.remove('active'));el.classList.add('active');renderInfoStats();}

// ── Top bar ───────────────────────────────────────────────────────────────────
function renderTopBar(){
  document.title=`${SYM} · ${EX_LABEL[LONG]||LONG} / ${EX_LABEL[SHORT]||SHORT} · avalant_`;
  setT('h-symbol',SYM+'USDT');
  setT('name-long',EX_LABEL[LONG]||LONG); setT('name-short',EX_LABEL[SHORT]||SHORT);
  document.getElementById('dot-long').className=`tb-ex-dot dot-${LONG}`;
  document.getElementById('dot-short').className=`tb-ex-dot dot-${SHORT}`;
  setT('lbl-long-ex',EX_LABEL[LONG]||LONG);
  setT('lbl-short-ex',EX_LABEL[SHORT]||SHORT);
  setT('vol-long-ex',EX_LABEL[LONG]||LONG); setT('vol-short-ex',EX_LABEL[SHORT]||SHORT);
  setT('stt-lbl-long',EX_LABEL[LONG]||LONG); setT('stt-lbl-short',EX_LABEL[SHORT]||SHORT);
  setT('ftt-lbl-long',EX_LABEL[LONG]||LONG); setT('ftt-lbl-short',EX_LABEL[SHORT]||SHORT);
  setT('fc-lbl-long',EX_LABEL[LONG]||LONG); setT('fc-lbl-short',EX_LABEL[SHORT]||SHORT);
  setT('th-long',EX_LABEL[LONG]||LONG); setT('th-short',EX_LABEL[SHORT]||SHORT);
  setT('book-long-name',EX_LABEL[LONG]||LONG); setT('book-short-name',EX_LABEL[SHORT]||SHORT);
  setT('ov-long-title',EX_LABEL[LONG]||LONG); setT('ov-short-title',EX_LABEL[SHORT]||SHORT);
  setT('ov-long-hist-title',(EX_LABEL[LONG]||LONG)+' · History');
  setT('ov-short-hist-title',(EX_LABEL[SHORT]||SHORT)+' · History');
  document.getElementById('stt-dot-long').className=`tt-dot dot-${LONG}`;
  document.getElementById('stt-dot-short').className=`tt-dot dot-${SHORT}`;
  const r=_opp; if(!r) return;
  // First data paint — drop the skeleton shimmer.
  document.body.classList.remove('arb-loading');
  const lIvl=r.long_interval_h||r.interval_h||8, sIvl=r.short_interval_h||r.interval_h||8;
  // Denormalise to native per-interval rate (opportunity stores 8h-normalised rates)
  const lRateNative = r.long_rate  * (lIvl / 8);
  const sRateNative = r.short_rate * (sIvl / 8);
  const lrCls=lRateNative>0?'pos':lRateNative<0?'neg':'neu';
  const srCls=sRateNative>0?'pos':sRateNative<0?'neg':'neu';
  setH('fund-long-rate', `<span class="${lrCls}" title="Per ${lIvl}h tick (native)">${sign(lRateNative)}${lRateNative.toFixed(4)}%</span>`);
  setH('fund-short-rate',`<span class="${srCls}" title="Per ${sIvl}h tick (native)">${sign(sRateNative)}${sRateNative.toFixed(4)}%</span>`);
  setT('fund-long-ivl',`/ ${lIvl}h /`); setT('fund-short-ivl',`/ ${sIvl}h /`);
  setT('fund-long-cd',fmtCD(r.next_ts_long)); setT('fund-short-cd',fmtCD(r.next_ts_short));
  setT('vol-long-val',fmtV(r.long_volume)+' USDT'); setT('vol-short-val',fmtV(r.short_volume)+' USDT');
  // Net / 8h = funding + entry-basis − fees. We use live in_pct (from
  // top-of-book) when available so Net mirrors what an actual entry now
  // would capture. Falls back to mark-based price_spread when the
  // orderbook tick hasn't landed yet.
  const inOrSpread = (typeof r.in_pct === 'number' && r.in_pct !== null) ? r.in_pct : (r.price_spread || 0);
  const netFund = (r.gross_funding||0) + inOrSpread - (r.total_fees||0);
  const nc = netFund>=0?'pos':'neg';
  setH('tb-net-val',`<span class="${nc}">${sign(netFund)}${netFund.toFixed(4)}%</span>`);
  // Funding / 8h — gross_funding is already 8h-normalised (short_rate − long_rate).
  const f8h = (r.gross_funding||0);
  const f8cls = f8h>=0?'pos':'neg';
  setH('tb-funding-8h', `<span class="${f8cls}">${sign(f8h)}${f8h.toFixed(4)}%</span>`);
  renderOverview();
}
setInterval(()=>{if(!_opp)return;setT('fund-long-cd',fmtCD(_opp.next_ts_long));setT('fund-short-cd',fmtCD(_opp.next_ts_short));},1000);

// Per-leg fallback: populate a side's infobar fields (Fund / Ivl / Next / Vol)
// from /screener/all-exchanges-funding. Runs in parallel with /pair at init
// so that even if /pair returns {opp:null} (pair has no arb right now), each
// leg still shows its funding data individually instead of dashes.
function _applyLegFallback(side, row){
  if (!row) return;
  const r = row;
  const ivl = r.interval_h || 8;
  const native = (Number(r.rate) || 0) * 100;  // rate is a decimal fraction
  const cls = native > 0 ? 'pos' : native < 0 ? 'neg' : 'neu';
  // Only fill a field if the main renderTopBar hasn't already set a real value.
  // Check text content first — if _opp won the race, don't overwrite.
  const fundEl = document.getElementById(`fund-${side}-rate`);
  if (fundEl && (fundEl.textContent === '—' || fundEl.textContent.trim() === '')) {
    setH(`fund-${side}-rate`, `<span class="${cls}" title="Per ${ivl}h tick (native)">${sign(native)}${native.toFixed(4)}%</span>`);
  }
  const ivlEl = document.getElementById(`fund-${side}-ivl`);
  if (ivlEl && (ivlEl.textContent === '—' || ivlEl.textContent.trim() === '')) {
    setT(`fund-${side}-ivl`, `/ ${ivl}h /`);
  }
  const nextEl = document.getElementById(`fund-${side}-cd`);
  if (nextEl && (nextEl.textContent === '—' || nextEl.textContent.trim() === '')) {
    setT(`fund-${side}-cd`, r.next_ts ? fmtCD(r.next_ts) : '—');
  }
  const volEl = document.getElementById(`vol-${side}-val`);
  if (volEl && (volEl.textContent === '—' || volEl.textContent.trim() === '')) {
    setT(`vol-${side}-val`, (r.volume_usd ? fmtV(r.volume_usd) : '—') + ' USDT');
  }
  // Seed trade-panel LAST from funding mark so it's non-empty before books land.
  if (side === 'long'  && _trade && _trade.long)  { _trade.long.last  = _trade.long.last  || Number(r.price) || 0; }
  if (side === 'short' && _trade && _trade.short) { _trade.short.last = _trade.short.last || Number(r.price) || 0; }
  const tLast = document.getElementById(`trade-last-${side}`);
  if (tLast && (tLast.textContent === '—' || tLast.textContent.trim() === '') && Number(r.price)) {
    tLast.textContent = '$' + Number(r.price).toFixed(4);
  }
}

// ── P&L ──────────────────────────────────────────────────────────────────────
function renderOverview(){
  const r=_opp; if(!r) return;
  const sf=r.short_rate, lf=-r.long_rate;
  setH('b-short',`<span class="pos">${sign(sf)}${sf.toFixed(4)}%</span>`);
  setH('b-long',lf>=0?`<span class="pos">+${lf.toFixed(4)}%</span>`:`<span class="neg">${lf.toFixed(4)}%</span>`);
  const spCls=r.price_spread<=0?'pos':'neg';
  setH('b-spread',`<span class="${spCls}">${sign(r.price_spread)}${r.price_spread.toFixed(4)}%</span>`);
  setH('b-fees',`<span class="neg">−${r.total_fees.toFixed(4)}%</span>`);
  setH('b-net',`<span class="${r.net_profit>=0?'pos':'neg'}">${sign(r.net_profit)}${r.net_profit.toFixed(4)}%</span>`);
  calcUpdate();
}
function toggleCalc(){
  const card = document.getElementById('calc-card');
  const chev = document.getElementById('calc-chev');
  const nowOpen = card.classList.toggle('is-open');
  if (chev) chev.style.transform = nowOpen ? 'rotate(180deg)' : 'rotate(0deg)';
  if (nowOpen) calcUpdate();
}
function calcResetSpread(){
  const el = document.getElementById('calc-spread');
  if (el) { el.value = ''; calcUpdate(); }
}
function calcUpdate(){
  const r=_opp; if(!r) return;
  const size=parseFloat(document.getElementById('calc-size').value)||0;
  const periods=parseFloat(document.getElementById('calc-periods').value)||1;
  // Spread: use override if user entered one, else live opp.price_spread
  const spreadInput = document.getElementById('calc-spread');
  const spreadRaw = spreadInput ? spreadInput.value : '';
  const spreadPct = (spreadRaw !== '' && !isNaN(parseFloat(spreadRaw))) ? parseFloat(spreadRaw) : (r.price_spread||0);

  // Funding compounds over periods (×8h); price spread realised once at close
  const funding = r.gross_funding/100 * size * periods;
  const spread  = spreadPct/100 * size;
  const fees    = r.total_fees/100 * size;
  const net     = funding + spread - fees;
  const pctOnSize = size > 0 ? net / size * 100 : 0;

  // Annualised: net over (periods × 8h) extrapolated to 365d
  const hours = periods * 8;
  const apr = size > 0 && hours > 0 ? net / size * (8760 / hours) * 100 : 0;

  const setCell = (id, val, cls) => {
    const el = document.getElementById(id); if (!el) return;
    el.textContent = val; el.className = 'calc-val ' + cls;
  };
  setCell('c-funding', (funding>=0?'+':'') + fmtU(funding), funding>=0?'pos':'neg');
  setCell('c-spread',  (spread>=0?'+':'')  + fmtU(spread),  spread>=0?'pos':'neg');
  setCell('c-fees', '−' + fmtU(fees), 'neg');
  setCell('c-net', (net>=0?'+':'') + fmtU(net), net>=0?'pos':'neg');
  setCell('c-apr', (apr>=0?'+':'') + apr.toFixed(2) + '%', apr>=0?'pos':'neg');
  setCell('c-pct', (pctOnSize>=0?'+':'') + pctOnSize.toFixed(3) + '%', pctOnSize>=0?'pos':'neg');
}

// ── Spread chart ──────────────────────────────────────────────────────────────
async function loadPrices(){
  const body=document.getElementById('spread-body');
  body.innerHTML='<div class="empty"><span class="spinner"></span> Loading…</div>';
  try{
    const res=await Auth.apiFetch(`/screener/arb-price-history?symbol=${SYM}&long_ex=${LONG}&short_ex=${SHORT}`);
    if(!res.ok) throw new Error();
    const d=await res.json();
    _longPrices=d.long_prices||[]; _shortPrices=d.short_prices||[];
    renderSpreadChart();
  }catch{body.innerHTML='<div class="empty">Failed to load</div>';}
}

function renderSpreadChart(){
  const body=document.getElementById('spread-body');
  if(!_longPrices.length||!_shortPrices.length){body.innerHTML='<div class="empty">No data</div>';return;}
  const W=body.clientWidth||500, H=body.clientHeight||200;
  const PL=52, PR=10, PT=12, PB=20, cW=W-PL-PR, cH=H-PT-PB;

  // Align prices by nearest timestamp
  const aligned=_longPrices.map(lp=>{
    const sp=_shortPrices.reduce((a,b)=>Math.abs(b.ts-lp.ts)<Math.abs(a.ts-lp.ts)?b:a);
    const spread=lp.c>0?(sp.c-lp.c)/lp.c*100:0;
    return{ts:lp.ts,spread,longC:lp.c,shortC:sp.c};
  }).filter(x=>Math.abs(x.ts-_shortPrices.reduce((a,b)=>Math.abs(b.ts-x.ts)<Math.abs(a.ts-x.ts)?b:a).ts)<7200);

  if(aligned.length<2){body.innerHTML='<div class="empty">Not enough data</div>';return;}

  const spreads=aligned.map(x=>x.spread);
  const minTs=aligned[0].ts, maxTs=aligned[aligned.length-1].ts;
  const minS=Math.min(...spreads), maxS=Math.max(...spreads);
  const pad=Math.max((maxS-minS)*.12,0.005);
  const lo=minS-pad, hi=maxS+pad;

  const xOf=ts=>PL+((ts-minTs)/(maxTs-minTs||1))*cW;
  const yOf=v=>PT+(1-(v-lo)/(hi-lo))*cH;
  const tsOf=x=>minTs+((x-PL)/cW)*(maxTs-minTs);
  const zeroY=yOf(0);

  // Build path
  const pts=aligned.map(x=>[xOf(x.ts),yOf(x.spread)]);
  let d=`M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}`;
  for(let i=1;i<pts.length;i++){const cx=(pts[i-1][0]+pts[i][0])/2;d+=` C${cx.toFixed(1)},${pts[i-1][1].toFixed(1)} ${cx.toFixed(1)},${pts[i][1].toFixed(1)} ${pts[i][0].toFixed(1)},${pts[i][1].toFixed(1)}`;}

  // Fill above 0 (positive spread = green) and below 0 (negative = red)
  const fillAbove=d+` L${pts[pts.length-1][0].toFixed(1)},${Math.min(zeroY,PT+cH).toFixed(1)} L${pts[0][0].toFixed(1)},${Math.min(zeroY,PT+cH).toFixed(1)}Z`;
  const fillBelow=d+` L${pts[pts.length-1][0].toFixed(1)},${Math.max(zeroY,PT).toFixed(1)} L${pts[0][0].toFixed(1)},${Math.max(zeroY,PT).toFixed(1)}Z`;

  const T=_svgTheme();
  const fmtY=v=>(v>=0?'+':'')+v.toFixed(3)+'%';
  const yVals=[lo,lo+(hi-lo)*.25,lo+(hi-lo)*.5,lo+(hi-lo)*.75,hi];
  const yLabels=yVals.map(v=>`<text x="${PL-4}" y="${yOf(v).toFixed(1)}" text-anchor="end" dominant-baseline="middle" fill="${T.axis}" font-size="9" font-family="JetBrains Mono,monospace">${fmtY(v)}</text>`).join('');
  const nX=4;
  const xLabels=Array.from({length:nX}).map((_,i)=>{const ts=minTs+(maxTs-minTs)*i/(nX-1),x=xOf(ts).toFixed(1),dt=new Date(ts*1000);return`<text x="${x}" y="${H-5}" text-anchor="middle" fill="${T.axis}" font-size="8.5" font-family="Inter,sans-serif">${dt.toLocaleDateString('en-US',{month:'short',day:'numeric'})} ${String(dt.getHours()).padStart(2,'0')}:00</text>`;}).join('');
  const grid=yVals.map(v=>`<line x1="${PL}" y1="${yOf(v).toFixed(1)}" x2="${W-PR}" y2="${yOf(v).toFixed(1)}" stroke="${T.grid}" stroke-width="1"/>`).join('');

  const zLine=(lo<0&&hi>0)?`<line x1="${PL}" y1="${zeroY.toFixed(1)}" x2="${W-PR}" y2="${zeroY.toFixed(1)}" stroke="${T.zero}" stroke-width="1.5" stroke-dasharray="4,3"/>`:'';

  body.innerHTML=`<svg id="ssvg" viewBox="0 0 ${W} ${H}" style="width:100%;height:100%;display:block;cursor:crosshair">
    <defs>
      <clipPath id="sc"><rect x="${PL}" y="${PT}" width="${cW}" height="${cH}"/></clipPath>
      <clipPath id="sc-above"><rect x="${PL}" y="${PT}" width="${cW}" height="${Math.max(0,zeroY-PT).toFixed(1)}"/></clipPath>
      <clipPath id="sc-below"><rect x="${PL}" y="${zeroY.toFixed(1)}" width="${cW}" height="${Math.max(0,PT+cH-zeroY).toFixed(1)}"/></clipPath>
      <linearGradient id="sg" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${T.greenFill}" stop-opacity=".25"/><stop offset="100%" stop-color="${T.greenFill}" stop-opacity="0"/></linearGradient>
      <linearGradient id="rg" x1="0" y1="1" x2="0" y2="0"><stop offset="0%" stop-color="${T.redFill}" stop-opacity=".25"/><stop offset="100%" stop-color="${T.redFill}" stop-opacity="0"/></linearGradient>
    </defs>
    <rect x="${PL}" y="${PT}" width="${cW}" height="${cH}" fill="${T.bg}"/>
    ${grid}${zLine}${yLabels}${xLabels}
    <g clip-path="url(#sc)">
      <path d="${fillAbove}" fill="url(#sg)" clip-path="url(#sc-above)"/>
      <path d="${fillBelow}" fill="url(#rg)" clip-path="url(#sc-below)"/>
      <path d="${d}" fill="none" stroke="${T.line}" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round" opacity=".9"/>
    </g>
    <line id="sch" x1="0" y1="${PT}" x2="0" y2="${PT+cH}" stroke="${T.zero}" stroke-width="1" stroke-dasharray="3,3" opacity="0"/>
    <circle id="scd" r="3" fill="${T.line}" stroke="${T.dot}" stroke-width="2" opacity="0"/>
    <rect x="${PL}" y="${PT}" width="${cW}" height="${cH}" fill="transparent"/>
  </svg>`;

  const svg=body.querySelector('#ssvg');
  const sch=svg.querySelector('#sch'), scd=svg.querySelector('#scd');
  const tip=document.getElementById('spread-tooltip');
  const near=(arr,ts)=>arr.length?arr.reduce((a,b)=>Math.abs(b.ts-ts)<Math.abs(a.ts-ts)?b:a):null;

  svg.addEventListener('mousemove',e=>{
    const r=svg.getBoundingClientRect(),sx=(e.clientX-r.left)*(W/r.width);
    if(sx<PL||sx>W-PR){sHide();return;}
    const hTs=tsOf(sx), pt=near(aligned,hTs);
    if(!pt){sHide();return;}
    const ax=xOf(pt.ts),ay=yOf(pt.spread);
    sch.setAttribute('x1',ax.toFixed(1));sch.setAttribute('x2',ax.toFixed(1));sch.setAttribute('opacity','1');
    scd.setAttribute('cx',ax.toFixed(1));scd.setAttribute('cy',ay.toFixed(1));scd.setAttribute('opacity','1');
    const spCls=pt.spread>0?'pos':pt.spread<0?'neg':'neu';
    setT('stt-date',fmtDate(pt.ts));
    setH('stt-spread',`<span class="${spCls}">${sign(pt.spread)}${pt.spread.toFixed(4)}%</span>`);
    setT('stt-val-long',fmtP(pt.longC));
    setT('stt-val-short',fmtP(pt.shortC));
    const wR=body.getBoundingClientRect();let left=e.clientX-wR.left;left=Math.max(75,Math.min(wR.width-75,left));
    tip.style.left=left+'px';tip.style.top='6px';tip.classList.add('visible');
  });
  svg.addEventListener('mouseleave',sHide);
  function sHide(){sch.setAttribute('opacity','0');scd.setAttribute('opacity','0');tip.classList.remove('visible');}
  _spreadBuilt=true;
}

// ── Funding rate chart ────────────────────────────────────────────────────────
function renderFundChart(){
  const body=document.getElementById('fund-chart-body');
  if(!_longHist.length&&!_shortHist.length){body.innerHTML='<div class="empty">No data</div>';return;}
  const W=body.clientWidth||500, H=body.clientHeight||120;
  const PL=48, PR=8, PT=8, PB=16, cW=W-PL-PR, cH=H-PT-PB;
  const allR=[..._longHist.map(x=>x.rate*100),..._shortHist.map(x=>x.rate*100)];
  const allT=[..._longHist.map(x=>x.ts),..._shortHist.map(x=>x.ts)];
  const minR=Math.min(...allR),maxR=Math.max(...allR),minTs=Math.min(...allT),maxTs=Math.max(...allT);
  const rPad=(maxR-minR)*.15||.01;
  const lo=minR-rPad,hi=maxR+rPad;
  const xOf=ts=>PL+((ts-minTs)/(maxTs-minTs||1))*cW;
  const yOf=r=>PT+(1-(r-lo)/(hi-lo))*cH;
  const tsOf=x=>minTs+((x-PL)/cW)*(maxTs-minTs);

  const mkPath=(series,color,gid)=>{
    if(series.length<2)return'';
    const pts=series.map(x=>[xOf(x.ts),yOf(x.rate*100)]);
    let d=`M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}`;
    for(let i=1;i<pts.length;i++){const cx=(pts[i-1][0]+pts[i][0])/2;d+=` C${cx.toFixed(1)},${pts[i-1][1].toFixed(1)} ${cx.toFixed(1)},${pts[i][1].toFixed(1)} ${pts[i][0].toFixed(1)},${pts[i][1].toFixed(1)}`;}
    const fill=d+` L${pts[pts.length-1][0].toFixed(1)},${PT+cH} L${pts[0][0].toFixed(1)},${PT+cH}Z`;
    return`<linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${color}" stop-opacity=".15"/><stop offset="100%" stop-color="${color}" stop-opacity="0"/></linearGradient><path d="${fill}" fill="url(#${gid})"/><path d="${d}" fill="none" stroke="${color}" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"/>`;
  };

  const T=_svgTheme();
  const zeroY=yOf(0).toFixed(1);
  const zLine=(lo<0&&hi>0)?`<line x1="${PL}" y1="${zeroY}" x2="${W-PR}" y2="${zeroY}" stroke="${T.zero}" stroke-width="1" stroke-dasharray="3,2"/>`:'';
  const yLbls=[lo,(lo+hi)/2,hi].map(v=>`<text x="${PL-4}" y="${yOf(v).toFixed(1)}" text-anchor="end" dominant-baseline="middle" fill="${T.axis}" font-size="8.5" font-family="JetBrains Mono,monospace">${v>=0?'+':''}${v.toFixed(3)}%</text>`).join('');
  const xLbls=[minTs,(minTs+maxTs)/2,maxTs].map(ts=>{const x=xOf(ts).toFixed(1),dt=new Date(ts*1000);return`<text x="${x}" y="${H-3}" text-anchor="middle" fill="${T.axis}" font-size="8.5" font-family="Inter,sans-serif">${dt.toLocaleDateString('en-US',{month:'short',day:'numeric'})}</text>`;}).join('');

  body.innerHTML=`<svg id="fsvg" viewBox="0 0 ${W} ${H}" style="width:100%;height:100%;display:block;cursor:crosshair">
    <defs><clipPath id="fc"><rect x="${PL}" y="${PT}" width="${cW}" height="${cH}"/></clipPath>
    ${mkPath(_longHist,T.greenStroke,'fgl')}${mkPath(_shortHist,T.redStroke,'fgs')}</defs>
    <rect x="${PL}" y="${PT}" width="${cW}" height="${cH}" fill="${T.bg}"/>
    ${zLine}${yLbls}${xLbls}
    <g clip-path="url(#fc)">${mkPath(_longHist,T.greenStroke,'fgl2')}${mkPath(_shortHist,T.redStroke,'fgs2')}</g>
    <line id="fch" x1="0" y1="${PT}" x2="0" y2="${PT+cH}" stroke="${T.zero}" stroke-width="1" stroke-dasharray="3,3" opacity="0"/>
    <circle id="fdl" r="2.5" fill="${T.greenStroke}" stroke="${T.dot}" stroke-width="1.5" opacity="0"/>
    <circle id="fds" r="2.5" fill="${T.redStroke}" stroke="${T.dot}" stroke-width="1.5" opacity="0"/>
    <rect x="${PL}" y="${PT}" width="${cW}" height="${cH}" fill="transparent"/>
  </svg>`;

  const svg=document.getElementById('fsvg'),fch=svg.querySelector('#fch'),fdl=svg.querySelector('#fdl'),fds=svg.querySelector('#fds'),ftip=document.getElementById('fund-tooltip');
  const near=(arr,ts)=>arr.length?arr.reduce((a,b)=>Math.abs(b.ts-ts)<Math.abs(a.ts-ts)?b:a):null;
  svg.addEventListener('mousemove',e=>{
    const r=svg.getBoundingClientRect(),sx=(e.clientX-r.left)*(W/r.width);
    if(sx<PL||sx>W-PR){fHide();return;}
    const hTs=tsOf(sx),nl=near(_longHist,hTs),ns=near(_shortHist,hTs);
    const aTs=nl?nl.ts:(ns?ns.ts:hTs),ax=xOf(aTs);
    fch.setAttribute('x1',ax.toFixed(1));fch.setAttribute('x2',ax.toFixed(1));fch.setAttribute('opacity','1');
    if(nl){fdl.setAttribute('cx',xOf(nl.ts).toFixed(1));fdl.setAttribute('cy',yOf(nl.rate*100).toFixed(1));fdl.setAttribute('opacity','1');}else fdl.setAttribute('opacity','0');
    if(ns){fds.setAttribute('cx',xOf(ns.ts).toFixed(1));fds.setAttribute('cy',yOf(ns.rate*100).toFixed(1));fds.setAttribute('opacity','1');}else fds.setAttribute('opacity','0');
    setT('ftt-date',fmtDate(nl?nl.ts:(ns?ns.ts:aTs)));
    const lr=nl?nl.rate*100:null,sr=ns?ns.rate*100:null;
    setH('ftt-long',lr!=null?`<span style="color:${lr>0?'#1AFFAB':lr<0?'#F87171':'#9B9FAB'}">${sign(lr)}${lr.toFixed(4)}%</span>`:'<span style="color:#55596A">—</span>');
    setH('ftt-short',sr!=null?`<span style="color:${sr>0?'#1AFFAB':sr<0?'#F87171':'#9B9FAB'}">${sign(sr)}${sr.toFixed(4)}%</span>`:'<span style="color:#55596A">—</span>');
    if(lr!=null&&sr!=null){const net=sr-lr;setH('ftt-net',`<span style="color:${net>=0?'#1AFFAB':'#F87171'}">${sign(net)}${net.toFixed(4)}%</span>`);}else setT('ftt-net','—');
    const wR=body.getBoundingClientRect();let left=e.clientX-wR.left;left=Math.max(70,Math.min(wR.width-70,left));
    ftip.style.left=left+'px';ftip.style.top='4px';ftip.classList.add('visible');
  });
  svg.addEventListener('mouseleave',fHide);
  function fHide(){fch.setAttribute('opacity','0');fdl.setAttribute('opacity','0');fds.setAttribute('opacity','0');ftip.classList.remove('visible');}
  _fundBuilt=true;
}

// ── Overview funding history tables ───────────────────────────────────────────
function renderOverviewTables(){
  const fmtR=v=>v!=null?`<span style="color:${v>0?'#1AFFAB':v<0?'#F87171':'#9B9FAB'};font-family:var(--mono)">${sign(v)}${v.toFixed(4)}%</span>`:'<span style="color:#55596A">—</span>';
  const now=Date.now()/1000;
  const sum24h=arr=>arr.filter(x=>x.ts>=now-86400).reduce((a,x)=>a+x.rate*100,0);
  const sum7d=arr=>arr.filter(x=>x.ts>=now-7*86400).reduce((a,x)=>a+x.rate*100,0);
  const l24=sum24h(_longHist), l7d=sum7d(_longHist);
  const s24=sum24h(_shortHist), s7d=sum7d(_shortHist);
  setH('ov-long-24h',`<span class="${l24>=0?'pos':'neg'}">${sign(l24)}${l24.toFixed(4)}%</span>`);
  setH('ov-long-7d', `<span class="${l7d>=0?'pos':'neg'}">${sign(l7d)}${l7d.toFixed(4)}%</span>`);
  setH('ov-short-24h',`<span class="${s24>=0?'pos':'neg'}">${sign(s24)}${s24.toFixed(4)}%</span>`);
  setH('ov-short-7d', `<span class="${s7d>=0?'pos':'neg'}">${sign(s7d)}${s7d.toFixed(4)}%</span>`);

  const mkTable=(arr,tbodyId)=>{
    let acc=0;
    const rows=[...arr].sort((a,b)=>b.ts-a.ts).map(x=>{
      const r=x.rate*100; acc+=r;
      return`<tr><td>${fmtDateFull(x.ts)}</td><td>${fmtR(r)}</td><td>${fmtR(acc)}</td></tr>`;
    });
    document.getElementById(tbodyId).innerHTML=rows.join('')||'<tr><td colspan="3" style="padding:12px;text-align:center;color:var(--text3)">No data</td></tr>';
  };
  mkTable(_longHist,'fh-long-tbody');
  mkTable(_shortHist,'fh-short-tbody');
}

// ── Info tab stats ────────────────────────────────────────────────────────────
function renderInfoStats(){
  if(!_longPrices.length||!_shortPrices.length) return;
  const cutoff=Date.now()/1000-_infoPeriodH*3600;
  const aligned=_longPrices.filter(x=>x.ts>=cutoff).map(lp=>{
    const sp=_shortPrices.reduce((a,b)=>Math.abs(b.ts-lp.ts)<Math.abs(a.ts-lp.ts)?b:a);
    return{ts:lp.ts,spread:lp.c>0?(sp.c-lp.c)/lp.c*100:0,longC:lp.c,shortC:sp.c};
  });
  if(!aligned.length){['i-max-in','i-min-in','i-max-out','i-min-out','i-median','i-gaps'].forEach(id=>setT(id,'—'));return;}
  const spreads=aligned.map(x=>x.spread).sort((a,b)=>a-b);
  const pos=spreads.filter(x=>x>0), neg=spreads.filter(x=>x<0);
  const delta=parseFloat(document.getElementById('info-delta').value)||0;
  const gaps=aligned.filter(x=>Math.abs(x.spread)>=delta).length;
  const median=spreads[Math.floor(spreads.length/2)];
  setH('i-max-in',pos.length?`<span class="pos">+${Math.max(...pos).toFixed(4)}%</span>`:'<span class="neu">—</span>');
  setH('i-min-in',pos.length?`<span class="pos">+${Math.min(...pos).toFixed(4)}%</span>`:'<span class="neu">—</span>');
  setH('i-max-out',neg.length?`<span class="neg">${Math.min(...neg).toFixed(4)}%</span>`:'<span class="neu">—</span>');
  setH('i-min-out',neg.length?`<span class="neg">${Math.max(...neg).toFixed(4)}%</span>`:'<span class="neu">—</span>');
  setH('i-median',`<span class="${median>=0?'pos':'neg'}">${sign(median)}${median.toFixed(4)}%</span>`);
  setT('i-gaps',gaps);
  const fmtR=v=>`<span style="color:${v>0?'#1AFFAB':v<0?'#F87171':'#9B9FAB'};font-family:var(--mono)">${sign(v)}${v.toFixed(4)}%</span>`;
  document.getElementById('info-spread-tbody').innerHTML=[...aligned].sort((a,b)=>b.ts-a.ts).slice(0,200).map(x=>`<tr><td style="color:var(--text3)">${fmtDate(x.ts)}</td><td style="text-align:right">${fmtR(x.longC>0?(x.longC-x.longC)/x.longC*100:0)}</td><td style="text-align:right">${fmtP(x.shortC)}</td><td style="text-align:right">${fmtR(x.spread)}</td></tr>`).join('');
}

// ── Order book grouping ───────────────────────────────────────────────────────
// Depth grouping = price precision levels (like on exchange)
// Level 0 = finest (tick size), 1 = ×10, 2 = ×100
// Default to the finest granularity (idx=0 = native tick). The previous
// default of 1 multiplied groupSize by 10, which collapsed 20 raw levels
// into 2-3 aggregated buckets on mid-priced tokens (RAVE at $1.06 went
// from 20 levels → 3 visible rows). Users can widen the grouping via the
// depth buttons.
const _depthGroupIdx={long:0,short:0};
let _lastBookData={long:null,short:null};

function getTickSize(price){
  if(price>=10000) return 1;
  if(price>=1000)  return 0.1;
  if(price>=100)   return 0.01;
  if(price>=10)    return 0.001;
  if(price>=1)     return 0.0001;
  if(price>=0.1)   return 0.00001;
  return 0.000001;
}
function getGroupSize(price,idx){
  const tick=getTickSize(price);
  return tick*Math.pow(10,idx);
}
function groupBookLevels(levels,groupSize){
  const map=new Map();
  for(const[p,q]of levels){
    const g=Math.floor(p/groupSize)*groupSize;
    const key=g.toFixed(10);
    map.set(key,(map.get(key)||0)+q);
  }
  return [...map.entries()].map(([k,q])=>[parseFloat(k),q]).sort((a,b)=>b[0]-a[0]);
}
function getDepthLabel(price,idx){
  const g=getGroupSize(price,idx);
  if(g>=1) return g.toFixed(0);
  const d=Math.max(0,-Math.floor(Math.log10(g)));
  return g.toFixed(d);
}
function updateDepthLabels(side,price){
  const btns=document.querySelectorAll(`#depth-btns-${side} .depth-btn`);
  btns.forEach((btn,i)=>{btn.textContent=getDepthLabel(price,i);});
}

function setDepthGroup(side,idx,el){
  _depthGroupIdx[side]=idx;
  document.querySelectorAll(`#depth-btns-${side} .depth-btn`).forEach(b=>b.classList.remove('active'));
  el.classList.add('active');
  if(_lastBookData[side]) renderBook(_lastBookData[side],side);
}

// ── Order book render ─────────────────────────────────────────────────────────
const _midPrev={long:null,short:null};
const _bookInflight={long:false,short:false};
async function fetchBook(exchange,side){
  if(_bookInflight[side]) return;  // skip if previous request still pending
  _bookInflight[side]=true;
  try{
    // Detail page wants the deepest book the venue offers — 200 levels.
    // limit > 30 forces a fresh REST snapshot in get_cached_orderbook
    // (WS prewarm caches only top-of-book for the screener list).
    const res=await Auth.apiFetch(`/screener/orderbook?symbol=${SYM}&exchange=${exchange}&limit=200`);
    if(!res.ok) return;
    const d=await res.json();
    // Don't overwrite the last good snapshot with an empty REST response
    // (cache hiccup, WS resync). If the new payload has no levels but we
    // had a previous one, keep showing the previous and just skip render.
    // Otherwise the chart sampler bails out → entry/exit chart stutters.
    const empty = !d || (!(d.bids||[]).length && !(d.asks||[]).length);
    if (empty && _lastBookData[side]) return;
    _lastBookData[side]=d;
    renderBook(d,side);
  }catch{}
  finally{_bookInflight[side]=false;}
}

// ── Pre-allocated DOM pool for the depth book ─────────────────────────────────
// Previous renderBook stringified 100 rows × 2 sides and innerHTML'd them on
// every WS frame (≤60 fps with rAF coalescing). On a hot pair (Binance BTC)
// that's 12000 row-strings/sec on the main thread + matching GC pressure.
// Row-pool keeps 50 ask + 50 bid DOM nodes per side, mutates textContent +
// style.width only. Zero allocations after init, no HTML parse, no GC churn.
const ROW_POOL_SIZE = 50;
const _bookPool = { long: null, short: null };
const _bookKind = { long: '', short: '' }; // 'pool' | 'empty' | ''

function _buildBookSidePool(containerId, kind){
  const el = document.getElementById(containerId);
  if (!el) return null;
  el.innerHTML = '';  // wipe whatever was there (empty msg / old innerHTML output)
  const rows = [];
  for (let i = 0; i < ROW_POOL_SIZE; i++){
    const row = document.createElement('div');
    row.className = 'book-row';
    const bg = document.createElement('div');
    bg.className = 'book-row-bg';
    bg.style.width = '0%';
    bg.style.background = kind === 'ask' ? '#F87171' : '#1AFFAB';
    const price = document.createElement('span');
    price.className = kind === 'ask' ? 'ask-price' : 'bid-price';
    const amount = document.createElement('span');
    amount.className = 'book-amount';
    const total = document.createElement('span');
    total.className = 'book-total';
    row.append(bg, price, amount, total);
    row.style.display = 'none';
    el.appendChild(row);
    rows.push({row, bg, price, amount, total});
  }
  return rows;
}

function _ensureBookPool(side){
  const p = _bookPool[side];
  if (p && _bookKind[side] === 'pool') return p;
  const asks = _buildBookSidePool(`asks-${side}`, 'ask');
  const bids = _buildBookSidePool(`bids-${side}`, 'bid');
  _bookPool[side] = {asks, bids};
  _bookKind[side] = 'pool';
  return _bookPool[side];
}

function _renderEmptyBook(side){
  if (_bookKind[side] === 'empty') return;
  const asksEl = document.getElementById(`asks-${side}`);
  const bidsEl = document.getElementById(`bids-${side}`);
  if (asksEl) asksEl.innerHTML = `<div class="book-empty" style="padding:18px 10px;color:var(--text3);font-size:11px;text-align:center">no orderbook · symbol delisted or unlisted</div>`;
  if (bidsEl) bidsEl.innerHTML = '';
  _bookPool[side] = null;
  _bookKind[side] = 'empty';
}

function _applyLevelsToPool(rows, levels, max, dir){
  // dir: +1 walk top→bottom (bids); for asks we precomputed reverse order.
  let total = 0;
  const n = levels.length;
  for (let i = 0; i < ROW_POOL_SIZE; i++){
    const r = rows[i];
    if (i >= n){
      if (r.row.style.display !== 'none') r.row.style.display = 'none';
      continue;
    }
    const [p, q] = levels[i];
    total += q * p;
    const pct = (q / max * 100).toFixed(1);
    const widthStr = pct + '%';
    // Touch DOM only when the underlying value actually changed — most
    // rows on a hot pair only flicker the top few levels per frame.
    if (r._w !== widthStr){ r.bg.style.width = widthStr; r._w = widthStr; }
    const ps = fmtBookP(p);
    if (r._p !== ps){ r.price.textContent = ps; r._p = ps; }
    const qs = fmtV(q);
    if (r._q !== qs){ r.amount.textContent = qs; r._q = qs; }
    const ts = fmtV(total);
    if (r._t !== ts){ r.total.textContent = ts; r._t = ts; }
    if (r.row.style.display === 'none') r.row.style.display = '';
  }
}

function renderBook(d,side){
  const rawAsks=(d.asks||[]);
  const rawBids=(d.bids||[]);
  if(!rawAsks.length&&!rawBids.length){
    _renderEmptyBook(side);
    return;
  }

  // Detect price for tick size
  const refPrice=rawBids[0]?.[0]||rawAsks[0]?.[0]||0;
  const groupSize=getGroupSize(refPrice,_depthGroupIdx[side]);
  updateDepthLabels(side,refPrice);

  // asks: pick the LOWEST 50 (closest to market) then reverse so the
  // table renders from highest down to lowest with the spread at the
  // bottom. Bids stay sort-desc + first 50 (highest bid = best bid).
  const asks=groupBookLevels(rawAsks,groupSize).sort((a,b)=>a[0]-b[0]).slice(0,ROW_POOL_SIZE).reverse();
  const bids=groupBookLevels(rawBids,groupSize).sort((a,b)=>b[0]-a[0]).slice(0,ROW_POOL_SIZE);

  // maxA / maxB without Math.max(...spread) — avoids the temp args array.
  let maxA = 1, maxB = 1;
  for (let i = 0; i < asks.length; i++) if (asks[i][1] > maxA) maxA = asks[i][1];
  for (let i = 0; i < bids.length; i++) if (bids[i][1] > maxB) maxB = bids[i][1];

  const pool = _ensureBookPool(side);
  _applyLevelsToPool(pool.asks, asks, maxA, -1);
  _applyLevelsToPool(pool.bids, bids, maxB, +1);

  const midPrice=asks.length&&bids.length?(asks[asks.length-1][0]+bids[0][0])/2:(asks[0]?.[0]||bids[0]?.[0]||0);
  const prev=_midPrev[side];
  const midEl=document.getElementById(`mid-${side}`);
  const arrowEl=document.getElementById(`mid-${side}-arrow`);
  midEl.textContent=fmtBookP(midPrice);
  if(prev!==null&&prev!==midPrice){
    const up=midPrice>prev;
    midEl.style.color=up?'#1AFFAB':'#F87171';
    arrowEl.textContent=up?'▲':'▼';
    arrowEl.style.color=up?'#1AFFAB':'#F87171';
    clearTimeout(midEl._colorTimer);
    midEl._colorTimer=setTimeout(()=>{midEl.style.color='';arrowEl.textContent='';},1500);
  }
  _midPrev[side]=midPrice;
  setT(`book-${side}-price`,fmtBookP(midPrice));
  // Live LAST on the trade panel — mirrors the orderbook mid each tick
  // (fetchBook runs every 150ms). Without this the trade panel stayed at
  // the initial _opp.long_price / short_price snapshot and was often '—'
  // when the pair wasn't in the arb result cache.
  if (typeof _trade !== 'undefined' && _trade && _trade[side] && midPrice > 0) {
    _trade[side].last = midPrice;
    const tLast = document.getElementById(`trade-last-${side}`);
    if (tLast) tLast.textContent = '$' + Number(midPrice).toFixed(midPrice < 1 ? 6 : 4);
  }
  // Live spread uses raw top-of-book (not grouped), so it reacts to every tick
  const rawBestBid=rawBids[0]?.[0]||0;
  const rawBestAsk=rawAsks[0]?.[0]||0;
  const rawMid=(rawBestBid&&rawBestAsk)?(rawBestBid+rawBestAsk)/2:(rawBestBid||rawBestAsk||0);
  if(side==='long') { _liveMidLong=rawMid; _liveAskLong=rawBestAsk; }
  if(side==='short') { _liveMidShort=rawMid; _liveBidShort=rawBestBid; }
  // Sample first so updateLiveSpread can read the freshest local in_pct
  // when _opp.in_pct isn't available (pair outside top-1000 arb feed).
  sampleEntryExit();
  updateLiveSpread();
}

// ── Entry/Exit live divergence chart ──────────────────────────────────────────
const _eeHist=[];   // [{ts, inPct, outPct}]
// EE_MAX was 9000 (~22 min @ 150ms) — ~220KB of objects per pair held in
// JS heap, plus the localStorage write below ran every 1s with a JSON.stringify
// of the full tail. Trimmed to 2000 (~5 min @ 150ms / ~20 min @ 600ms typical)
// to cut both heap pressure and main-thread stalls from synchronous setItem.
const EE_MAX=2000;
const EE_STORAGE_KEY=`ee-hist:${SYM}:${LONG}:${SHORT}`;
const EE_MAX_STORED=2000;  // cap persisted samples to keep localStorage small

// Load on init
(()=>{
  try{
    const raw=localStorage.getItem(EE_STORAGE_KEY);
    if(!raw) return;
    const saved=JSON.parse(raw);
    if(!Array.isArray(saved)) return;
    const cutoff=Date.now()/1000-1300;  // drop entries older than 20min+buffer
    for(const p of saved){
      if(p&&typeof p.ts==='number'&&p.ts>cutoff) _eeHist.push(p);
    }
  }catch{}
})();

let _eeSaveTimer=null;
function _eeScheduleSave(){
  if(_eeSaveTimer) return;
  // 5s throttle (was 1s). localStorage.setItem is synchronous and blocks
  // main thread; on a 5min-of-history JSON string (~150KB) the hit is
  // 10-20ms each — directly visible to the user as scroll jank.
  _eeSaveTimer=setTimeout(()=>{
    _eeSaveTimer=null;
    if(document.hidden) return;  // skip writes when tab is in background
    try{
      const tail=_eeHist.slice(-EE_MAX_STORED);
      localStorage.setItem(EE_STORAGE_KEY,JSON.stringify(tail));
    }catch{}
  },5000);
}
let _eeTfSec=30;    // selected timeframe in seconds
let _eeLastRender=0;
let _eeSize=0;              // 0 = use best level only
let _eeSizeUnit='usdt';     // 'usdt' | 'token'
let _eeNeedsFit=true;   // only auto-fit on init or TF change
function setEeTf(el,sec){
  _eeTfSec=sec;
  el.parentElement.querySelectorAll('.tf-btn').forEach(b=>{
    if(b.id==='ee-unit-usdt'||b.id==='ee-unit-token') return;
    b.classList.remove('active');
  });
  el.classList.add('active');
  _eeNeedsFit=true;
  if(_eeChart) _eeChart.applyOptions({timeScale:{secondsVisible:sec<=60}});
  renderEntryExitChart();
}
function setEeUnit(el,u){
  _eeSizeUnit=u;
  document.getElementById('ee-unit-usdt').classList.toggle('active',u==='usdt');
  document.getElementById('ee-unit-token').classList.toggle('active',u==='token');
}
function onEeSizeInput(){
  const v=parseFloat(document.getElementById('ee-size').value);
  _eeSize=isFinite(v)&&v>0?v:0;
}
// Walk orderbook to compute avg fill price for given size (USDT or token)
function _vwap(levels,size,unit){
  if(!levels||!levels.length) return null;
  if(!size||size<=0) return levels[0][0];
  let filledQty=0, cost=0;
  for(const [p,q] of levels){
    if(unit==='token'){
      const need=size-filledQty; if(need<=0) break;
      const take=Math.min(q,need);
      cost+=take*p; filledQty+=take;
    }else{
      const needN=size-cost; if(needN<=0) break;
      const take=Math.min(q,needN/p);
      cost+=take*p; filledQty+=take;
    }
  }
  return filledQty>0?cost/filledQty:levels[0][0];
}
function sampleEntryExit(){
  const L=_lastBookData.long, S=_lastBookData.short;
  if(!L||!S) return;
  if(!L.asks?.length||!L.bids?.length||!S.asks?.length||!S.bids?.length) return;
  // Buying on long (asks ascending) / selling on short (bids descending)
  const askL=_vwap(L.asks,_eeSize,_eeSizeUnit);
  const bidS=_vwap(S.bids,_eeSize,_eeSizeUnit);
  // Closing: selling on long (bids) / buying on short (asks)
  const bidL=_vwap(L.bids,_eeSize,_eeSizeUnit);
  const askS=_vwap(S.asks,_eeSize,_eeSizeUnit);
  if(!askL||!bidL||!askS||!bidS) return;
  const inPct=(bidS-askL)/askL*100;
  const outPct=(bidL-askS)/askS*100;
  const ts=Date.now()/1000;
  const last=_eeHist[_eeHist.length-1];
  // 20ms (was 120ms) so entry/exit redraws at full underlying data rate.
  // The == check still filters duplicate samples — only changed values
  // hit the chart/numeric update path.
  if(last && ts-last.ts<0.02 && last.inPct===inPct && last.outPct===outPct) return;
  _eeHist.push({ts,inPct,outPct});
  if(_eeHist.length>EE_MAX) _eeHist.shift();
  _eeScheduleSave();
  // Evict samples older than max supported window (20m) to save memory
  const cutoff=ts-1300;
  while(_eeHist.length && _eeHist[0].ts<cutoff) _eeHist.shift();
  setT('ee-in-val',(inPct>=0?'+':'')+inPct.toFixed(4)+'%');
  setT('ee-out-val',(outPct>=0?'+':'')+outPct.toFixed(4)+'%');
  setT('ee-count',_eeHist.length+' pts');
  const now=performance.now();
  if(now-_eeLastRender>250){_eeLastRender=now;renderEntryExitChart();}
}

// ── Lightweight Charts (TradingView) instance ─────────────────────────────────
// One candle = one TF period
const EE_BUCKET = {30:30, 60:60, 300:300, 900:900, 1200:1200};
let _eeChart=null, _eeInSeries=null, _eeOutSeries=null;

function _eeBucket(ts){
  const b=EE_BUCKET[_eeTfSec]||2;
  return Math.floor(ts/b)*b;
}

function _eeBuildCandles(){
  const bucket=EE_BUCKET[_eeTfSec]||60;
  // Both In and Out as OHLC candles — entry and exit divergence read
  // the same way so a trader can compare bar bodies directly.
  const bIn=new Map();
  const bOut=new Map();
  for(const p of _eeHist){
    const t=Math.floor(p.ts/bucket)*bucket;
    const iv=p.inPct, ov=p.outPct;
    let ci=bIn.get(t);
    if(!ci){ci={time:t,open:iv,high:iv,low:iv,close:iv};bIn.set(t,ci);}
    else { if(iv>ci.high) ci.high=iv; if(iv<ci.low) ci.low=iv; ci.close=iv; }
    let co=bOut.get(t);
    if(!co){co={time:t,open:ov,high:ov,low:ov,close:ov};bOut.set(t,co);}
    else { if(ov>co.high) co.high=ov; if(ov<co.low) co.low=ov; co.close=ov; }
  }
  const inArr =[...bIn.values()].sort((a,b)=>a.time-b.time);
  const outArr=[...bOut.values()].sort((a,b)=>a.time-b.time);
  return {inArr,outArr};
}

function _svgTheme(){
  return document.body.classList.contains('light')?{
    bg:'#FFFFFF',grid:'#E0E0E0',axis:'#595959',line:'#000000',dot:'#FFFFFF',zero:'#8C8C8C',
    greenFill:'#006B3C',redFill:'#8B0000',greenStroke:'#006B3C',redStroke:'#8B0000',
  }:{
    bg:'#0D0D12',grid:'#1F1F28',axis:'#55596A',line:'#E6E8E3',dot:'#0B0B0E',zero:'#3A3A50',
    greenFill:'#1AFFAB',redFill:'#F87171',greenStroke:'#1AFFAB',redStroke:'#F87171',
  };
}
function _eeThemeColors(){
  const light=document.body.classList.contains('light');
  return light?{
    bg:'#FFFFFF',text:'#1A1A1A',grid:'#E0E0E0',border:'#000000',cross:'#595959',
    zero:'#8C8C8C',
    // In = saturated entry colours; Out = muted / translucent so they read
    // as the secondary series without crowding the In bars.
    inUp:'#006B3C', inDown:'#8B0000',
    outUp:'#006B3C55', outDown:'#8B000055', outWickUp:'#006B3C88', outWickDown:'#8B000088',
  }:{
    bg:'#0D0D12',text:'#9B9FAB',grid:'#1F1F28',border:'#22222A',cross:'#3A3A50',
    zero:'#3A3A50',
    inUp:'#1AFFAB', inDown:'#F87171',
    outUp:'#1AFFAB55', outDown:'#F8717155', outWickUp:'#1AFFAB88', outWickDown:'#F8717188',
  };
}
function _eeApplyTheme(){
  if(!_eeChart) return;
  const c=_eeThemeColors();
  _eeChart.applyOptions({
    layout:{background:{type:'solid',color:'rgba(0,0,0,0)'},textColor:c.text},
    grid:{vertLines:{color:c.grid},horzLines:{color:c.grid}},
    rightPriceScale:{borderColor:c.border},
    timeScale:{borderColor:c.border},
    crosshair:{vertLine:{color:c.cross},horzLine:{color:c.cross}},
  });
  _eeInSeries.applyOptions({
    upColor:c.inUp, downColor:c.inDown,
    borderUpColor:c.inUp, borderDownColor:c.inDown,
    wickUpColor:c.inUp, wickDownColor:c.inDown,
  });
  _eeOutSeries.applyOptions({
    upColor:c.outUp, downColor:c.outDown,
    borderUpColor:c.outUp, borderDownColor:c.outDown,
    wickUpColor:c.outWickUp, wickDownColor:c.outWickDown,
  });
}
// 161KB / 50KB gzip vendor lib used only by the entry/exit candlestick
// chart (below the fold on most viewports). Load lazily — orderbook
// rendering on the hot WS path stays uncontended.
let _lwcPromise=null;
function _loadLightweightCharts(){
  if(typeof LightweightCharts!=='undefined') return Promise.resolve();
  if(_lwcPromise) return _lwcPromise;
  _lwcPromise=new Promise((resolve,reject)=>{
    const s=document.createElement('script');
    s.src='/vendor/lightweight-charts-4.1.3.standalone.min.js';
    s.async=true;
    s.onload=()=>resolve();
    s.onerror=()=>{_lwcPromise=null;reject(new Error('lightweight-charts load failed'));};
    document.head.appendChild(s);
  });
  return _lwcPromise;
}
function _eeInit(){
  const el=document.getElementById('ee-chart');
  if(!el||_eeChart) return;
  if(typeof LightweightCharts==='undefined'){
    // Kick off the lazy load (idempotent — uses cached Promise) and
    // re-attempt init once the script lands. renderEntryExitChart()
    // calls _eeInit on every sample, so we'll also catch up that way.
    _loadLightweightCharts().then(_eeInit).catch(()=>{});
    return;
  }
  const c=_eeThemeColors();
  _eeChart=LightweightCharts.createChart(el,{
    layout:{background:{type:'solid',color:'rgba(0,0,0,0)'},textColor:c.text,fontFamily:'JetBrains Mono, monospace',fontSize:10},
    grid:{vertLines:{color:c.grid},horzLines:{color:c.grid}},
    rightPriceScale:{borderColor:c.border,scaleMargins:{top:0.12,bottom:0.12},visible:true},
    timeScale:{borderColor:c.border,timeVisible:true,secondsVisible:_eeTfSec<=60,rightOffset:6,barSpacing:8},
    crosshair:{mode:LightweightCharts.CrosshairMode.Normal,vertLine:{color:c.cross,style:2,width:1},horzLine:{color:c.cross,style:2,width:1}},
    localization:{priceFormatter:v=>(v>=0?'+':'')+v.toFixed(4)+'%'},
  });
  // TradingView-style: single price axis. Both In (entry divergence) and
  // Out (exit divergence) render as candles so the trader can read body
  // direction/size the same way for each. Out uses a translucent palette
  // so it stays secondary to In but remains fully structured (OHLC + wicks).
  _eeInSeries=_eeChart.addCandlestickSeries({
    upColor:c.inUp, downColor:c.inDown,
    borderUpColor:c.inUp, borderDownColor:c.inDown,
    wickUpColor:c.inUp, wickDownColor:c.inDown,
    priceLineVisible:false,
    priceFormat:{type:'price',precision:4,minMove:0.0001},
    title:'In',
  });
  _eeOutSeries=_eeChart.addCandlestickSeries({
    upColor:c.outUp, downColor:c.outDown,
    borderUpColor:c.outUp, borderDownColor:c.outDown,
    wickUpColor:c.outWickUp, wickDownColor:c.outWickDown,
    priceLineVisible:false, lastValueVisible:true,
    priceFormat:{type:'price',precision:4,minMove:0.0001},
    title:'Out',
  });
  // Zero reference line so "In > 0 = positive entry divergence" reads visually.
  _eeInSeries.createPriceLine({price:0, color:c.zero||c.border, lineStyle:2, lineWidth:1, axisLabelVisible:false, title:''});
  const ro=new ResizeObserver(()=>{if(_eeChart) _eeChart.applyOptions({width:el.clientWidth,height:el.clientHeight});});
  ro.observe(el);
  _eeChart.applyOptions({width:el.clientWidth,height:el.clientHeight});
}

function renderEntryExitChart(){
  if(_eeHist.length<2) return;
  _eeInit();
  if(!_eeChart) return;
  const loading=document.getElementById('ee-loading');
  const chartEl=document.getElementById('ee-chart');
  if(loading) loading.style.display='none';
  if(chartEl) chartEl.style.display='block';
  const {inArr,outArr}=_eeBuildCandles();
  if(!inArr.length) return;
  // Update last candle incrementally if possible to avoid disturbing user's pan/zoom
  _eeInSeries.setData(inArr);
  _eeOutSeries.setData(outArr);
  if(_eeNeedsFit){
    _eeChart.timeScale().fitContent();
    _eeNeedsFit=false;
  }
}

let _bookInterval=null;
let _bookWs=null;
let _bookWsLastMsgTs=0;
// Per-side last update timestamps — used by the staleness check below.
// `0` means "no data yet"; updated each /ws/book frame / /ws/trades event.
let _lastBookSideTs={long:0,short:0};
let _lastTradeSideTs={long:0,short:0};
// Toast cooldown — don't re-fire warning more than once per minute per side.
let _staleToastLast={long:0,short:0};
let _staleNoticeShown={long:false,short:false};

// ── Data-freshness watchdog (per side) ────────────────────────────────────
// Two distinct issues we surface to the user:
//
//   1) "No data at all yet" — page loaded, /ws/book connected, but
//      <venue>:<symbol> never appeared in any frame. Either the venue
//      doesn't list this symbol (e.g. low-cap meme on Backpack), the
//      go-fetcher is still warming up the subscription, or there's a
//      live outage. We give 12 s of grace, then toast once.
//
//   2) "Data is now stale" — used to get fresh frames, now we haven't
//      seen one in >15 s. Could be a venue WS hiccup or a market that
//      just stopped trading. Quieter cooldown (60 s) so the user isn't
//      spammed if multiple symbols go quiet in sequence.
//
// Banner-style indicator on the venue card is the long-term home; toast
// is the catch-the-eye signal at first-arrival.
function _checkBookStaleness(){
  const now = Date.now();
  // grace period: time since page boot
  if (!window._arbBootTs) window._arbBootTs = now;
  const sincePageBoot = now - window._arbBootTs;
  const exName = (s)=> EX_LABEL[s==='long'?LONG:SHORT] || (s==='long'?LONG:SHORT);
  ['long','short'].forEach(side => {
    const lastTs = _lastBookSideTs[side];
    const venue  = exName(side);
    // (1) no data at all
    if (!lastTs) {
      if (sincePageBoot > 12000 && !_staleNoticeShown[side]) {
        _staleNoticeShown[side] = true;
        try {
          toast({
            title: `Нет данных по ${SYM} на ${venue}`,
            sub:   'Возможно символ не торгуется на этой бирже или фид прогревается',
            type:  'warn',
            duration: 6000,
          });
        } catch (_) {}
        _setStaleBadge(side, true, 'no data');
      }
      return;
    }
    // (2) data going stale
    const ageMs = now - lastTs;
    if (ageMs > 15000) {
      _setStaleBadge(side, true, `${Math.round(ageMs/1000)}s ago`);
      if (now - _staleToastLast[side] > 60000) {
        _staleToastLast[side] = now;
        try {
          toast({
            title: `Задержка данных ${venue}`,
            sub:   `Последнее обновление ${Math.round(ageMs/1000)}s назад`,
            type:  'warn',
            duration: 5000,
          });
        } catch (_) {}
      }
    } else {
      _setStaleBadge(side, false);
    }
  });
}

function _setStaleBadge(side, stale, label){
  // Mark the venue card itself with a CSS class — styling defined inline
  // below so this works without touching design.css.
  const card = document.querySelector(`.ob-card-${side}`) || document.getElementById(`${side}-card`) || document.querySelector(`[data-side="${side}"]`);
  if (!card) return;
  let badge = card.querySelector('.stale-badge');
  if (stale) {
    if (!badge) {
      badge = document.createElement('div');
      badge.className = 'stale-badge';
      badge.style.cssText = 'position:absolute;top:6px;right:6px;padding:2px 6px;border-radius:6px;background:#3a1f1f;color:#fca5a5;font-size:10px;font-weight:600;z-index:5;border:1px solid #5b2a2a';
      const cs = getComputedStyle(card);
      if (cs.position === 'static') card.style.position = 'relative';
      card.appendChild(badge);
    }
    badge.textContent = label ? `⚠ ${label}` : '⚠ stale';
  } else if (badge) {
    badge.remove();
  }
}

// Kick the watchdog every 3s after the page boots.
setTimeout(() => setInterval(_checkBookStaleness, 3000), 3000);

let _bookWsBackoff=1000;

function _bookPairFor(side){
  return (side==='long'?LONG:SHORT).toLowerCase()+':'+SYM.toUpperCase();
}

function _openBookWs(){
  if(_bookWs && (_bookWs.readyState===0 || _bookWs.readyState===1)) return;
  const proto=location.protocol==='https:'?'wss':'ws';
  const url=`${proto}://${location.host}/api/screener/ws/book`;
  const token=Auth.getToken();
  if(!token) return;
  let ws;
  try{ ws=new WebSocket(url); }catch(_){ return; }
  _bookWs=ws;
  ws.onopen=()=>{
    _bookWsBackoff=1000;
    try{
      // Auth must be the first frame; server closes 4401 otherwise.
      ws.send(JSON.stringify({auth:token}));
      ws.send(JSON.stringify({action:'subscribe', pairs:[_bookPairFor('long'),_bookPairFor('short')]}));
    }catch(_){}
  };
  // rAF ids — ensures renderBook() fires at most once per animation frame
  // even when the exchange pushes faster (Bybit 20ms, Binance 100ms).
  const _bookRafId={long:null,short:null};
  ws.onmessage=(e)=>{
    _bookWsLastMsgTs=Date.now();
    let msg; try{ msg=JSON.parse(e.data); }catch(_){ return; }
    const books=msg && msg.books;
    if(!books) return;
    const longPair=_bookPairFor('long'), shortPair=_bookPairFor('short');
    if(books[longPair]){
      _lastBookSideTs.long=Date.now();
      const d=books[longPair];
      _lastBookData.long=d;
      // Instant fast path: update top-of-book vars + in/out on every frame.
      const la=d.asks||[], lb=d.bids||[];
      _liveAskLong=la[0]?.[0]||0;
      _liveMidLong=(lb[0]?.[0]&&la[0]?.[0])?(lb[0][0]+la[0][0])/2:(lb[0]?.[0]||la[0]?.[0]||0);
      sampleEntryExit(); updateLiveSpread();
      // Heavy DOM render throttled to animation-frame rate (≤60fps).
      if(!_bookRafId.long){
        _bookRafId.long=requestAnimationFrame(()=>{ _bookRafId.long=null; if(_lastBookData.long) renderBook(_lastBookData.long,'long'); });
      }
    }
    if(books[shortPair]){
      _lastBookSideTs.short=Date.now();
      const d=books[shortPair];
      _lastBookData.short=d;
      // Instant fast path: update top-of-book vars + in/out on every frame.
      const sb=d.bids||[], sa=d.asks||[];
      _liveBidShort=sb[0]?.[0]||0;
      _liveMidShort=(sb[0]?.[0]&&sa[0]?.[0])?(sb[0][0]+sa[0][0])/2:(sb[0]?.[0]||sa[0]?.[0]||0);
      sampleEntryExit(); updateLiveSpread();
      // Heavy DOM render throttled to animation-frame rate (≤60fps).
      if(!_bookRafId.short){
        _bookRafId.short=requestAnimationFrame(()=>{ _bookRafId.short=null; if(_lastBookData.short) renderBook(_lastBookData.short,'short'); });
      }
    }
  };
  ws.onclose=()=>{
    _bookWs=null;
    if (_Idle.shouldStayClosed()) return;  // idle-killed → stay closed
    // Exponential backoff reconnect, capped at 10s.
    setTimeout(_openBookWs, Math.min(_bookWsBackoff, 10000));
    _bookWsBackoff=Math.min(_bookWsBackoff*2, 10000);
  };
  ws.onerror=()=>{ try{ ws.close(); }catch(_){} };
}
// Register with the idle tracker so we close on idle and reopen on activity.
_Idle.onWake({
  close: () => { if (_bookWs && _bookWs.readyState <= 1) try { _bookWs.close(4000, 'idle'); } catch (_) {} },
  open:  () => _openBookWs(),
});

function startBooks(){
  // 1) One HTTP fetch per side for fast first paint. 2) Live WS subscription
  // replaces the old 150ms polling — the server pushes diffs from books.json
  // which is maintained by the fetcher. 3) HTTP fallback every 2s if the
  // WS channel has gone silent (disconnect / proxy timeout).
  fetchBook(LONG,'long'); fetchBook(SHORT,'short');
  _openBookWs();
  _openTradesWs();
  _bookInterval=setInterval(()=>{
    if(document.hidden) return;
    const silentMs=Date.now()-_bookWsLastMsgTs;
    if(!_bookWs || _bookWs.readyState!==1 || silentMs>3000){
      fetchBook(LONG,'long'); fetchBook(SHORT,'short');
    }
  }, 2000);
}

// ── /ws/trades — per-fill stream that pulses matching price-level rows in
// the depth book. This is the layer that gives the UI "arbion-style" tick
// frequency (20-100+/sec on hot pairs) — depth alone caps at the venue's
// 100-500ms snapshot/diff cadence. ──────────────────────────────────────
let _tradesWs=null, _tradesWsBackoff=1000;
function _tradesPairFor(side){ return (side==='long'?LONG:SHORT).toLowerCase()+':'+SYM.toUpperCase(); }

function _flashTradeRow(side, price, taker){
  // Buys hit asks (taker bought from ask); sells hit bids (taker sold into bid).
  // Render: buy → ask row pulse green; sell → bid row pulse red.
  const targetSide = (taker === 'B') ? 'asks' : 'bids';
  const container = document.getElementById(`${targetSide}-${side}`);
  const sel = (taker === 'B') ? '.ask-price' : '.bid-price';
  const cls = (taker === 'B') ? 'tp-buy' : 'tp-sell';
  // Pulse the activity dot regardless of price-match outcome — always
  // gives visual confirmation that a trade came in.
  _flashTradeDot(side, taker);
  if (!container) return;
  const px = +price;
  const rows = container.querySelectorAll('.book-row');
  let best = null, bestDelta = Infinity;
  for (const r of rows) {
    // Pool rows that aren't currently bound to a level are display:none.
    // Their textContent still holds the last value they showed; skip them
    // so trade flashes can't latch onto a stale price.
    if (r.style.display === 'none') continue;
    const p = parseFloat((r.querySelector(sel)?.textContent || '').replace(/[,\s]/g,''));
    if (!isFinite(p)) continue;
    const d = Math.abs(p - px);
    if (d < bestDelta) { bestDelta = d; best = r; }
  }
  if (!best) return;
  best.classList.remove('tp-buy','tp-sell');
  // eslint-disable-next-line no-unused-expressions
  best.offsetWidth;
  best.classList.add(cls);
  setTimeout(() => best.classList.remove(cls), 720);
}

// Activity dot — renders on first call, then pulses on each subsequent trade.
function _flashTradeDot(side, taker){
  // Look for an existing dot anchored to the venue card header. Card
  // selectors vary by layout — try a few common spots.
  let anchor =
       document.querySelector(`#mid-${side}`)
    || document.querySelector(`#book-${side}-price`)
    || document.querySelector(`[data-side="${side}"] .ob-card-header`);
  if (!anchor) return;
  let dot = anchor.parentElement.querySelector(`.trade-activity-dot[data-side="${side}"]`);
  if (!dot) {
    dot = document.createElement('span');
    dot.className = 'trade-activity-dot';
    dot.setAttribute('data-side', side);
    anchor.parentElement.appendChild(dot);
  }
  const cls = (taker === 'B') ? 'tp-active-buy' : 'tp-active-sell';
  dot.classList.remove('tp-active-buy','tp-active-sell');
  // eslint-disable-next-line no-unused-expressions
  dot.offsetWidth;
  dot.classList.add(cls);
  setTimeout(() => dot.classList.remove(cls), 720);
}

function _openTradesWs(){
  if(_tradesWs && (_tradesWs.readyState===0 || _tradesWs.readyState===1)) return;
  const proto=location.protocol==='https:'?'wss':'ws';
  const url=`${proto}://${location.host}/api/screener/ws/trades`;
  // Public feed — auth optional; pass token if logged in for uid tagging.
  const token=Auth.getToken() || '';
  let ws;
  try{ ws=new WebSocket(url); }catch(_){ return; }
  _tradesWs=ws;
  ws.onopen=()=>{
    _tradesWsBackoff=1000;
    try{
      ws.send(JSON.stringify({auth:token}));
      ws.send(JSON.stringify({action:'subscribe', pairs:[_tradesPairFor('long'),_tradesPairFor('short')]}));
    }catch(_){}
  };
  const longPair=_tradesPairFor('long'), shortPair=_tradesPairFor('short');
  // rAF-coalesced trade-drain. Hot Binance pairs hit 100-300 trades/sec;
  // doing flash + entry-resample on every trade is what made /arb burn
  // CPU and grow memory (the per-trade setTimeout(720) backlog alone
  // could pile up to thousands of pending timers on busy pairs).
  // Drain queue once per frame: only flash the LAST trade per side
  // (older flashes would be invisible inside one frame anyway),
  // single sampleEntryExit + updateLiveSpread per frame.
  let _tradeQ = [];
  let _tradeRaf = null;
  function _drainTradeQ(){
    _tradeRaf = null;
    if (!_tradeQ.length) return;
    const q = _tradeQ; _tradeQ = [];
    let touched = false;
    const lastFlash = new Map(); // side -> last trade for that side
    for (const t of q) {
      const px = +t.p;
      if (px > 0) {
        if (t.side === 'long')  { _liveMidLong  = px; if (t.d === 'B') _liveAskLong  = px; }
        if (t.side === 'short') { _liveMidShort = px; if (t.d === 'S') _liveBidShort = px; }
      }
      lastFlash.set(t.side, t);
      touched = true;
    }
    for (const [, t] of lastFlash) _flashTradeRow(t.side, t.p, t.d);
    if (touched && typeof sampleEntryExit === 'function') {
      sampleEntryExit();
      if (typeof updateLiveSpread === 'function') updateLiveSpread();
    }
  }
  ws.onmessage=(e)=>{
    let msg; try{ msg=JSON.parse(e.data); }catch(_){ return; }
    const arr = msg && msg.trades;
    if (!Array.isArray(arr)) return;
    for (const t of arr) {
      // t = {e:exchange,s:symbol,p:price,q:size,d:'B'|'S',t:ts}
      const pair = (t.e||'').toLowerCase() + ':' + (t.s||'').toUpperCase();
      const side = (pair === longPair) ? 'long' : (pair === shortPair ? 'short' : null);
      if (!side) continue;
      _lastTradeSideTs[side] = Date.now();
      _tradeQ.push({side, p: t.p, d: t.d});
    }
    if (_tradeQ.length && _tradeRaf === null) {
      _tradeRaf = requestAnimationFrame(_drainTradeQ);
    }
  };
  ws.onclose=()=>{
    _tradesWs=null;
    if (_Idle.shouldStayClosed()) return;
    setTimeout(_openTradesWs, Math.min(_tradesWsBackoff, 10000));
    _tradesWsBackoff=Math.min(_tradesWsBackoff*2, 10000);
  };
  ws.onerror=()=>{ try{ ws.close(); }catch(_){} };
}

// ── Funding history table ─────────────────────────────────────────────────────
function buildHistTable(){
  const merged=_longHist.map(l=>{
    const s=_shortHist.reduce((a,b)=>Math.abs(b.ts-l.ts)<Math.abs(a.ts-l.ts)?b:a,{ts:Infinity,rate:null});
    return{ts:l.ts,long:l.rate,short:Math.abs(s.ts-l.ts)<900?s.rate:null};
  }).concat(_shortHist.filter(s=>!_longHist.some(l=>Math.abs(l.ts-s.ts)<900)).map(s=>({ts:s.ts,long:null,short:s.rate}))).sort((a,b)=>b.ts-a.ts);
  setT('hist-count',merged.length+' periods');
  setT('th-long',EX_LABEL[LONG]||LONG); setT('th-short',EX_LABEL[SHORT]||SHORT);
  const fR=v=>v!=null?`<span style="color:${v>0?'#1AFFAB':v<0?'#F87171':'#9B9FAB'}">${sign(v)}${v.toFixed(4)}%</span>`:`<span style="color:#3A3A50">—</span>`;
  const fN=v=>v!=null?`<span style="color:${v>=0?'#1AFFAB':'#F87171'}">${sign(v)}${v.toFixed(4)}%</span>`:`<span style="color:#3A3A50">—</span>`;
  const now=new Date();
  const fmtShort=ts=>{const dt=new Date(ts*1000);return dt.toDateString()===now.toDateString()?dt.toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',hour12:false}):dt.toLocaleDateString('en-US',{month:'short',day:'numeric'})+' '+dt.toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',hour12:false});};
  const histRows=document.getElementById('hist-rows');
  if(histRows) histRows.innerHTML=merged.slice(0,200).map(row=>{
    const lr=row.long!=null?row.long*100:null,sr=row.short!=null?row.short*100:null,net=(lr!=null&&sr!=null)?sr-lr:null;
    return`<div class="hist-row"><span>${fmtShort(row.ts)}</span><span>${fR(lr)}</span><span>${fR(sr)}</span><span>${fN(net)}</span></div>`;
  }).join('');
}

// ── WS live history update ────────────────────────────────────────────────────
let _lastNextTsLong=null,_lastNextTsShort=null,_prevLongRate=null,_prevShortRate=null;
function histPrependRow(ts,longRate,shortRate){
  const rows=document.getElementById('hist-rows');if(!rows)return;
  const spin=rows.querySelector('.empty');if(spin)spin.remove();
  const fR=v=>v!=null?`<span style="color:${v>0?'#1AFFAB':v<0?'#F87171':'#9B9FAB'}">${sign(v)}${v.toFixed(4)}%</span>`:`<span style="color:#3A3A50">—</span>`;
  const fN=v=>v!=null?`<span style="color:${v>=0?'#1AFFAB':'#F87171'}">${sign(v)}${v.toFixed(4)}%</span>`:`<span style="color:#3A3A50">—</span>`;
  const lr=longRate!=null?longRate*100:null,sr=shortRate!=null?shortRate*100:null,net=(lr!=null&&sr!=null)?sr-lr:null;
  const now=new Date(),dt=new Date(ts*1000);
  const lbl=dt.toDateString()===now.toDateString()?dt.toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',hour12:false}):dt.toLocaleDateString('en-US',{month:'short',day:'numeric'})+' '+dt.toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',hour12:false});
  const div=document.createElement('div');div.className='hist-row';div.style.background='rgba(26,255,171,.06)';
  div.innerHTML=`<span>${lbl}</span><span>${fR(lr)}</span><span>${fR(sr)}</span><span>${fN(net)}</span>`;
  rows.prepend(div);
  setTimeout(()=>{div.style.background='';},2500);
  const cnt=document.getElementById('hist-count');if(cnt){const n=parseInt(cnt.textContent)||0;cnt.textContent=(n+1)+' periods';}
}

// ── WS ────────────────────────────────────────────────────────────────────────
let _ws=null;
function connectWs(){
  const token=Auth.getToken();if(!token)return;
  const proto=location.protocol==='https:'?'wss':'ws';
  _ws=new WebSocket(`${proto}://${location.host}/api/screener/ws/long-short`);
  _ws.onopen=()=>{ try{ _ws.send(JSON.stringify({auth:token})); }catch{} };
  _ws.onmessage=ev=>{
    try{
      const data=JSON.parse(ev.data);
      // Broadcaster sends snapshot on connect, diffs thereafter. On a diff
      // we only need to scan added + updated — if nothing about THIS pair
      // changed this tick we just keep our last _opp.
      const pool = data.type === 'diff'
        ? [...(data.added||[]), ...(data.updated||[])]
        : (data.opportunities || []);
      const opp = pool.find(o=>o.symbol===SYM&&o.long_exchange===LONG&&o.short_exchange===SHORT);
      if(!opp)return;
      _opp=opp; _ssSave(opp); renderTopBar();
      if(_lastNextTsLong!==null&&opp.next_ts_long&&opp.next_ts_long!==_lastNextTsLong){
        histPrependRow(_lastNextTsLong,_prevLongRate,_prevShortRate);
        if(_prevLongRate!=null) _longHist.unshift({ts:_lastNextTsLong,rate:_prevLongRate});
        if(_prevShortRate!=null) _shortHist.unshift({ts:_lastNextTsShort||_lastNextTsLong,rate:_prevShortRate});
        if(_fundBuilt) renderFundChart();
        renderOverviewTables();
      }
      _lastNextTsLong=opp.next_ts_long||_lastNextTsLong;
      _lastNextTsShort=opp.next_ts_short||_lastNextTsShort;
      _prevLongRate=opp.long_rate; _prevShortRate=opp.short_rate;
    }catch{}
  };
  _ws.onclose=()=>{
    if (_Idle.shouldStayClosed()) return;
    setTimeout(connectWs,3000);
  };
  _ws.onerror=()=>_ws.close();
  setInterval(()=>{if(_ws?.readyState===WebSocket.OPEN)_ws.send('ping');},20000);
}
// Register trades + long-short WS with idle tracker.
_Idle.onWake({
  close: () => { if (_tradesWs && _tradesWs.readyState <= 1) try { _tradesWs.close(4000, 'idle'); } catch (_) {} },
  open:  () => _openTradesWs(),
});
_Idle.onWake({
  close: () => { if (_ws && _ws.readyState <= 1) try { _ws.close(4000, 'idle'); } catch (_) {} },
  open:  () => connectWs(),
});

// ── Live spread = In (orderbook entry basis) ──────────────────────────────────
// Per-user spec: Live Spread MUST equal In. Mark-based basis (price_spread)
// gets bizarre values when one venue's funding mark is stale (e.g. +8.99%
// when both books actually quote within 0.02%). in_pct comes from
// top-of-book bestBidShort − bestAskLong, refreshed every WS book tick on
// go-fetcher and broadcast on /ws/long-short. When in_pct is briefly null
// (book gap), keep the previous value rather than flipping to mark — the
// sticky cache on go-fetcher already smooths most drops.
let _liveMidLong=0, _liveMidShort=0;
let _liveAskLong=0, _liveBidShort=0;  // kept for orderbook display
let _lastLiveSpread = null;           // sticky local value
function _liveBasisPct(){
  // Prefer locally-computed value from fresh book data — sampleEntryExit()
  // runs on every book WS frame and uses the same top-of-book formula as
  // go-fetcher, so it's identical in meaning but updates instantly.
  if (typeof _eeHist !== 'undefined' && _eeHist.length) {
    const last = _eeHist[_eeHist.length - 1];
    if (last && typeof last.inPct === 'number') return last.inPct;
  }
  // Books not yet loaded — fall back to WS-broadcast value.
  if (_opp && typeof _opp.in_pct === 'number') return _opp.in_pct;
  return _lastLiveSpread;             // fall through to last good
}
function updateLiveSpread(){
  const sp=_liveBasisPct();
  if(sp===null) return;
  _lastLiveSpread = sp;
  const cls=sp>=0?'pos':'neg';
  setH('tb-live-spread',`<span class="${cls}">${sign(sp)}${sp.toFixed(4)}%</span>`);
}

// Mirror live-spread into b-spread + recompute calc.
setInterval(()=>{
  if(document.hidden) return;
  const sp=_liveBasisPct();
  if(sp===null) return;
  const cls=sp<=0?'pos':'neg';
  setH('b-spread',`<span class="${cls}">${sign(sp)}${sp.toFixed(4)}%</span>`);
  calcUpdate();
},3000);

// ── Open Interest ─────────────────────────────────────────────────────────────
async function loadOI(){
  setT('oi-long-ex',EX_LABEL[LONG]||LONG); setT('oi-short-ex',EX_LABEL[SHORT]||SHORT);
  setT('oi-card-long-lbl',EX_LABEL[LONG]||LONG); setT('oi-card-short-lbl',EX_LABEL[SHORT]||SHORT);
  try{
    const res=await Auth.apiFetch(`/screener/open-interest?symbol=${SYM}&long_ex=${LONG}&short_ex=${SHORT}`);
    if(!res.ok) return;
    const d=await res.json();
    const fmtOI=v=>v!=null?fmtV(v):'—';
    const lo=d.open_interest?.[LONG], so=d.open_interest?.[SHORT];
    // For HL: show USD value (oi × mark). For others: show contracts.
    const fmtEntry=(ex,e)=>{
      if(!e) return {val:'—',label:' contracts'};
      if(ex==='hyperliquid' && e.oi_usd){
        return {val:fmtOI(e.oi_usd), label:' USD'};
      }
      return {val:fmtOI(e.oi), label:' contracts'};
    };
    const loFmt=fmtEntry(LONG,lo), soFmt=fmtEntry(SHORT,so);
    setT('oi-long-val',loFmt.val+loFmt.label); setT('oi-short-val',soFmt.val+soFmt.label);
    setT('oi-card-long',loFmt.val); setT('oi-card-short',soFmt.val);
    // Store OI USD for HL trade-card warning
    window._hlOiUsd = (LONG==='hyperliquid' && lo?.oi_usd) ? lo.oi_usd
                    : (SHORT==='hyperliquid' && so?.oi_usd) ? so.oi_usd : 0;
  }catch{}
}

// ── All Rates tab ─────────────────────────────────────────────────────────────
let _allRatesLoaded=false;
async function loadAllRates(){
  if(_allRatesLoaded) return;
  const spinner=document.getElementById('all-rates-spinner');
  if(spinner) spinner.style.display='inline-block';
  try{
    const res=await Auth.apiFetch(`/screener/all-exchanges-funding?symbol=${SYM}`);
    if(!res.ok) throw new Error();
    const d=await res.json();
    renderAllRates(d.rates||[]);
    setT('all-rates-count',d.rates?.length||0);
    _allRatesLoaded=true;
  }catch{
    document.getElementById('all-ex-list').innerHTML='<div class="empty">Failed to load</div>';
  }finally{if(spinner)spinner.style.display='none';}
}
function renderAllRates(rates){
  if(!rates.length){document.getElementById('all-ex-list').innerHTML='<div class="empty">Not listed</div>';return;}
  const maxAbs=Math.max(...rates.map(r=>Math.abs(r.rate*100*8/r.interval_h)),.0001);
  const rows=rates.map(r=>{
    const rate8h=r.rate*(8/r.interval_h)*100;
    const cls=rate8h>0?'pos':rate8h<0?'neg':'neu';
    const barColor=rate8h>0?'var(--green)':'var(--red)';
    const barW=(Math.abs(rate8h)/maxAbs*100).toFixed(1);
    const isLong=r.exchange===LONG,isShort=r.exchange===SHORT;
    const badgeText=document.body.classList.contains('light')?'#FFF':'#000';
    const badge=isLong?`<span style="font-size:8px;color:${badgeText};background:var(--green);border-radius:3px;padding:1px 4px;font-weight:700">LONG</span>`:
                isShort?`<span style="font-size:8px;color:${badgeText};background:var(--red);border-radius:3px;padding:1px 4px;font-weight:700">SHORT</span>`:'';
    return`<div class="all-ex-row">
      <span class="all-ex-dot dot-${r.exchange}"></span>
      <span class="all-ex-name">${EX_LABEL[r.exchange]||r.exchange}</span>
      ${badge}
      <div class="all-ex-bar-wrap"><div class="all-ex-bar" style="width:${barW}%;background:${barColor}"></div></div>
      <span class="all-ex-rate ${cls}">${sign(rate8h)}${Math.abs(rate8h).toFixed(4)}%</span>
      <span class="all-ex-ivl">${r.interval_h}h</span>
    </div>`;
  });
  document.getElementById('all-ex-list').innerHTML=rows.join('');
}

// ── Theme toggle ──────────────────────────────────────────────────────────────
// Theme toggle + persistence handled globally by /theme.js.
// Re-render charts on theme change.
window.addEventListener('themechange',()=>{
  try{_eeApplyTheme();}catch{}
  try{if(_spreadBuilt) renderSpreadChart();}catch{}
  try{if(_fundBuilt) renderFundChart();}catch{}
});
// Migration: honor old per-page key if present
(()=>{try{const old=localStorage.getItem('arb-theme');if(old&&!localStorage.getItem('theme')){localStorage.setItem('theme',old);if(old==='light')document.body.classList.add('light');}}catch{}})();

// ── Fullscreen ────────────────────────────────────────────────────────────────
function toggleFullscreen(){
  if(!document.fullscreenElement){
    document.documentElement.requestFullscreen().catch(()=>{});
  }else{document.exitFullscreen();}
}
document.addEventListener('fullscreenchange',()=>{
  const icon=document.getElementById('fs-icon');
  if(document.fullscreenElement) icon.innerHTML='<path d="M6 1H1v5M15 6V1h-5M6 15H1v-5M15 10v5h-5"/>';
  else icon.innerHTML='<path d="M1 6V1h5M15 6V1h-5M1 10v5h5M15 10v5h-5"/>';
});

// ── Navigate modal ────────────────────────────────────────────────────────────
// Sourced from /api/meta/venues via exchanges.js — fallback to the static
// list while the meta fetch is in flight (fresh-load race).
const _EXCHANGES_FALLBACK=['binance','bybit','okx','gate','kucoin','mexc','bitget','hyperliquid','aster','ethereal','whitebit','bingx','htx','paradex','extended','lighter'];
function _exchangesList(){
  const lst = (window.EX && window.EX.lists && window.EX.lists.screener_all) || [];
  return lst.length ? lst : _EXCHANGES_FALLBACK;
}
// Legacy name kept for places that read it eagerly during page bootstrap.
let _EXCHANGES = _exchangesList();
if (window.EX && window.EX.ready) {
  window.EX.ready.then(() => { _EXCHANGES = _exchangesList(); });
}
// ── Symbol / exchange popovers ────────────────────────────────────────────────
let _allSymbols=[];
let _popState={items:[],hi:0,type:null,side:null,onPick:null};

async function _loadAllSymbols(){
  if(_allSymbols.length) return _allSymbols;
  try{
    const r=await Auth.apiFetch('/screener/funding');
    if(r.ok){
      const d=await r.json();
      const set=new Set((d.rows||[]).map(x=>x.symbol));
      _allSymbols=[...set].sort();
    }
  }catch{}
  return _allSymbols;
}

function _positionPopover(anchor){
  const pop=document.getElementById('ap-pop');
  const r=anchor.getBoundingClientRect();
  pop.style.left=Math.max(8,r.left)+'px';
  pop.style.top=(r.bottom+4)+'px';
}

function _openPop(anchor,{items,current,placeholder,onPick}){
  const pop=document.getElementById('ap-pop');
  const search=document.getElementById('ap-search-input');
  search.placeholder=placeholder||'Search…';
  search.value='';
  _popState={items:items.slice(),_all:items.slice(),hi:0,current,onPick};
  _renderPop(items);
  pop.classList.add('open');
  _positionPopover(anchor);
  setTimeout(()=>search.focus(),0);
}

function _renderPop(list){
  const el=document.getElementById('ap-list');
  if(!list.length){el.innerHTML='<div class="ap-empty">No matches</div>';return;}
  _popState.items=list;
  el.innerHTML=list.map((it,i)=>{
    const isCur=it.value===_popState.current;
    const dot=it.color?`<span class="dot" style="background:${it.color}"></span>`:'';
    return `<div class="ap-item${isCur?' current':''}${i===_popState.hi?' hi':''}" data-i="${i}" onclick="_pickPop(${i})">${dot}<span>${it.label}</span></div>`;
  }).join('');
}

function _pickPop(i){
  const it=_popState.items[i];
  if(!it) return;
  closePopover();
  _popState.onPick&&_popState.onPick(it.value);
}

function closePopover(){document.getElementById('ap-pop').classList.remove('open');}

function openSymbolPopover(anchor){
  const typeParam = TYPE === 'spot' ? '&type=spot-short' : (TYPE === 'dex' ? '&type=dex-short' : '');
  const onPick=v=>{location.href=`/arb?symbol=${v}&long=${LONG}&short=${SHORT}${typeParam}`;};
  // Open immediately with whatever we have (cached or placeholder) — no waiting on fetch
  const cached=_allSymbols.length?_allSymbols:[SYM];
  _openPop(anchor,{items:cached.map(s=>({value:s,label:s})),current:SYM,placeholder:'Search token…',onPick});
  // Refresh list in background if cache empty
  if(!_allSymbols.length){
    _loadAllSymbols().then(syms=>{
      if(!syms.length) return;
      _popState._all=syms.map(s=>({value:s,label:s}));
      _popState.items=_popState._all;
      _renderPop(_popState._all);
    });
  }
}

function swapExchanges(){
  location.href=`/arb?symbol=${SYM}&long=${SHORT}&short=${LONG}`;
}

async function openExPopover(anchor,side){
  const current=side==='long'?LONG:SHORT;
  const other=side==='long'?SHORT:LONG;
  // Spot/short mode: LONG leg is a spot venue. Same-venue is allowed
  // (powers the funding-arb play) so we don't filter `other` out.
  if (TYPE === 'spot' && side === 'long') {
    const SPOT_VENUES = ['binance','bybit','okx','gate','kucoin','mexc','bitget','bingx','htx'];
    const items = SPOT_VENUES.map(e => ({
      value: e,
      label: (EX_LABEL[e]||e) + (e === SHORT ? ' · same as short' : ''),
      color: EX_COLOR[e],
    }));
    _openPop(anchor,{items,current,placeholder:'Search spot venue…',
      onPick:v=>{
        location.href=`/arb?type=spot-short&symbol=${SYM}&long=${v}&short=${SHORT}`;
      }});
    return;
  }
  // Show loading popover first, fetch listed exchanges, then replace list
  _openPop(anchor,{items:[{value:'__loading',label:'Loading…'}],current,placeholder:'Search exchange…',onPick:()=>{}});
  let listed=_EXCHANGES;
  try{
    const r=await Auth.apiFetch(`/screener/all-exchanges-funding?symbol=${SYM}`);
    if(r.ok){
      const d=await r.json();
      listed=(d.rates||[]).map(x=>x.exchange);
    }
  }catch{}
  const items=listed.filter(e=>e!==other).map(e=>({value:e,label:EX_LABEL[e]||e,color:EX_COLOR[e]}));
  _openPop(anchor,{items,current,placeholder:'Search exchange…',
    onPick:v=>{
      const newLong=side==='long'?v:LONG;
      const newShort=side==='short'?v:SHORT;
      const typeParam = TYPE === 'spot' ? '&type=spot-short' : (TYPE === 'dex' ? '&type=dex-short' : '');
      location.href=`/arb?symbol=${SYM}&long=${newLong}&short=${newShort}${typeParam}`;
    }});
}

// Popover keyboard + outside click
document.addEventListener('click',e=>{
  const pop=document.getElementById('ap-pop');
  if(!pop.classList.contains('open')) return;
  if(pop.contains(e.target)) return;
  if(e.target.closest('.hero-sym')||e.target.closest('.hero-ex')) return;
  closePopover();
});
document.addEventListener('keydown',e=>{
  const pop=document.getElementById('ap-pop');
  if(!pop.classList.contains('open')) return;
  if(e.key==='Escape'){closePopover();return;}
  if(e.key==='ArrowDown'||e.key==='ArrowUp'){
    e.preventDefault();
    const n=_popState.items.length; if(!n) return;
    _popState.hi=(e.key==='ArrowDown'?_popState.hi+1:_popState.hi-1+n)%n;
    _renderPop(_popState.items);
    const hi=document.querySelector('#ap-list .hi'); if(hi) hi.scrollIntoView({block:'nearest'});
  }else if(e.key==='Enter'){e.preventDefault();_pickPop(_popState.hi);}
});
document.getElementById('ap-search-input').addEventListener('input',e=>{
  const q=e.target.value.trim().toLowerCase();
  const all=_popState._all||[];
  const filtered=q?all.filter(it=>it.label.toLowerCase().includes(q)||it.value.toLowerCase().includes(q)):all;
  _popState.hi=0;
  _renderPop(filtered);
});

function openNavModal(){
  const modal=document.getElementById('nav-modal');
  ['nav-long','nav-short'].forEach(id=>{
    const sel=document.getElementById(id);
    sel.innerHTML=_EXCHANGES.map(e=>`<option value="${e}" ${(id==='nav-long'?LONG:SHORT)===e?'selected':''}>${EX_LABEL[e]||e}</option>`).join('');
  });
  document.getElementById('nav-symbol').value=SYM;
  modal.classList.add('open');
  document.getElementById('nav-symbol').focus();
}
function closeNavModal(){document.getElementById('nav-modal').classList.remove('open');}
function navGo(){
  const sym=(document.getElementById('nav-symbol').value||'').toUpperCase().replace('USDT','');
  const le=document.getElementById('nav-long').value;
  const se=document.getElementById('nav-short').value;
  if(!sym){Confirm.tell({title:'Missing symbol', message:'Enter a symbol first.', okText:'OK'});return;}
  if(le===se){Confirm.tell({title:'Same exchange', message:'Long and short must differ.', okText:'OK'});return;}
  location.href=`/arb?symbol=${sym}&long=${le}&short=${se}`;
}
document.getElementById('nav-modal').addEventListener('click',e=>{if(e.target===e.currentTarget)closeNavModal();});
document.getElementById('nav-symbol').addEventListener('keydown',e=>{if(e.key==='Enter')navGo();if(e.key==='Escape')closeNavModal();});

// ── Alerts ────────────────────────────────────────────────────────────────────
let _alerts=[];
let _hasTgUsername=false;
function _toast(msg, ok=false, sub=''){
  window.toast({title:msg, type:ok?'success':'error', sub});
}
function _setAddBtnState(){
  const btn=document.getElementById('al-submit');
  if(!btn) return;
  btn.disabled=!_hasTgUsername;
  btn.title=_hasTgUsername?'':'Set Telegram username in profile first';
}
async function openAlertModal(){
  document.getElementById('alert-modal').classList.add('open');
  setT('alert-pair-sym',SYM+'USDT');
  setT('alert-pair-long',EX_LABEL[LONG]||LONG);
  setT('alert-pair-short',EX_LABEL[SHORT]||SHORT);
  setT('al-current-pair',(EX_LABEL[LONG]||LONG)+' → '+(EX_LABEL[SHORT]||SHORT));
  const dl=document.getElementById('alert-pair-dot-long'); if(dl) dl.style.background=EX_COLOR[LONG]||'#888';
  const ds=document.getElementById('alert-pair-dot-short'); if(ds) ds.style.background=EX_COLOR[SHORT]||'#888';
  await loadAlerts();
  // Check TG username
  try{
    const me=await Auth.apiFetch('/auth/me');
    if(me.ok){
      const u=await me.json();
      _hasTgUsername=!!u.tg_username;
      document.getElementById('alert-tg-note').style.display=_hasTgUsername?'none':'';
    } else {
      _hasTgUsername=false;
    }
  }catch{_hasTgUsername=false;}
  _setAddBtnState();
}
function closeAlertModal(){document.getElementById('alert-modal').classList.remove('open');}
document.getElementById('alert-modal').addEventListener('click',e=>{if(e.target===e.currentTarget)closeAlertModal();});
function _refreshAlertCount(){
  const mine=(_alerts||[]).filter(a=>a.symbol===SYM&&_alertMode(a)===TYPE&&(
    (a.long_exchange===LONG&&a.short_exchange===SHORT)||(a.long_exchange==='*'&&a.short_exchange==='*')
  ));
  const active=mine.filter(a=>a.enabled).length;
  const el=document.getElementById('tb-alert-count');
  if(el){el.textContent=active; el.className='metric-val'+(active>0?' pos':' neu'); el.style.fontSize='20px';}
}
async function loadAlerts(){
  const list=document.getElementById('alert-list');
  if(list) list.innerHTML='<div class="empty"><span class="spinner"></span></div>';
  try{
    const res=await Auth.apiFetch('/alerts');
    if(!res.ok) throw new Error(`HTTP ${res.status}`);
    _alerts=await res.json();
    if(list) renderAlertList();
    _refreshAlertCount();
  }catch(err){
    if(list) list.innerHTML='<div class="alert-empty"><div>Failed to load alerts. Check your connection.</div></div>';
  }
}
function _alertMode(a){ return a.mode || 'futures'; }
function renderAlertList(){
  const list=document.getElementById('alert-list');
  // Show alerts for THIS exact pair (matching mode) + any-exchange alerts on THIS symbol+mode
  const mine=_alerts.filter(a =>
    a.symbol===SYM && _alertMode(a)===TYPE && (
      (a.long_exchange===LONG && a.short_exchange===SHORT) ||
      (a.long_exchange==='*'   && a.short_exchange==='*')
    )
  );
  if(!mine.length){
    list.innerHTML=`<div class="alert-empty">
      <div class="alert-empty-icon"><svg width="22" height="22" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 2a5 5 0 0 1 5 5v3l1 2H2l1-2V7a5 5 0 0 1 5-5z"/><path d="M6.5 13.5a1.5 1.5 0 0 0 3 0"/></svg></div>
      <div>No alerts yet. Set a threshold above to get pinged on Telegram when the in-spread crosses it.</div>
    </div>`;
    return;
  }
  const dirLabel={any:'Any side',above:'In-spread ≥ +',below:'In-spread ≤ −'};
  list.innerHTML=mine.map(a=>{
    const isAny = a.long_exchange==='*' && a.short_exchange==='*';
    const scopeBadge = isAny ? '<span class="alert-item-scope">🔭 Any pair</span>' : '';
    const tIcon = (a.trigger_mode||'speed')==='protected' ? '🛡' : '⚡';
    return `<div class="alert-item" data-id="${a.id}">
      <div class="alert-item-info">
        <span class="alert-item-pair">${tIcon} ±${a.threshold}% in-spread ${scopeBadge}</span>
        <span class="alert-item-meta">${dirLabel[a.direction]||a.direction}<span class="sep"></span>${a.enabled?'active':'paused'}</span>
      </div>
      <button class="alert-item-toggle ${a.enabled?'enabled':''}" onclick="toggleAlert(${a.id})">${a.enabled?'ON':'OFF'}</button>
      <button class="alert-item-del" onclick="deleteAlert(${a.id})" title="Delete">×</button>
    </div>`;
  }).join('');
}
// ── Custom Direction dropdown ──
const _alDirLabels = {
  above: { text: 'Above · spread ≥ +threshold', ic: 'al-dd-ic-above', sym: '↑' },
  below: { text: 'Below · spread ≤ −threshold', ic: 'al-dd-ic-below', sym: '↓' },
};
function alDirToggle(ev){
  ev && ev.stopPropagation();
  const dd = document.getElementById('al-dir-dd');
  const open = !dd.classList.contains('open');
  dd.classList.toggle('open', open);
  dd.querySelector('.al-dd-btn').setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open) setTimeout(() => document.addEventListener('click', _alDirOutside, { once:false }), 0);
}
function _alDirOutside(e){
  const dd = document.getElementById('al-dir-dd');
  if (!dd) return;
  if (!dd.contains(e.target)) { dd.classList.remove('open'); document.removeEventListener('click', _alDirOutside); }
}
function alDirPick(btn){
  const v = btn.dataset.v;
  const dd = document.getElementById('al-dir-dd');
  document.getElementById('al-direction').value = v;
  dd.querySelectorAll('.al-dd-item').forEach(x => x.classList.toggle('is-active', x.dataset.v === v));
  const meta = _alDirLabels[v];
  const btnEl = dd.querySelector('.al-dd-btn');
  btnEl.querySelector('.al-dd-text').textContent = meta.text;
  const icEl = btnEl.querySelector('.al-dd-ic');
  icEl.className = 'al-dd-ic ' + meta.ic;
  icEl.textContent = meta.sym;
  dd.classList.remove('open');
  document.removeEventListener('click', _alDirOutside);
}

async function createAlert(){
  if(!_hasTgUsername){_toast('Set Telegram username in your profile first');return;}
  const thresholdRaw=document.getElementById('al-threshold').value;
  const threshold=parseFloat(thresholdRaw);
  if(!isFinite(threshold)||threshold<=0){_toast('Threshold must be a positive number');return;}
  const direction=document.getElementById('al-direction').value;
  const anyEx=document.getElementById('al-any-ex')?.checked;
  const btn=document.getElementById('al-submit');
  if(btn) btn.disabled=true;
  try{
    const trigger_mode = document.getElementById('al-trigger-mode')?.value || 'speed';
    const url = anyEx ? '/alerts/token' : '/alerts';
    const body = anyEx
      ? {symbol:SYM, threshold, direction, mode:TYPE, trigger_mode}
      : {symbol:SYM, long_exchange:LONG, short_exchange:SHORT, threshold, direction, mode:TYPE, trigger_mode};
    const res=await Auth.apiFetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(!res.ok){
      let msg='Failed to create alert';
      try{const e=await res.json(); msg=e.detail||msg;}catch{}
      _toast(msg);
      return;
    }
    const dirTxt={any:'±',above:'+',below:'−'}[direction]||'±';
    const scope = anyEx ? 'any exchange pair' : `${EX_LABEL[LONG]||LONG} / ${EX_LABEL[SHORT]||SHORT}`;
    _toast('Alert created',true,`<span class="mono">${SYM}</span> · ${scope} · trigger at <span class="mono">${dirTxt}${threshold}%</span>`);
    await loadAlerts();
  }catch(err){
    _toast('Network error — please try again');
  }finally{
    _setAddBtnState();
  }
}
async function toggleAlert(id){
  try{
    const res=await Auth.apiFetch(`/alerts/${id}/toggle`,{method:'PATCH'});
    if(!res.ok) throw new Error();
    await loadAlerts();
  }catch{_toast('Failed to toggle alert');}
}
async function deleteAlert(id){
  try{
    const res=await Auth.apiFetch(`/alerts/${id}`,{method:'DELETE'});
    if(!res.ok) throw new Error();
    await loadAlerts();
  }catch{_toast('Failed to delete alert');}
}

// ── Init ──────────────────────────────────────────────────────────────────────
const EX_LABEL_MAP = {binance:'Binance',bybit:'Bybit',okx:'OKX',gate:'Gate.io',kucoin:'KuCoin',mexc:'MEXC',bitget:'Bitget',hyperliquid:'Hyperliquid',aster:'Aster',ethereal:'Ethereal',whitebit:'WhiteBIT',bingx:'BingX'};

async function _assertExchangesEnabled(){
  try {
    // Lightweight availability endpoint (~5KB) — replaces the 800KB funding
    // blob we used to await here, which added ~4s to every /arb cold load.
    const r = await fetch('/api/screener/availability');
    if (!r.ok) return true;
    const d = await r.json();
    const enabled = new Set(d.exchanges || []);
    const missing = [LONG, SHORT].filter(ex => !enabled.has(ex));
    if (missing.length) {
      _showExchangeDisabledModal(missing);
      return false;
    }
    const symbols = new Set(d.symbols || []);
    if (!symbols.has(SYM)) {
      _showSymbolHiddenModal();
      return false;
    }
  } catch (_) { /* don't block on error */ }
  return true;
}

function _showSymbolHiddenModal(){
  const html = `
    <div id="ex-disabled-mask" style="position:fixed;inset:0;background:rgba(14,14,17,0.82);backdrop-filter:blur(6px);z-index:9000;display:flex;align-items:center;justify-content:center;padding:24px">
      <div style="max-width:440px;width:100%;background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:28px 24px;text-align:center;box-shadow:0 24px 60px rgba(0,0,0,0.5)">
        <div style="width:48px;height:48px;margin:0 auto 16px;border-radius:12px;background:rgba(229,192,123,0.12);border:1px solid rgba(229,192,123,0.4);display:flex;align-items:center;justify-content:center;color:var(--yellow)">
          <svg width="24" height="24" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M1.5 8s2.5-5 6.5-5 6.5 5 6.5 5-2.5 5-6.5 5-6.5-5-6.5-5z"/><path d="M2 2l12 12"/></svg>
        </div>
        <h2 style="margin:0 0 8px;font-size:19px;letter-spacing:-0.01em">Token not available</h2>
        <p style="margin:0 0 20px;color:var(--text3);font-size:13px;line-height:1.55">
          <span style="font-family:var(--mono);color:var(--text)">${SYM}</span> is currently hidden by the administrator. It will reappear on the screener once it's unblocked.
        </p>
        <a href="/screener" style="display:inline-block;padding:9px 22px;border-radius:9px;background:var(--green);color:#0a0a0f;font-weight:700;font-size:13px;text-decoration:none;letter-spacing:-0.005em">Back to screener</a>
      </div>
    </div>`;
  document.body.insertAdjacentHTML('beforeend', html);
}

function _showExchangeDisabledModal(missing){
  const names = missing.map(ex => EX_LABEL_MAP[ex] || ex).join(' & ');
  const html = `
    <div id="ex-disabled-mask" style="position:fixed;inset:0;background:rgba(14,14,17,0.82);backdrop-filter:blur(6px);z-index:9000;display:flex;align-items:center;justify-content:center;padding:24px">
      <div style="max-width:440px;width:100%;background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:28px 24px;text-align:center;box-shadow:0 24px 60px rgba(0,0,0,0.5)">
        <div style="width:48px;height:48px;margin:0 auto 16px;border-radius:12px;background:rgba(229,192,123,0.12);border:1px solid rgba(229,192,123,0.4);display:flex;align-items:center;justify-content:center;color:var(--yellow)">
          <svg width="24" height="24" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.5L1.5 13.5h13L8 1.5z"/><path d="M8 6v3.5"/><circle cx="8" cy="11.7" r="0.55" fill="currentColor"/></svg>
        </div>
        <h2 style="margin:0 0 8px;font-size:19px;letter-spacing:-0.01em">Exchange temporarily unavailable</h2>
        <p style="margin:0 0 20px;color:var(--text3);font-size:13px;line-height:1.55">
          ${names} ${missing.length > 1 ? 'are' : 'is'} currently disabled by the administrator.
          This pair cannot be viewed right now — try again later or pick another exchange.
        </p>
        <a href="/screener" style="display:inline-block;padding:9px 22px;border-radius:9px;background:var(--green);color:#0a0a0f;font-weight:700;font-size:13px;text-decoration:none;letter-spacing:-0.005em">Back to screener</a>
      </div>
    </div>`;
  document.body.insertAdjacentHTML('beforeend', html);
}

async function init(){
  if(!SYM||!LONG||!SHORT)return;
  // Before wiring anything up, confirm both legs are currently enabled by admin.
  if (!(await _assertExchangesEnabled())) return;

  // Instant paint from sessionStorage snapshot if one is still fresh —
  // swap-direction / page-return lands on previous state in <50ms.
  const cached = typeof _ssLoad === 'function' ? _ssLoad() : null;
  if (cached) { _opp = cached; renderTopBar(); }

  renderTopBar();
  // Critical path: WS + books start immediately (non-blocking);
  // lightweight /pair endpoint fetches only 2 exchanges (not all 12).
  connectWs();
  startBooks();

  // Fire /pair AND /all-exchanges-funding concurrently. /pair gives a full
  // synthesized opp (preferred). /all-exchanges-funding covers the case
  // where the pair has no arb (e.g. rates too close, or filtered by the
  // zero-rate guard) — we still want to show each leg's funding/ivl/vol
  // independently instead of dashes.
  (async () => {
    try {
      const r = await Auth.apiFetch(`/screener/pair?symbol=${SYM}&long_ex=${LONG}&short_ex=${SHORT}`);
      if (!r.ok) return;
      const d = await r.json();
      if (d.opp) {
        _opp = d.opp;
        if (typeof _ssSave === 'function') _ssSave(d.opp);
        renderTopBar();
      }
    } catch {}
  })();
  (async () => {
    // Populate per-leg infobar fields even when /pair has nothing. Don't
    // touch _opp so that if /pair wins the race, its synthesized spread /
    // net metrics take precedence.
    try {
      const r = await Auth.apiFetch(`/screener/all-exchanges-funding?symbol=${SYM}`);
      if (!r.ok) return;
      const d = await r.json();
      const byEx = {};
      for (const row of (d.rates || [])) byEx[row.exchange] = row;
      _applyLegFallback('long',  byEx[LONG]);
      _applyLegFallback('short', byEx[SHORT]);
    } catch {}
  })();
  // Alerts count in topbar — lightweight, non-critical
  Auth.apiFetch('/alerts').then(r=>r.ok?r.json():[]).then(a=>{_alerts=a||[];_refreshAlertCount();}).catch(()=>{});
}

// ── Lazy loaders for the chart-tab data ──
let _priceHistLoading = false, _priceHistLoaded = false;
let _fundHistLoading  = false, _fundHistLoaded  = false;

async function _ensurePriceHistory(){
  if (_priceHistLoaded || _priceHistLoading) return;
  _priceHistLoading = true;
  try {
    const r = await Auth.apiFetch(`/screener/arb-price-history?symbol=${SYM}&long_ex=${LONG}&short_ex=${SHORT}`);
    if (!r.ok) return;
    const d = await r.json();
    _longPrices = d.long_prices||[]; _shortPrices = d.short_prices||[];
    _priceHistLoaded = true;
    renderSpreadChart();
  } finally { _priceHistLoading = false; }
}

async function _ensureFundHistory(){
  if (_fundHistLoaded || _fundHistLoading) return;
  _fundHistLoading = true;
  try {
    const r = await Auth.apiFetch(`/screener/arb-history?symbol=${SYM}&long_ex=${LONG}&short_ex=${SHORT}`);
    if (!r.ok) return;
    const d = await r.json();
    _longHist = d.long_history||[]; _shortHist = d.short_history||[];
    _fundHistLoaded = true;
    renderFundChart(); buildHistTable(); renderOverviewTables();
  } finally { _fundHistLoading = false; }
}
window.addEventListener('resize',()=>{if(_spreadBuilt)renderSpreadChart();if(_fundBuilt)renderFundChart();if(_eeHist.length>1)renderEntryExitChart();});
// Wake-up refresh: when the tab returns to foreground, force one immediate
// pair refresh + book fetch so the user doesn't see stale data until the
// next 5s/2s interval tick.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  try { _refreshPair && _refreshPair(); } catch (_) {}
  try { fetchBook(LONG,'long'); fetchBook(SHORT,'short'); } catch (_) {}
});
init();

// ═══════════════════════════════════════════════════════════════════════
//  ALPHA FEATURES · slippage calc · paper trading · backtest · alpha badge
// ═══════════════════════════════════════════════════════════════════════
let _xsTimer=null;
function xsDebounce(){clearTimeout(_xsTimer);_xsTimer=setTimeout(xsUpdate,350);}

async function xsUpdate(){
  const size=parseFloat(document.getElementById('xs-size').value)||0;
  if(size<=10) return;
  try{
    const r=await Auth.apiFetch(`/screener/executable-spread?symbol=${SYMBOL}&long_ex=${LONG}&short_ex=${SHORT}&size_usd=${size}`);
    if(!r.ok) return;
    const d=await r.json();
    const f=(v,dp=3)=>v==null?'—':Number(v).toFixed(dp)+'%';
    document.getElementById('xs-quoted').textContent=f(d.quoted_spread_pct);
    document.getElementById('xs-exec').textContent=f(d.executable_spread_pct);
    document.getElementById('xs-slip').textContent=f(d.slippage_pct);
    const net=d.net_spread_pct;
    const netEl=document.getElementById('xs-net');
    netEl.textContent=f(net);
    netEl.className='calc-val '+(net>0?'pos':'neg');
    const mx=Math.max(Math.abs(d.quoted_spread_pct||0),Math.abs(d.executable_spread_pct||0),0.05);
    document.getElementById('xs-bar-q').style.width=Math.min(100,Math.abs(d.quoted_spread_pct||0)/mx*100)+'%';
    document.getElementById('xs-bar-e').style.width=Math.min(100,Math.abs(d.executable_spread_pct||0)/mx*100)+'%';
  }catch(_){}
}

// ── Paper Trading ──
let _ptTimer=null;
async function loadPaperStats(){
  try{
    const r=await Auth.apiFetch('/screener/paper/stats');
    if(!r.ok) return;
    const s=await r.json();
    const totalEl=document.getElementById('pt-total');
    totalEl.textContent='$'+(s.total_pnl_usd||0).toFixed(2);
    totalEl.style.color=(s.total_pnl_usd||0)>0?'var(--green)':(s.total_pnl_usd||0)<0?'var(--red)':'var(--text)';
    document.getElementById('pt-open').textContent=s.open_count||0;
    document.getElementById('pt-wr').textContent=(s.closed_count||0)>0?s.win_rate_pct.toFixed(0)+'%':'—';
  }catch(_){}
}
async function loadPaperList(){
  try{
    const r=await Auth.apiFetch('/screener/paper/positions?status=open');
    if(!r.ok) return;
    const rows=await r.json();
    // Filter to current pair only
    const mine=rows.filter(p=>p.symbol===SYMBOL&&p.long_exchange===LONG&&p.short_exchange===SHORT);
    const el=document.getElementById('pt-list');
    if(!mine.length){el.innerHTML='<div style="color:var(--text3);font-size:11px;padding:6px 2px">No open positions for this pair</div>';return;}
    el.innerHTML=mine.map(p=>{
      const netCls=p.net_pnl_usd>0?'pos':'neg';
      return `
      <div style="border:1px solid var(--border);border-radius:7px;padding:8px;display:flex;flex-direction:column;gap:4px;background:var(--surface2)">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span class="mono" style="font-size:11px;color:var(--text2)">$${p.size_usd.toFixed(0)} · entry ${p.entry_spread_pct.toFixed(3)}%</span>
          <span class="mono ${netCls}" style="font-weight:700;font-size:13px">${p.net_pnl_usd>0?'+':''}$${p.net_pnl_usd.toFixed(2)}</span>
        </div>
        <div style="display:flex;gap:10px;font-size:10px;color:var(--text3)">
          <span>Price P&L <span class="mono" style="color:var(--text2)">${p.price_pnl_usd>0?'+':''}$${p.price_pnl_usd.toFixed(2)}</span></span>
          <span>Funding <span class="mono" style="color:var(--text2)">${p.funding_pnl_usd>0?'+':''}$${p.funding_pnl_usd.toFixed(2)}</span></span>
          <span style="margin-left:auto">spread ${p.current_spread_pct.toFixed(3)}%</span>
        </div>
        <div style="display:flex;gap:6px;margin-top:2px">
          <button onclick="paperClose(${p.id})" style="flex:1;height:22px;padding:0 8px;background:var(--surface3);border:1px solid var(--border);color:var(--text);border-radius:5px;cursor:pointer;font-family:inherit;font-size:11px">Close</button>
          <button onclick="paperDelete(${p.id})" style="height:22px;padding:0 8px;background:transparent;border:1px solid var(--border);color:var(--red);border-radius:5px;cursor:pointer;font-family:inherit;font-size:11px">×</button>
        </div>
      </div>`;
    }).join('');
  }catch(_){}
}
async function paperOpen(){
  const size=parseFloat(document.getElementById('pt-size').value)||0;
  if(size<10){_toast('Size must be ≥ $10');return;}
  try{
    const r=await Auth.apiFetch('/screener/paper/positions',{method:'POST',body:JSON.stringify({symbol:SYMBOL,long_exchange:LONG,short_exchange:SHORT,size_usd:size})});
    if(!r.ok){const e=await r.json().catch(()=>({}));_toast(e.detail||'Failed to open');return;}
    _toast('Position opened');
    loadPaperStats();loadPaperList();
  }catch(_){_toast('Failed to open');}
}
async function paperClose(id){
  try{
    const r=await Auth.apiFetch(`/screener/paper/positions/${id}/close`,{method:'POST'});
    if(!r.ok) return;
    const j=await r.json();
    _toast(`Closed · realized ${j.realized_pnl_usd>0?'+':''}$${j.realized_pnl_usd.toFixed(2)}`);
    loadPaperStats();loadPaperList();
  }catch(_){}
}
async function paperDelete(id){
  try{
    const r=await Auth.apiFetch(`/screener/paper/positions/${id}`,{method:'DELETE'});
    if(r.ok){loadPaperStats();loadPaperList();}
  }catch(_){}
}

// ── Backtest ──
async function runBacktest(){
  const days=parseInt(document.getElementById('bt-days').value)||7;
  const size=parseFloat(document.getElementById('bt-size').value)||1000;
  try{
    const r=await Auth.apiFetch(`/screener/backtest?symbol=${SYMBOL}&long_ex=${LONG}&short_ex=${SHORT}&days=${days}&size_usd=${size}`);
    if(!r.ok) return;
    const d=await r.json();
    if(d.error){_toast(d.error);return;}
    const netEl=document.getElementById('bt-net');
    netEl.textContent='$'+d.net_pnl_usd.toFixed(2);
    netEl.className='calc-val '+(d.net_pnl_usd>0?'pos':'neg');
    document.getElementById('bt-pct').textContent=d.net_pnl_pct.toFixed(3)+'%';
    document.getElementById('bt-pct').className='calc-val '+(d.net_pnl_pct>0?'pos':'neg');
    document.getElementById('bt-apr').textContent=d.annualized_apr_pct.toFixed(1)+'%';
    document.getElementById('bt-apr').className='calc-val '+(d.annualized_apr_pct>0?'pos':'neg');
    document.getElementById('bt-fees').textContent='$'+d.round_trip_fees_usd.toFixed(2);
  }catch(_){}
}

// ── Alpha score badge + anomaly flag ──
async function loadAlphaBadge(){
  try{
    const r=await Auth.apiFetch('/screener/alpha');
    if(!r.ok) return;
    const d=await r.json();
    const mine=(d.opportunities||[]).find(o=>o.symbol===SYMBOL&&o.long_exchange===LONG&&o.short_exchange===SHORT);
    if(!mine) return;
    const score=mine.alpha_score||0;
    const rank=mine.alpha_rank||'—';
    const color=score<40?'var(--red)':score<70?'var(--yellow)':'var(--green)';
    const hero=document.querySelector('.hero-block');
    if(hero&&!document.getElementById('alpha-pill')){
      const pill=document.createElement('div');
      pill.id='alpha-pill';
      pill.title=`Alpha rank #${rank} of ${d.opportunities.length}`;
      pill.style.cssText=`display:inline-flex;align-items:center;gap:4px;padding:3px 8px;margin-left:8px;border-radius:999px;background:${color}22;border:1px solid ${color};color:${color};font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:700;letter-spacing:.02em`;
      pill.innerHTML=`<span style="opacity:.7">α</span>${score.toFixed(0)}`;
      hero.appendChild(pill);
    }
  }catch(_){}
}
async function loadAnomalyFlag(){
  try{
    const r=await Auth.apiFetch('/screener/anomalies?hours=1&limit=50');
    if(!r.ok) return;
    const rows=await r.json();
    const hit=rows.find(a=>a.symbol===SYMBOL&&a.long_exchange===LONG&&a.short_exchange===SHORT);
    if(!hit) return;
    const hero=document.querySelector('.hero-block');
    if(hero&&!document.getElementById('anom-flag')){
      const flag=document.createElement('div');
      flag.id='anom-flag';
      flag.title=`z-score ${hit.z_score.toFixed(1)} at ${hit.created_at}`;
      flag.style.cssText=`display:inline-flex;align-items:center;gap:4px;padding:3px 8px;margin-left:6px;border-radius:999px;background:rgba(229,192,123,.12);border:1px solid var(--yellow);color:var(--yellow);font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:700;animation:ws-pulse 1.6s ease-in-out infinite`;
      flag.innerHTML='⚡ ANOMALY';
      hero.appendChild(flag);
    }
  }catch(_){}
}

// Kick off alpha features after init
// Secondary loads — fire after the critical path is painted but not at 1200ms (too late)
setTimeout(()=>{loadAlphaBadge();loadAnomalyFlag();initArbWatch();initTradePanel();initAccBlock();},250);

// ═══════════════════════════════════════════════════════════════════════
//  ACCOUNT BLOCK · Positions / Orders / P&L / Balances (UI only for now)
// ═══════════════════════════════════════════════════════════════════════
function accSwitch(el){
  const pane = el.dataset.pane;
  document.querySelectorAll('.acc-tab').forEach(t => t.classList.toggle('is-active', t === el));
  document.querySelectorAll('.acc-pane').forEach(p => p.classList.toggle('is-active', p.id === 'acc-pane-'+pane));
}

async function initAccBlock(){
  // One-time migration of legacy localStorage Sync entries → backend.
  // Idempotent on the server side (UNIQUE on user_id+leg_a_key+leg_b_key).
  _migrateLegacyManualPairs().catch(() => {});
  await Promise.all([_refreshPairDecisions(), accLoadKeyCounts(), accLoadPositions(), accLoadBalances(), accLoadOrders(), accLoadPnl(), accLoadTriggers()]);
  // Backend is now WS-fed: WS user-streams (11 venues) push position/
  // balance changes the moment they happen on the exchange, the
  // snapshot store is always fresh, and the reconcile worker (60s)
  // catches anything WS missed. Polling every 10s was needed when REST
  // was the source of truth — now it just adds load. 30s gives near-
  // real-time UX (events are usually visible within 1s of an exchange
  // push because /ws/long-short funding ticks push price changes that
  // trigger a re-render with current snapshot data).
  // 30s → 10s: pair decisions + positions + balances + orders batch.
  // /arb is the trader's primary screen; 30s ощутимо медленно после
  // close/open. Each underlying endpoint has server-side cache so
  // the venue API load is bounded.
  setInterval(() => { if (document.hidden) return; _refreshPairDecisions(); accLoadPositions(); accLoadBalances(); accLoadOrders(); }, 10000);
  // 60s → 15s: PnL refresh. Includes cumulative funding paid which only
  // changes per funding interval (1-8h) but unrealized PnL moves with
  // mark price every second.
  setInterval(() => { if (document.hidden) return; accLoadPnl(); }, 15000);
  // 5s baseline poll for triggers — kept as a safety net even when the WS
  // is connected, in case a push is dropped (Mobile Safari background tabs
  // throttle WS connections to a degree that we've seen kills events).
  setInterval(() => { if (document.hidden) return; accLoadTriggers(); }, 5000);

  // Per-user WS for instant position/trigger refresh on mutation.
  // Falls back gracefully to the 5s poll if the WS can't open.
  _connectPositionsWS();
}

async function accLoadOrders(){
  try {
    const r = await Auth.apiFetch('/trade/orders?limit=50');
    if (!r.ok) throw new Error();
    const rows = await r.json();
    _renderOrderHistory(rows);
  } catch {
    _renderOrderHistory([]);
  }
}

// ── P&L tab ───────────────────────────────────────────────────────────
const _pnlExpanded = new Set();
function _pnlToggle(id){
  if (_pnlExpanded.has(id)) _pnlExpanded.delete(id);
  else _pnlExpanded.add(id);
  _renderPnl(window._pnlLastRows || []);
}
function _pnlFmtUsd(v){
  if (v == null || Number.isNaN(Number(v))) return '—';
  const n = Number(v);
  return (n >= 0 ? '+$' : '−$') + Math.abs(n).toFixed(2);
}
function _pnlFmtRoi(total, notional){
  if (!notional || Number.isNaN(Number(total))) return '—';
  const pct = (Number(total) / Number(notional)) * 100;
  return (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
}
async function accLoadPnl(){
  const tbody = document.getElementById('acc-pnl-body');
  const empty = document.getElementById('acc-pnl-empty');
  try {
    const r = await Auth.apiFetch('/trade/pnl?days=30');
    if (!r.ok) throw new Error();
    const rows = await r.json();
    _renderPnl(rows);
  } catch {
    _renderPnl([]);
  }
  _updatePnlSyncStatus();
  // First open: kick off a sync if it's been > 30 min since the last
  // one, OR if no sync has ever happened. Quiet — happens in the
  // background while the table renders.
  if (!window._pnlAutoSynced){
    window._pnlAutoSynced = true;
    try {
      const sr = await Auth.apiFetch('/trade/pnl/sync');
      if (sr.ok){
        const j = await sr.json();
        const last = j.last_synced_at ? new Date(j.last_synced_at).getTime() : 0;
        const stale = !last || (Date.now() - last > 30 * 60 * 1000);
        if (stale && !j.in_progress){
          accSyncPnl(null, /*quiet*/true);
        }
      }
    } catch {}
  }
}

async function accSyncPnl(btn, quiet){
  const button = btn || document.getElementById('acc-pnl-sync-btn');
  const flag = document.getElementById('acc-pnl-syncing');
  if (button) button.disabled = true;
  if (flag) flag.style.display = 'inline-flex';
  try {
    const r = await Auth.apiFetch('/trade/pnl/sync', { method: 'POST' });
    if (!r.ok) throw new Error('sync failed');
  } catch (e) {
    if (!quiet && typeof toast === 'function') toast('Sync failed', 'error');
    if (button) button.disabled = false;
    if (flag) flag.style.display = 'none';
    return;
  }
  // Poll status every 3s, up to 60s.
  const t0 = Date.now();
  while (Date.now() - t0 < 60_000){
    await new Promise(r => setTimeout(r, 3000));
    try {
      const r = await Auth.apiFetch('/trade/pnl/sync');
      if (!r.ok) break;
      const j = await r.json();
      if (!j.in_progress) break;
    } catch { break; }
  }
  // Refetch PnL list — backfill may have created new rows.
  try {
    const r = await Auth.apiFetch('/trade/pnl?days=30');
    if (r.ok) _renderPnl(await r.json());
  } catch {}
  if (button) button.disabled = false;
  if (flag) flag.style.display = 'none';
  _updatePnlSyncStatus();
  if (!quiet && typeof toast === 'function') toast('PnL synced', 'success');
}

async function _updatePnlSyncStatus(){
  const el = document.getElementById('acc-pnl-synced');
  if (!el) return;
  try {
    const r = await Auth.apiFetch('/trade/pnl/sync');
    if (!r.ok) return;
    const j = await r.json();
    if (!j.last_synced_at) {
      el.textContent = 'Sync to pull recent fills from your venues';
      return;
    }
    const ts = new Date(j.last_synced_at).getTime();
    const sec = Math.max(0, Math.floor((Date.now() - ts) / 1000));
    const human = sec < 60 ? 'just now'
                : sec < 3600 ? `${Math.floor(sec/60)} min ago`
                : sec < 86400 ? `${Math.floor(sec/3600)} h ago`
                : `${Math.floor(sec/86400)} d ago`;
    el.textContent = `Last synced ${human}`;
  } catch {}
}
function _renderPnl(rows){
  window._pnlLastRows = rows || [];
  const tbody = document.getElementById('acc-pnl-body');
  const empty = document.getElementById('acc-pnl-empty');
  if (!rows.length){
    if (tbody) tbody.innerHTML = '';
    if (empty) empty.style.display = '';
    document.getElementById('acc-pnl-total').textContent = '$0.00';
    document.getElementById('acc-pnl-count').textContent = '0';
    document.getElementById('acc-pnl-winrate').textContent = '—';
    document.getElementById('acc-pnl-funding').textContent = '$0.00';
    return;
  }
  if (empty) empty.style.display = 'none';

  // Summary cards
  let totalPnl = 0, totalFunding = 0, wins = 0;
  for (const r of rows){
    totalPnl += Number(r.total_pnl_usd || 0);
    totalFunding += Number(r.total_funding_pnl_usd != null ? r.total_funding_pnl_usd : (r.funding_pnl_usd || 0));
    if (Number(r.total_pnl_usd || 0) > 0) wins++;
  }
  const totalEl = document.getElementById('acc-pnl-total');
  totalEl.textContent = _pnlFmtUsd(totalPnl);
  totalEl.className = 'acc-sum-val ' + (totalPnl >= 0 ? 'pos' : 'neg');
  document.getElementById('acc-pnl-count').textContent = rows.length;
  document.getElementById('acc-pnl-winrate').textContent = ((wins / rows.length) * 100).toFixed(0) + '%';
  document.getElementById('acc-pnl-funding').textContent = _pnlFmtUsd(totalFunding);

  if (!tbody) return;
  const exLbl = (e) => (window.EX_LABEL && EX_LABEL[e]) || (e || '').toUpperCase();
  tbody.innerHTML = rows.map(r => {
    const isOpen = _pnlExpanded.has(r.id);
    const total = Number(r.total_pnl_usd || 0);
    const totalCls = total >= 0 ? 'pnl-pl-pos' : 'pnl-pl-neg';
    const chev = `<svg class="oh-chev ${isOpen?'open':''}" viewBox="0 0 16 16" onclick="event.stopPropagation();_pnlToggle('${r.id}')"><path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
    let venuesCell = '';
    let typeCell = '';
    let roiCell = '—';
    let notional = 0;
    if (r.kind === 'pair'){
      // Same mode rule as the live pair-card header — show Futures/Spot/DEX
      // instead of the old generic "PAIR" pill.
      typeCell = `<span class="pnl-type pair">${_pairModeLabel(r)}</span>`;
      venuesCell = `<span class="pnl-venue-row"><span class="ex-pill">${_esc(exLbl(r.long.exchange))}</span><span style="color:var(--green)">⇄</span><span class="ex-pill">${_esc(exLbl(r.short.exchange))}</span></span>`;
      notional = (Number(r.long.qty || 0) * Number(r.long.entry_price || 0)) || 0;
    } else {
      typeCell = `<span class="pnl-type single">${(r.side || '').toUpperCase()}</span>`;
      venuesCell = `<span class="pnl-venue-row"><span class="ex-pill">${_esc(exLbl(r.exchange))}</span></span>`;
      notional = Number(r.qty || 0) * Number(r.entry_price || 0);
    }
    if (notional > 0) roiCell = _pnlFmtRoi(total, notional);
    const shareData = _htmlEsc(JSON.stringify(r));
    const shareBtn = `<button class="pnl-share" title="Share P&amp;L card"
                     data-share-pnl='${shareData}'
                     onclick='event.stopPropagation();_openShareFromPnl(this)'
                     style="background:transparent;border:1px solid var(--border);color:var(--green);padding:3px 7px;border-radius:5px;cursor:pointer;font-size:11px;font-family:inherit;margin-left:6px">↗</button>`;
    const main = `
      <tr onclick="_pnlToggle('${r.id}')" style="cursor:pointer">
        <td>${chev}</td>
        <td style="color:var(--text2);font-size:11px">${_ohFmtTime(r.closed_at)}</td>
        <td>${typeCell}</td>
        <td><span class="sym">${_esc(r.symbol||'')}</span></td>
        <td>${venuesCell}</td>
        <td class="num"><span class="${totalCls}">${_pnlFmtUsd(total)}</span></td>
        <td class="num"><span class="${totalCls}">${roiCell}</span>${shareBtn}</td>
      </tr>`;
    if (!isOpen) return main;

    const detail = (r.kind === 'pair')
      ? _renderPnlPairDetail(r)
      : _renderPnlSingleDetail(r);
    return main + `<tr class="pnl-detail-row"><td colspan="7">${detail}</td></tr>`;
  }).join('');
}
function _renderPnlPairDetail(r){
  const exLbl = (e) => (window.EX_LABEL && EX_LABEL[e]) || (e || '').toUpperCase();
  const legCard = (label, leg, cls) => `
    <div class="pnl-leg-card">
      <span class="leg-label ${cls}">${label}</span>
      <div><div class="k">Venue</div><div>${_esc(exLbl(leg.exchange))}</div></div>
      <div><div class="k">Qty</div><div>${leg.qty != null ? Number(leg.qty).toLocaleString(undefined,{maximumFractionDigits:6}) : '—'}</div></div>
      <div><div class="k">Entry</div><div>${leg.entry_price != null ? Number(leg.entry_price).toLocaleString(undefined,{maximumFractionDigits:8}) : '—'}</div></div>
      <div><div class="k">Exit</div><div>${leg.exit_price != null ? Number(leg.exit_price).toLocaleString(undefined,{maximumFractionDigits:8}) : '—'}</div></div>
      <div><div class="k">P&amp;L</div><div class="${(leg.realized_pnl_usd||0) >= 0 ? 'pnl-pl-pos':'pnl-pl-neg'}">${_pnlFmtUsd(leg.realized_pnl_usd)}</div></div>
    </div>`;
  return `
    ${legCard('LONG', r.long, 'long')}
    ${legCard('SHORT', r.short, 'short')}
    <div class="pnl-summary">
      <div><div class="lbl">Realized</div><div class="val ${(r.total_realized_pnl_usd||0)>=0?'pnl-pl-pos':'pnl-pl-neg'}">${_pnlFmtUsd(r.total_realized_pnl_usd)}</div></div>
      <div><div class="lbl">Funding</div><div class="val">${_pnlFmtUsd(r.total_funding_pnl_usd)}</div></div>
      <div><div class="lbl">Fees</div><div class="val pnl-pl-neg">${r.total_fees_usd != null ? '−$'+Number(r.total_fees_usd).toFixed(2) : '—'}</div></div>
      <div><div class="lbl">Total</div><div class="val ${(r.total_pnl_usd||0)>=0?'pnl-pl-pos':'pnl-pl-neg'}">${_pnlFmtUsd(r.total_pnl_usd)}</div></div>
      <div><div class="lbl">Entry spread</div><div class="val">${r.entry_spread_pct != null ? Number(r.entry_spread_pct).toFixed(2)+'%' : '—'}</div></div>
      <div><div class="lbl">Opened</div><div class="val" style="font-size:11px">${_esc(_ohFmtTime(r.opened_at))}</div></div>
    </div>`;
}
function _renderPnlSingleDetail(r){
  const exLbl = (e) => (window.EX_LABEL && EX_LABEL[e]) || (e || '').toUpperCase();
  const sideCls = r.side === 'buy' ? 'long' : 'short';
  const sideTxt = r.side === 'buy' ? 'LONG' : 'SHORT';
  return `
    <div class="pnl-leg-card">
      <span class="leg-label ${sideCls}">${sideTxt}</span>
      <div><div class="k">Venue</div><div>${_esc(exLbl(r.exchange))}</div></div>
      <div><div class="k">Qty</div><div>${r.qty != null ? Number(r.qty).toLocaleString(undefined,{maximumFractionDigits:6}) : '—'}</div></div>
      <div><div class="k">Entry</div><div>${r.entry_price != null ? Number(r.entry_price).toLocaleString(undefined,{maximumFractionDigits:8}) : '—'}</div></div>
      <div><div class="k">Exit</div><div>${r.exit_price != null ? Number(r.exit_price).toLocaleString(undefined,{maximumFractionDigits:8}) : '—'}</div></div>
      <div><div class="k">P&amp;L</div><div class="${(r.realized_pnl_usd||0)>=0?'pnl-pl-pos':'pnl-pl-neg'}">${_pnlFmtUsd(r.realized_pnl_usd)}</div></div>
    </div>
    <div class="pnl-summary">
      <div><div class="lbl">Realized</div><div class="val ${(r.realized_pnl_usd||0)>=0?'pnl-pl-pos':'pnl-pl-neg'}">${_pnlFmtUsd(r.realized_pnl_usd)}</div></div>
      <div><div class="lbl">Funding</div><div class="val">${_pnlFmtUsd(r.funding_pnl_usd)}</div></div>
      <div><div class="lbl">Fees</div><div class="val pnl-pl-neg">${r.fees_usd != null ? '−$'+Number(r.fees_usd).toFixed(2) : '—'}</div></div>
      <div><div class="lbl">Total</div><div class="val ${(r.total_pnl_usd||0)>=0?'pnl-pl-pos':'pnl-pl-neg'}">${_pnlFmtUsd(r.total_pnl_usd)}</div></div>
      <div><div class="lbl">Opened</div><div class="val" style="font-size:11px">${_esc(_ohFmtTime(r.opened_at))}</div></div>
      ${r.opened_externally ? `<div><div class="lbl">Source</div><div class="val" style="font-size:11px;color:var(--text2)">Opened on exchange</div></div>` : ''}
    </div>`;
}

// Shared Order History renderer — used from both the futures pair page
// (acc-pane-orders) and the spot/dex page. Reads from /trade/orders which
// is sourced from our trade_orders table — only orders our service placed,
// not arbitrary fills the user did directly on the exchange.
const _ohExpanded = new Set();
function _ohToggle(id){
  if (_ohExpanded.has(id)) _ohExpanded.delete(id);
  else _ohExpanded.add(id);
  const r = window._ohLastRows || [];
  _renderOrderHistory(r);
}
function _esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function _ohFmtTime(iso){
  if (!iso) return '—';
  try { const d = new Date(iso); return d.toLocaleString('en-US',{month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}); }
  catch { return iso; }
}
function _renderOrderHistory(rows){
  window._ohLastRows = rows || [];
  const tbody = document.getElementById('acc-orders-body');
  const empty = document.getElementById('acc-orders-empty');
  const cnt = document.getElementById('acc-cnt-orders');
  if (cnt) cnt.textContent = rows.length;
  if (!rows.length){
    if (tbody) tbody.innerHTML = '';
    if (empty) empty.style.display = '';
    return;
  }
  if (empty) empty.style.display = 'none';
  if (!tbody) return;
  tbody.innerHTML = rows.map(o => {
    const isOpen = _ohExpanded.has(o.id);
    const exLabel = (window.EX_LABEL && EX_LABEL[o.exchange]) || (o.exchange||'').toUpperCase();
    const sideCls = (o.side === 'buy') ? 'oh-side-buy' : 'oh-side-sell';
    const sideTxt = (o.side === 'buy') ? 'BUY' : 'SELL';
    const intent = (o.intent || '').toUpperCase();
    const status = (o.status || 'pending').toLowerCase();
    const qty = (o.filled_qty != null ? o.filled_qty : o.requested_qty);
    const price = o.avg_fill_price;
    const chev = `<svg class="oh-chev ${isOpen?'open':''}" viewBox="0 0 16 16" onclick="event.stopPropagation();_ohToggle(${o.id})"><path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
    const main = `
      <tr onclick="_ohToggle(${o.id})" style="cursor:pointer">
        <td>${chev}</td>
        <td style="color:var(--text2);font-size:11px">${_ohFmtTime(o.created_at)}</td>
        <td><span class="oh-intent">${_esc(intent)}</span></td>
        <td><span class="sym">${_esc(o.symbol||'')}</span></td>
        <td><span class="ex-pill">${_esc(exLabel)}</span></td>
        <td><span class="${sideCls}">${sideTxt}</span></td>
        <td class="num">${qty != null ? Number(qty).toLocaleString(undefined,{maximumFractionDigits:6}) : '—'}</td>
        <td class="num">${price != null ? Number(price).toLocaleString(undefined,{maximumFractionDigits:8}) : '—'}</td>
        <td><span class="oh-status ${status}">${status}</span></td>
      </tr>`;
    if (!isOpen) return main;
    const errBlock = (o.error_message)
      ? `<div class="oh-detail-err"><div class="lbl">${o.error_kind === 'exchange' ? 'Exchange error' : 'Error'}${o.error_code ? ' · ' + _esc(o.error_code) : ''}</div>${_esc(o.error_message)}</div>`
      : '';
    const rawBlock = (o.raw_response)
      ? `<div class="oh-detail-raw">${_esc(JSON.stringify(o.raw_response, null, 2))}</div>`
      : '';
    const detail = `
      <tr class="oh-detail-row">
        <td colspan="9">
          <div class="oh-detail-grid">
            <div><div class="k">Order ID</div><div class="v">${_esc(o.exchange_order_id || '—')}</div></div>
            <div><div class="k">Order type</div><div class="v">${_esc((o.order_type||'market').toUpperCase())}</div></div>
            <div><div class="k">Leverage</div><div class="v">${o.leverage != null ? o.leverage + '×' : '—'}</div></div>
            <div><div class="k">Margin mode</div><div class="v">${_esc(o.margin_mode || '—')}</div></div>
            <div><div class="k">Requested qty</div><div class="v">${o.requested_qty != null ? Number(o.requested_qty).toLocaleString(undefined,{maximumFractionDigits:6}) : '—'}</div></div>
            <div><div class="k">Filled qty</div><div class="v">${o.filled_qty != null ? Number(o.filled_qty).toLocaleString(undefined,{maximumFractionDigits:6}) : '—'}</div></div>
            <div><div class="k">Fee (USD)</div><div class="v">${o.fee_usd != null ? '$'+Number(o.fee_usd).toFixed(4) : '—'}</div></div>
            <div><div class="k">Finalized</div><div class="v">${_esc(_ohFmtTime(o.finalized_at))}</div></div>
          </div>
          ${errBlock}
          ${rawBlock}
        </td>
      </tr>`;
    return main + detail;
  }).join('');
}

async function accLoadKeyCounts(){
  // Trading view counts only screener-purpose keys (purpose IN screener /
  // both). Portfolio-only wallets aren't usable for trading and shouldn't
  // pad the Read-only column.
  try {
    const r = await Auth.apiFetch('/wallets');
    if (!r.ok) return;
    const rows = await r.json();
    const ex = rows.filter(w => w.wallet_type === 'exchange' && !w.is_archived
                                  && (w.purpose === 'screener' || w.purpose === 'both'));
    const tr = ex.filter(w => w.can_trade).length;
    const ro = Math.max(0, ex.length - tr);
    const roEl = document.getElementById('acc-ro-count'); if (roEl) roEl.textContent = ro;
    const trEl = document.getElementById('acc-tr-count'); if (trEl) trEl.textContent = tr;
  } catch {}
}

// Which pairs are currently expanded (user clicked the header).
// Default: collapsed. Persists across the 10s refresh tick.
const _accPairOpen = new Set();
function _accPairToggle(key) {
  if (_accPairOpen.has(key)) _accPairOpen.delete(key);
  else                       _accPairOpen.add(key);
  // Re-render immediately without waiting for the next poll tick.
  if (typeof accLoadPositions === 'function') accLoadPositions();
}

// Detect arb pairs: two positions on the same symbol, opposite sides,
// with matching USD notional (qty × mark). Match is on USD because
// different exchanges return qty in different units — KuCoin reports
// contracts (lots), Binance returns base-asset qty — so a raw-quantity
// match is unreliable. 10% tolerance handles slight price differences
// between the legs at snapshot time. Matched pair renders as a single
// "⇆ PAIR" block with summed metrics.
// Manual pairings — DB-backed via /api/trade/pair/* so they survive page
// refresh AND device switch. Cached in window._pairDecisions; refreshed on
// every accLoadPositions tick. Legacy localStorage entries get migrated
// to the API on first load.
//
// Schema: [{symbol, long_exchange, short_exchange}]. The user marks pairs
// via the "Sync ⇆" button when auto-detect can't (size mismatch, etc.).
const _MANUAL_PAIRS_KEY = 'avalant_manual_pairs_v1';
window._pairDecisions = window._pairDecisions || [];

async function _refreshPairDecisions(){
  try {
    const r = await Auth.apiFetch('/trade/pair/decisions');
    if (!r.ok) return;
    window._pairDecisions = await r.json() || [];
  } catch {}
}

function _loadManualPairs(){
  // Fast synchronous getter for renderers — relies on the cache being
  // refreshed by accLoadPositions. First call returns [] until the first
  // refresh completes.
  return window._pairDecisions || [];
}

async function _addManualPair(sym, longEx, shortEx){
  try {
    const r = await Auth.apiFetch('/trade/pair/sync', {
      method: 'POST',
      body: JSON.stringify({ symbol: sym, long_exchange: longEx, short_exchange: shortEx }),
    });
    if (!r.ok) return;
    await _refreshPairDecisions();
  } catch {}
}

async function _removeManualPair(sym, longEx, shortEx){
  try {
    const r = await Auth.apiFetch('/trade/pair/unsync', {
      method: 'POST',
      body: JSON.stringify({ symbol: sym, long_exchange: longEx, short_exchange: shortEx }),
    });
    if (!r.ok) return;
    await _refreshPairDecisions();
  } catch {}
}

// One-shot migration of any legacy localStorage entries → backend, then
// clear the local cache so we don't keep re-uploading on every load.
async function _migrateLegacyManualPairs(){
  let legacy = [];
  try { legacy = JSON.parse(localStorage.getItem(_MANUAL_PAIRS_KEY) || '[]') || []; }
  catch { legacy = []; }
  if (!Array.isArray(legacy) || !legacy.length) return;
  for (const p of legacy){
    if (!p || !p.symbol || !p.long_exchange || !p.short_exchange) continue;
    try {
      await Auth.apiFetch('/trade/pair/sync', {
        method: 'POST',
        body: JSON.stringify({ symbol: p.symbol, long_exchange: p.long_exchange, short_exchange: p.short_exchange }),
      });
    } catch {}
  }
  try { localStorage.removeItem(_MANUAL_PAIRS_KEY); } catch {}
}

function _acc_pair_positions(rows){
  const tagged = rows.map((p, i) => ({
    p,
    key: p.position_id || `${p.exchange}:${p.symbol}:${i}`,
    notional: Math.abs(Number(p.quantity || 0) * Number(p.mark_price || 0)),
  }));
  const bySym = {};
  for (const t of tagged) (bySym[t.p.symbol] = bySym[t.p.symbol] || []).push(t);
  const pairs = [];
  const used = new Set();

  // 1. MANUAL pairs first — user explicitly marked these as arb pairs.
  //    Match by symbol + exchanges + sides. Bypasses any size threshold.
  const manual = _loadManualPairs();
  for (const mp of manual) {
    const group = bySym[mp.symbol];
    if (!group) continue;
    const l = group.find(t => t.p.exchange === mp.long_exchange  && t.p.side === 'buy'  && !used.has(t.key));
    const s = group.find(t => t.p.exchange === mp.short_exchange && t.p.side === 'sell' && !used.has(t.key));
    if (!l || !s) continue;
    used.add(l.key); used.add(s.key);
    pairs.push({symbol: mp.symbol, long: l.p, short: s.p, _manual: true});
  }

  // 2. AUTO-DETECT for the rest. Rule: notional diff% must be within
  //    spread_pct ± TOLERANCE_PCT.
  //
  //    Tightened to 12% (was 5%) after a real-world miss: LABUSDT had
  //    diff=7.8% and spread=1.2%, delta=6.6% — clearly the same arb
  //    pair the user opened, but the 5% rule rejected it. 12% still
  //    keeps stray opposite-side positions on unrelated tokens from
  //    being conflated, and the user can always Sync ⇆ to override.
  const TOLERANCE_PCT = 12;
  for (const [sym, group] of Object.entries(bySym)) {
    const longs  = group.filter(t => t.p.side === 'buy'  && !used.has(t.key));
    const shorts = group.filter(t => t.p.side === 'sell' && !used.has(t.key));
    const candidates = [];
    for (const l of longs) for (const s of shorts) {
      const maxN = Math.max(l.notional, s.notional);
      if (maxN <= 0) continue;
      const lEntry = Number(l.p.entry_price || 0);
      const sEntry = Number(s.p.entry_price || 0);
      // Spread between the legs' entry prices, in %.
      const spreadPct = (lEntry > 0 && sEntry > 0)
        ? Math.abs((sEntry - lEntry) / lEntry) * 100
        : 0;
      const diffPct = (Math.abs(l.notional - s.notional) / maxN) * 100;
      if (Math.abs(diffPct - spreadPct) > TOLERANCE_PCT) continue;
      candidates.push({l, s, diffPct, spreadPct});
    }
    // Best candidate first = smallest deviation from the expected spread.
    candidates.sort((a, b) => Math.abs(a.diffPct - a.spreadPct) - Math.abs(b.diffPct - b.spreadPct));
    for (const c of candidates) {
      if (used.has(c.l.key) || used.has(c.s.key)) continue;
      used.add(c.l.key); used.add(c.s.key);
      pairs.push({symbol: sym, long: c.l.p, short: c.s.p});
    }
  }
  const singles = tagged.filter(t => !used.has(t.key)).map(t => t.p);
  return {pairs, singles};
}

// Sticky positions cache — keeps the last successful payload so brief
// network blips, 5xx hiccups, or transient empty responses don't blank
// the table while a position is actually open. Only clear after N
// consecutive empty responses (genuine "all closed" state).
let _accLastPositions = null;
let _accEmptyStreak = 0;
const _EMPTY_STREAK_LIMIT = 3;

// Diff-aware render: store the last-written HTML so we don't trash the DOM
// (and trigger a visible flicker) when the data hasn't changed across the
// 8-10s refresh tick. Backed by a key per target element.
// Stored on window so the spot/dex code path (which throws past this line
// for futures-mode setup) can still access it from earlier in the script.
// Pair-card mode label. The accordion header used to read "⇆ PAIR" for
// every pair regardless of whether it was a futures-vs-futures arb or a
// spot-vs-perp basis trade. The user wants the pair's MODE up front:
//
//   futures + futures  → "Futures"
//   spot    + perp     → "Spot"
//   DEX     + perp     → "DEX"
//
// The function inspects the page-level TYPE first (set from the URL),
// then falls back to inferring from the legs (e.g. when accLoadPositions
// runs on /app where TYPE isn't a meaningful page constant).
function _pairModeLabel(pair) {
  // Prefer per-pair flags over the page-wide TYPE — a /arb?type=futures
  // page can still show a spot/short pair (auto-detected or manually
  // synced) and that pair's header should read "Spot", not "Futures".
  if (pair) {
    if (pair._spot_short || pair.long?.is_spot ||
        (pair.long?.exchange || '').endsWith('_spot') ||
        (pair.short?.exchange || '').endsWith('_spot')) {
      return 'Spot';
    }
    if (pair._dex_short || pair.long?.is_dex || pair.short?.is_dex) {
      return 'DEX';
    }
  }
  try {
    if (typeof TYPE !== 'undefined') {
      if (TYPE === 'spot') return 'Spot';
      if (TYPE === 'dex')  return 'DEX';
      if (TYPE === 'futures') return 'Futures';
    }
  } catch (_) {}
  return 'Futures';
}

function _renderIfChanged(elId, html){
  if (!window._lastRenderedHTML) window._lastRenderedHTML = new Map();
  const prev = window._lastRenderedHTML.get(elId);
  if (prev === html) return;
  const el = document.getElementById(elId);
  if (!el) return;
  el.innerHTML = html;
  window._lastRenderedHTML.set(elId, html);
}

// Null-safe textContent setter — some pages render only a subset of
// the summary cards (futures pair page replaced the legacy uPnL/realized
// /fees grid with a closed-P&L grid for Stage 2c, which means the old
// `acc-pos-count` etc. only exist on the spot/dex pages now).
function _setText(elId, value) {
  const el = document.getElementById(elId);
  if (el) el.textContent = value;
}

// Spot-short pair fetch — returns the auto-paired (spot long + perp short)
// rows from the backend. Cached on the window so the diff renderer can
// detect when nothing changed and skip a re-render. Errors swallowed —
// the futures path still renders even if this one fails.
async function _accFetchSpotShortPairs() {
  try {
    const r = await Auth.apiFetch('/trade/spot-short-pairs');
    if (!r.ok) return [];
    const j = await r.json();
    if (!Array.isArray(j)) return [];
    // Filter to only "auto" and "paired" — "unpaired" decisions live in the
    // sync drawer but shouldn't render as a live arb-pair card.
    return j.filter(p => p.auto_paired || p.decision === 'paired');
  } catch (_) {
    return [];
  }
}

// Convert a spot-short-pair API row into the same shape `_acc_pair_positions`
// produces for futures pairs, so the renderer can treat them uniformly.
// Long leg gets `is_spot: true` so `_pairModeLabel` returns "Spot".
function _spotShortToPair(ssp) {
  const sp = ssp.spot || {};
  const sh = ssp.short || {};
  // mark = current spot price from backend (now reads go-fetcher's
  // funding.json — accurate).
  // entry: prefer REAL cost basis from venue trade history (backend
  // walks /myTrades and computes VWAP for the held qty). Falls back
  // to short.entry as a paired-open approximation when the venue
  // either has no spot_avg_entry helper or rejected the call (auth /
  // permission). With real entry, spot uPnL = (mark − entry) × qty
  // tracks the actual P&L on the spot leg, not just basis change.
  const mark  = Number(ssp.spot_price_estimate || sh.mark_price || 0);
  const realEntry = Number(sp.avg_entry_price || 0);
  const entry = realEntry > 0
    ? realEntry
    : Number(sh.entry_price || ssp.spot_price_estimate || 0);
  const spotQty = Number(sp.qty || 0);
  const spotUpnl = (entry > 0 && spotQty > 0) ? (mark - entry) * spotQty : 0;
  return {
    symbol: ssp.symbol,
    long: {
      symbol:             ssp.symbol,         // was missing → "undefinedUSDT"
      exchange:           sp.exchange,
      side:               'buy',
      quantity:           spotQty,
      entry_price:        entry,
      mark_price:         mark,
      unrealized_pnl_usd: spotUpnl,
      funding_pnl_usd:    null,
      leverage:           1,
      wallet_id:          sp.wallet_id,
      is_spot:            true,
      _wallet_name:       sp.wallet_name,
    },
    short: {
      ...sh,
      symbol:      sh.symbol || ssp.symbol,
      exchange:    sh.exchange,
      side:        'sell',
      quantity:    Number(sh.quantity || 0),
      entry_price: Number(sh.entry_price || 0),
      mark_price:  Number(sh.mark_price || 0),
      unrealized_pnl_usd: Number(sh.unrealized_pnl_usd || 0),
      funding_pnl_usd:    sh.funding_pnl_usd,
      leverage:    Number(sh.leverage || 1),
      wallet_id:   sh.wallet_id,
    },
    _spot_short: true,
    _decision:   ssp.decision,
    _match_reason: ssp.match_reason,
  };
}

async function accLoadPositions(){
  const tbody = document.getElementById('acc-positions-body');
  const empty = document.getElementById('acc-positions-empty');
  let rows;
  let spotShortPairs = [];
  try {
    // Fire both fetches concurrently — spot-short uses the same
    // list_user_positions internally, but the work IS shared at the
    // backend cache level (4s TTL).
    const [posResp, sspList] = await Promise.all([
      Auth.apiFetch('/trade/positions'),
      _accFetchSpotShortPairs(),
    ]);
    if (!posResp.ok) throw new Error();
    rows = await posResp.json();
    spotShortPairs = sspList;
  } catch {
    // Network/server hiccup — keep showing the last state, do not blank.
    return;
  }
  if (!rows.length){
    _accEmptyStreak++;
    if (_accLastPositions && _accEmptyStreak < _EMPTY_STREAK_LIMIT){
      // Probably a transient blip — keep showing the last good payload.
      return;
    }
    _setText('acc-cnt-positions', '0');
    _setText('acc-pos-count', '0');
    tbody.innerHTML = '';
    empty.style.display = 'block';
    _accLastPositions = [];
    return;
  }
  _accEmptyStreak = 0;
  _accLastPositions = rows;
  _setText('acc-cnt-positions', rows.length);
  _setText('acc-pos-count', rows.length);
  empty.style.display = 'none';

    const {pairs, singles} = _acc_pair_positions(rows);

    // Spot-short pairs come from a separate API. Convert + consume any
    // singles whose (symbol, exchange, side='sell') matches a paired
    // short so we don't double-render the same short leg.
    const sspPairs = spotShortPairs.map(_spotShortToPair);
    const sspShortKeys = new Set(
      sspPairs.map(p => `${p.symbol}|${p.short.exchange}|sell`)
    );
    const filteredSingles = singles.filter(p => {
      const key = `${p.symbol}|${p.exchange}|${p.side}`;
      return !sspShortKeys.has(key);
    });
    // Spot pairs render BEFORE the futures pairs so they sit at the top —
    // they're typically the longer-running cash-and-carry positions while
    // futures pairs come and go faster.
    const allPairs = sspPairs.concat(pairs);

    const sumUsd = (v) => (v>=0?'+':'−') + '$' + Math.abs(v).toFixed(2);
    const rowFor = (p) => {
      const sideCls  = p.side === 'buy' ? 'acc-side-long' : 'acc-side-short';
      const sideText = p.side === 'buy' ? 'LONG' : 'SHORT';
      const pnl = Number(p.unrealized_pnl_usd || 0);
      const mark = Number(p.mark_price || 0);
      const qty  = Number(p.quantity || 0);
      const sizeUsd = qty * mark;
      const pnlCls = pnl >= 0 ? 'acc-pos-pnl-pos' : 'acc-pos-pnl-neg';
      const pnlPct = (p.entry_price > 0 && qty > 0) ? (pnl / (p.entry_price * qty) * 100) : 0;
      const funding = (p.funding_pnl_usd != null)
        ? `<span class="${(Number(p.funding_pnl_usd)||0)>=0?'acc-pos-pnl-pos':'acc-pos-pnl-neg'}">${sumUsd(Number(p.funding_pnl_usd))}</span>`
        : '<span style="color:var(--text3)">—</span>';
      return `
        <tr>
          <td><span class="sym">${p.symbol}</span><span style="color:var(--text3);margin-left:3px">USDT</span></td>
          <td><span class="ex-pill">${(EX_LABEL[p.exchange]||p.exchange)}</span></td>
          <td class="${sideCls}">${sideText}</td>
          <td class="num">${qty.toFixed(4)}<br><span style="color:var(--text3);font-size:10px">${sizeUsd.toFixed(2)} USDT</span></td>
          <td class="num">${Number(p.entry_price||0).toFixed(4)}</td>
          <td class="num" style="color:var(--text2)">${mark.toFixed(4)}</td>
          <td class="num">${funding}</td>
          <td class="num ${pnlCls}">${sumUsd(pnl)}</td>
          <td class="num ${pnlCls}">${pnlPct>=0?'+':''}${pnlPct.toFixed(2)}%</td>
          <td style="white-space:nowrap">
            <button class="pos-close" onclick="tradeClose(${p.wallet_id}, '${p.position_id||p.symbol}')" style="background:transparent;border:1px solid var(--border);color:var(--text3);padding:4px 10px;border-radius:5px;cursor:pointer;font-size:10.5px;font-family:inherit">Close</button>
            <button class="pos-share" title="Share P&amp;L card"
                    data-share='${_htmlEsc(JSON.stringify({symbol:p.symbol,exchange:p.exchange,side:p.side,quantity:qty,entry_price:Number(p.entry_price||0),mark_price:mark,leverage:Number(p.leverage||1),margin_mode:p.margin_mode,unrealized_pnl_usd:pnl,pnl_pct:pnlPct,funding_pnl_usd:(p.funding_pnl_usd!=null?Number(p.funding_pnl_usd):null)}))}'
                    onclick='_openShareFromBtn(this)'
                    style="background:transparent;border:1px solid var(--border);color:var(--green);padding:4px 8px;border-radius:5px;cursor:pointer;font-size:11px;font-family:inherit;margin-left:4px">↗</button>
          </td>
        </tr>`;
    };

    // Paired rows — stacked as a clickable header + both legs grouped below.
    // Header is a collapsible "summary" by default; click toggles the legs.
    // State persists in _accPairOpen so it survives re-renders from the
    // 10s refresh tick. Default collapsed.
    // Defensive: if a row's shape is unexpected (e.g. WS snapshot fields
    // missing), don't let one broken row blank out the whole table.
    // Wrap rowFor to log + fall back to a minimal placeholder.
    const safeRowFor = (p) => {
      try { return rowFor(p); }
      catch (e) {
        console.error('[positions] rowFor failed:', e, 'row=', p);
        return `<tr><td colspan="10" style="color:var(--red);font-size:11px">Row render error · check console</td></tr>`;
      }
    };

    const pairHtml = allPairs.map((pair, pi) => {
      const lp = Number(pair.long.unrealized_pnl_usd || 0);
      const sp = Number(pair.short.unrealized_pnl_usd || 0);
      const totalPnl = lp + sp;
      const lf = Number(pair.long.funding_pnl_usd || 0);
      const sf = Number(pair.short.funding_pnl_usd || 0);
      const hasFunding = (pair.long.funding_pnl_usd != null) || (pair.short.funding_pnl_usd != null);
      const totalFunding = hasFunding ? (lf + sf) : null;
      const lq = Number(pair.long.quantity || 0) * Number(pair.long.mark_price || 0);
      const sq = Number(pair.short.quantity || 0) * Number(pair.short.mark_price || 0);
      const pairUsd = (lq + sq) / 2;
      // Net P&L = price uPnL + accumulated funding (paid/received).
      // For arb pairs the funding is the actual carry — including it in the
      // headline number gives the trader the real take-home, not just the
      // mark-to-market on prices.
      const netPnl = totalPnl + (hasFunding ? totalFunding : 0);
      const pnlCls = netPnl >= 0 ? 'acc-pos-pnl-pos' : 'acc-pos-pnl-neg';
      const fundCls = hasFunding && totalFunding >= 0 ? 'acc-pos-pnl-pos' : 'acc-pos-pnl-neg';
      const combinedPct = pairUsd > 0 ? (netPnl / pairUsd * 100) : 0;
      // Entry spread: divergence between the two legs' entry prices, measured
      // relative to the long leg. Positive = short entered higher than long
      // (favourable for a long-short arb). This is the "spread I entered at".
      const leEntry = Number(pair.long.entry_price || 0);
      const seEntry = Number(pair.short.entry_price || 0);
      const entrySpread = (leEntry > 0 && seEntry > 0) ? ((seEntry - leEntry) / leEntry * 100) : null;
      const entrySpreadTxt = entrySpread != null
        ? `<span class="${entrySpread>=0?'acc-pos-pnl-pos':'acc-pos-pnl-neg'}">${entrySpread>=0?'+':''}${entrySpread.toFixed(4)}%</span>`
        : '<span style="color:var(--text3)">—</span>';
      const pairKey = `${pair.symbol}:${pair.long.exchange}:${pair.short.exchange}`;
      const isOpen = _accPairOpen.has(pairKey);
      const caret = isOpen ? '▾' : '▸';
      const header = `
        <tr class="acc-pair-header" data-pair-key="${pairKey}" onclick="_accPairToggle('${pairKey}')"
            style="background:rgba(26,255,171,0.04);border-top:1px solid rgba(26,255,171,0.18);cursor:pointer;user-select:none">
          <td colspan="2" style="padding:8px 10px">
            <span style="color:var(--green);font-family:monospace;margin-right:6px">${caret}</span>
            <span style="color:var(--green);font-size:10px;font-weight:700;letter-spacing:0.04em">⇆ ${_pairModeLabel(pair)}</span>
            <span class="sym" style="margin-left:6px">${pair.symbol}</span>
            <span style="color:var(--text3);margin-left:3px">USDT</span>
            <span style="color:var(--text3);margin-left:10px;font-size:11px">${EX_LABEL[pair.long.exchange]||pair.long.exchange} ⇄ ${EX_LABEL[pair.short.exchange]||pair.short.exchange}</span>
          </td>
          <td colspan="2" class="num" style="color:var(--text2);font-size:11px">
            ${pairUsd.toFixed(2)} USDT / leg
            <br><span style="color:var(--text3);font-size:10px">entry spread ${entrySpreadTxt}</span>
          </td>
          <td colspan="2" class="num" style="color:var(--text3);font-size:11px">Δ pair</td>
          <td class="num">${hasFunding ? `<span class="${fundCls}">${sumUsd(totalFunding)}</span>` : '<span style="color:var(--text3)">—</span>'}</td>
          <td class="num ${pnlCls}" style="font-weight:700"
              title="${hasFunding ? `Price uPnL ${sumUsd(totalPnl)} + Funding ${sumUsd(totalFunding)} = Net ${sumUsd(netPnl)}` : `Price uPnL ${sumUsd(totalPnl)}`}">
            ${sumUsd(netPnl)}
          </td>
          <td class="num ${pnlCls}">${combinedPct>=0?'+':''}${combinedPct.toFixed(2)}%</td>
          <td style="white-space:nowrap">
            <button class="pos-close"
                    onclick="event.stopPropagation();${pair._spot_short
                      ? `tradeClose(${pair.short.wallet_id}, '${pair.short.position_id||pair.symbol}')`
                      : `_tradeClosePair(${pair.long.wallet_id}, ${pair.short.wallet_id}, '${pair.symbol}')`}"
                    title="${pair._spot_short ? 'Close the short leg only — sell the spot holding manually on the exchange' : ''}"
                    style="background:var(--green);border:none;color:#0a0a0f;padding:4px 10px;border-radius:5px;cursor:pointer;font-size:10.5px;font-weight:700;font-family:inherit">${pair._spot_short ? 'Close Short' : 'Close Both'}</button>
            <button title="Share pair P&amp;L card"
                    data-share-pair='${_htmlEsc(JSON.stringify({
                      symbol: pair.symbol,
                      long: {
                        exchange: pair.long.exchange,
                        side: pair.long.side,
                        quantity: Number(pair.long.quantity || 0),
                        entry_price: Number(pair.long.entry_price || 0),
                        mark_price: Number(pair.long.mark_price || 0),
                        leverage: Number(pair.long.leverage || 1),
                        unrealized_pnl_usd: Number(pair.long.unrealized_pnl_usd || 0),
                      },
                      short: {
                        exchange: pair.short.exchange,
                        side: pair.short.side,
                        quantity: Number(pair.short.quantity || 0),
                        entry_price: Number(pair.short.entry_price || 0),
                        mark_price: Number(pair.short.mark_price || 0),
                        leverage: Number(pair.short.leverage || 1),
                        unrealized_pnl_usd: Number(pair.short.unrealized_pnl_usd || 0),
                      },
                      total_pnl_usd: netPnl,            // includes funding
                      total_price_pnl_usd: totalPnl,    // price-only uPnL
                      total_funding_usd: hasFunding ? totalFunding : null,
                      pair_size_usd: pairUsd,
                      combined_pct: combinedPct,
                      entry_spread_pct: entrySpread,
                    }))}'
                    onclick='event.stopPropagation();_openSharePairFromBtn(this)'
                    style="background:transparent;border:1px solid var(--border);color:var(--green);padding:4px 8px;border-radius:5px;cursor:pointer;font-size:11px;font-family:inherit;margin-left:4px">↗</button>
          </td>
        </tr>`;
      const legs = isOpen ? (safeRowFor(pair.long) + safeRowFor(pair.short)) : '';
      return header + legs;
    }).join('');
    const singlesHtml = filteredSingles.map(safeRowFor).join('');
    _renderIfChanged('acc-positions-body', pairHtml + singlesHtml);

    // P&L pane: total uPnL + total funding across all positions.
    const upnlSum = rows.reduce((a, p) => a + (Number(p.unrealized_pnl_usd)||0), 0);
    const upEl = document.getElementById('acc-upnl');
    if (upEl) { upEl.textContent = (upnlSum>=0?'+':'') + '$' + Math.abs(upnlSum).toFixed(2); upEl.className = 'acc-sum-val ' + (upnlSum>=0?'pos':'neg'); }
    const fundingSum = rows.reduce((a, p) => a + (Number(p.funding_pnl_usd)||0), 0);
    const fEl = document.getElementById('acc-funding-24h');
    if (fEl && rows.some(p => p.funding_pnl_usd != null)) {
      fEl.textContent = (fundingSum>=0?'+':'') + '$' + Math.abs(fundingSum).toFixed(2);
      fEl.className = 'acc-sum-val ' + (fundingSum>=0?'pos':'neg');
    }
}

async function _tradeClosePair(longWid, shortWid, sym){
  const ok = await Confirm.ask({
    title: 'Close both legs?',
    message: 'The long and short positions on this pair will be closed at market with reduce-only orders.',
    okText: 'Close both',
    danger: true,
  });
  if (!ok) return;
  const pending = window.toast && window.toast(
    'Closing pair…', 'loading',
    `<span class="mono">${sym}</span> · both legs`,
  );
  try {
    const [lr, sr] = await Promise.allSettled([
      Auth.apiFetch('/trade/close', {method:'POST', body: JSON.stringify({wallet_id: longWid,  symbol: sym})}),
      Auth.apiFetch('/trade/close', {method:'POST', body: JSON.stringify({wallet_id: shortWid, symbol: sym})}),
    ]);
    const okCount = [lr, sr].filter(r => r.status === 'fulfilled' && r.value.ok).length;
    if (okCount === 2)      pending && pending.update({title:'Pair closed', type:'success'});
    else if (okCount === 1) pending && pending.update({title:'One leg closed', type:'warn', sub:'The other failed — see Order History'});
    else                    pending && pending.update({title:'Close pair failed', type:'error', sub:'see Order History'});
    refreshTradePositions();
    _reloadTradeStatus();
    if (typeof accLoadPositions === 'function') accLoadPositions();
    if (typeof accLoadOrders === 'function') accLoadOrders();
  } catch (e) {
    pending && pending.update({title:'Close pair failed', type:'error', sub:(e.message || 'see Order History').slice(0,200)});
    if (typeof accLoadOrders === 'function') accLoadOrders();
  }
}

async function accLoadBalances(){
  const tbody = document.getElementById('acc-balances-body');
  const empty = document.getElementById('acc-balances-empty');
  try {
    const r = await Auth.apiFetch('/trade/balances');
    if (!r.ok) throw new Error();
    const rows = await r.json();
    if (!rows.length) { tbody.innerHTML = ''; empty.style.display = 'block'; return; }
    empty.style.display = 'none';
    tbody.innerHTML = rows.map(w => {
      return `<tr>
        <td><span class="ex-pill">${(EX_LABEL[w.exchange]||w.exchange)}</span></td>
        <td><span class="sym" style="font-family:Inter,sans-serif;font-weight:600;font-size:11.5px;color:var(--text)">${w.name}</span></td>
        <td>${w.can_trade
            ? '<span style="color:var(--yellow);font-weight:600;font-size:10.5px">TRADE</span>'
            : '<span style="color:var(--teal);font-weight:600;font-size:10.5px">READ</span>'}</td>
        <td class="num">${_renderBalCell(w)}</td>
        <td class="num" style="color:var(--text3)">—</td>
        <td class="num" style="color:var(--text3)">—</td>
      </tr>`;
    }).join('');
    document.getElementById('acc-cnt-balances').textContent = rows.length;
  } catch {
    tbody.innerHTML = '';
    empty.style.display = 'block';
    document.getElementById('acc-cnt-balances').textContent = '0';
  }
}

// ── Watchlist star on infobar ──
let _wlArbId=null;
async function initArbWatch(){
  try{
    const r=await Auth.apiFetch('/screener/watchlist');
    if(!r.ok)return;
    const rows=await r.json();
    const hit=rows.find(x=>x.symbol===SYM&&x.long_exchange===LONG&&x.short_exchange===SHORT);
    if(hit){_wlArbId=hit.id;document.getElementById('wl-star')?.classList.add('on');}
  }catch(_){}
}
async function toggleArbWatch(){
  const btn=document.getElementById('wl-star');if(!btn)return;
  btn.classList.add('pop');setTimeout(()=>btn.classList.remove('pop'),320);
  try{
    if(_wlArbId){
      const r=await Auth.apiFetch(`/screener/watchlist/${_wlArbId}`,{method:'DELETE'});
      if(!r.ok)throw new Error();
      _wlArbId=null;btn.classList.remove('on');_toast('Removed from watchlist');
    }else{
      const r=await Auth.apiFetch('/screener/watchlist',{method:'POST',body:JSON.stringify({symbol:SYM,long_exchange:LONG,short_exchange:SHORT})});
      if(!r.ok)throw new Error();
      const j=await r.json();_wlArbId=j.id;btn.classList.add('on');_toast('Added to watchlist');
    }
  }catch{_toast('Watchlist action failed');}
}

// ═══════════════════════════════════════════════════════════════════════
//  LIVE TRADING PANEL
// ═══════════════════════════════════════════════════════════════════════
const LEVELS = [1, 2, 3, 5, 10, 20, 25, 50, 75, 100, 125];
const _trade = {
  long:  { leverage: 3, maxLeverage: 125, margin: 'isolated', unit: 'token', balance: 0, last: 0, qty: 0, walletId: null, keyStatus: 'missing', orderType: 'market' },
  short: { leverage: 3, maxLeverage: 125, margin: 'isolated', unit: 'token', balance: 0, last: 0, qty: 0, walletId: null, keyStatus: 'missing', orderType: 'market' },
};
let _tradePosTimer = null;

async function initTradePanel(){
  setT('trade-leg-long-ex',  EX_LABEL[LONG]  || LONG);
  setT('trade-leg-short-ex', EX_LABEL[SHORT] || SHORT);
  _trade.long.last  = _opp?.long_price  || 0;
  _trade.short.last = _opp?.short_price || 0;
  setT('trade-last-long',  _trade.long.last  ? '$' + Number(_trade.long.last).toFixed(4)  : '—');
  setT('trade-last-short', _trade.short.last ? '$' + Number(_trade.short.last).toFixed(4) : '—');
  // Placeholder while /trade/status is in flight — otherwise the user sees
  // stale "— USDT" for a couple of seconds and assumes the balance is gone.
  ['long', 'short'].forEach(side => {
    const bal = document.getElementById('trade-bal-' + side);
    if (bal) bal.textContent = 'Loading…';
    const st = document.getElementById('trade-leg-' + side + '-status');
    if (st) { st.textContent = '…'; st.className = 'trade-leg-status missing'; }
  });
  _renderLev('long'); _renderLev('short');
  _renderSubmit('long'); _renderSubmit('short');

  try {
    const r = await Auth.apiFetch('/trade/status?symbol=' + SYM + '&long_ex=' + LONG + '&short_ex=' + SHORT);
    if (r.ok) _applyTradeStatus(await r.json());
  } catch {}

  // Fetch public max leverage per exchange so the stepper can't exceed it
  try {
    const r = await Auth.apiFetch('/trade/leverage-limits?symbol=' + SYM + '&long_ex=' + LONG + '&short_ex=' + SHORT);
    if (r.ok) {
      const d = await r.json();
      if (d.long  && d.long.max_leverage)  _trade.long.maxLeverage  = d.long.max_leverage;
      if (d.short && d.short.max_leverage) _trade.short.maxLeverage = d.short.max_leverage;
      // Qty limits — min/step/max per leg from each venue's contract specs.
      // Drives the inline hint under the qty input + client-side reject.
      _trade.long.qtyLimits  = d.long  && d.long.qty_limits  ? d.long.qty_limits  : null;
      _trade.short.qtyLimits = d.short && d.short.qty_limits ? d.short.qty_limits : null;
      if (typeof ltRefreshQtyHint === 'function') ltRefreshQtyHint();
      // Clamp current choice
      if (_trade.long.leverage  > _trade.long.maxLeverage)  { _trade.long.leverage  = _trade.long.maxLeverage;  _renderLev('long');  tradeRecalc('long'); }
      if (_trade.short.leverage > _trade.short.maxLeverage) { _trade.short.leverage = _trade.short.maxLeverage; _renderLev('short'); tradeRecalc('short'); }
      // Visual hint next to leverage value
      _setLevHint('long',  _trade.long.maxLeverage);
      _setLevHint('short', _trade.short.maxLeverage);
      // Mirror to the new unified Live Trading panel
      if (typeof ltRefreshLeverageOptions === 'function') ltRefreshLeverageOptions();
    }
  } catch {}

  refreshTradePositions();
  // 8s → 3s: trade positions list under the trade card. Same logic as
  // _ptLoadOpenPositions — trader wants live PnL/SL state.
  _tradePosTimer = setInterval(() => { if (document.hidden) return; refreshTradePositions(); }, 3000);
}

function _applyTradeStatus(s){
  ['long','short'].forEach(side => {
    const info = s[side] || {};
    // If user has explicitly picked a different wallet via the LT-panel
    // Keys popover (saved in localStorage), keep their choice. Backend's
    // /trade/status defaults to _find_wallet which is "the" main key on
    // the exchange — but with multiple accounts we let user override.
    const saved = (typeof _ltSavedWalletId === 'function') ? _ltSavedWalletId(side) : null;
    _trade[side].walletId  = saved || info.wallet_id || null;
    _trade[side].balanceError = info.balance_error || null;
    _trade[side].keyStatus = info.status    || 'missing';   // ok | disabled | missing
    _trade[side].balance   = info.balance_usdt || 0;
    // Reservation-aware available — subtracts capital locked by other
    // pending open-triggers so the slider sizes against what user can
    // actually commit. Falls back to balance if backend didn't include
    // available_usdt (e.g. older API version, defensive).
    _trade[side].available = (info.available_usdt != null)
      ? info.available_usdt
      : (info.balance_usdt || 0);
    _trade[side].reserved  = info.reserved_usdt || 0;
    const st = document.getElementById(`trade-leg-${side}-status`);
    if (st) {
      if (info.status === 'ok')           { st.textContent = 'Keys · OK';       st.className = 'trade-leg-status ok'; }
      else if (info.status === 'admin_blocked') { st.textContent = 'Trading paused by admin'; st.className = 'trade-leg-status disabled'; }
      else if (info.status === 'disabled'){ st.textContent = 'Trade disabled';  st.className = 'trade-leg-status disabled'; }
      else                                 { st.textContent = 'No trade key';   st.className = 'trade-leg-status missing'; }
    }
    const bal = document.getElementById(`trade-bal-${side}`);
    if (bal) bal.textContent = (info.balance_usdt != null) ? (Number(info.balance_usdt).toFixed(2) + ' USDT') : '— USDT';
  });
  _renderSubmit('long'); _renderSubmit('short');
  // If the user picked a non-default wallet via the LT Keys popover
  // for either leg, /trade/status returned the default's balance —
  // override with the saved wallet's actual balance from /balances.
  if (typeof _ltSavedWalletId === 'function') {
    _ltApplySavedWalletBalances();
  }
  // Mirror to the new unified Live Trading panel
  if (typeof ltRefreshBalances === 'function') ltRefreshBalances();
  if (typeof ltSyncFromTrade === 'function')   ltSyncFromTrade();
  if (typeof ltRecalc === 'function')          ltRecalc();
}

async function _ltApplySavedWalletBalances() {
  // Apply per-leg saved overrides only if wallet-id differs from what
  // /trade/status returned. /trade/balances is cached 30s server-side.
  const longSaved  = _ltSavedWalletId('long');
  const shortSaved = _ltSavedWalletId('short');
  if (!longSaved && !shortSaved) return;
  try {
    const r = await Auth.apiFetch('/trade/balances');
    if (!r.ok) return;
    const rows = await r.json();
    const byId = Object.fromEntries(rows.map(b => [b.wallet_id, b]));
    if (longSaved && byId[longSaved]) {
      _trade.long.walletId  = longSaved;
      _trade.long.balance   = byId[longSaved].balance_usdt   || 0;
      _trade.long.available = (byId[longSaved].available_usdt != null) ? byId[longSaved].available_usdt : (byId[longSaved].balance_usdt || 0);
      _trade.long.reserved  = byId[longSaved].reserved_usdt  || 0;
      const lbl = document.getElementById('lt-bal-long-name');
      if (lbl) {
        const w = byId[longSaved];
        lbl.textContent = w.name || `wallet #${longSaved}`;
      }
    }
    if (shortSaved && byId[shortSaved]) {
      _trade.short.walletId  = shortSaved;
      _trade.short.balance   = byId[shortSaved].balance_usdt   || 0;
      _trade.short.available = (byId[shortSaved].available_usdt != null) ? byId[shortSaved].available_usdt : (byId[shortSaved].balance_usdt || 0);
      _trade.short.reserved  = byId[shortSaved].reserved_usdt  || 0;
      const lbl = document.getElementById('lt-bal-short-name');
      if (lbl) {
        const w = byId[shortSaved];
        lbl.textContent = w.name || `wallet #${shortSaved}`;
      }
    }
    if (typeof ltRefreshBalances === 'function') ltRefreshBalances();
    if (typeof ltRecalc === 'function')          ltRecalc();
  } catch {}
}

function tradeSwitchTab(btn){
  const leg = btn.dataset.leg;
  document.querySelectorAll(`.trade-tab[data-leg="${leg}"]`).forEach(b => b.classList.toggle('is-active', b === btn));
}

function tradeSetMargin(el){
  const leg = el.parentElement.dataset.leg;
  _trade[leg].margin = el.dataset.v;
  el.parentElement.querySelectorAll('.trade-dd-chip').forEach(c => c.classList.toggle('is-active', c === el));
  tradeRecalc(leg);
}

function tradeSetOtype(el){
  const leg = el.dataset.leg;
  const v = el.dataset.v;
  _trade[leg].orderType = v;
  el.closest('.trade-otype-row').querySelectorAll('.trade-dd-chip').forEach(c => c.classList.toggle('is-active', c === el));
  const priceRow = document.getElementById('trade-price-inp-row-' + leg);
  const priceLbl = document.getElementById('trade-price-lbl-' + leg);
  priceRow.style.display = v === 'market' ? 'none' : 'flex';
  if (priceLbl) priceLbl.textContent = v === 'limit' ? 'Limit Price' : 'Stop Price';
}

function tradeSetUnit(el){
  const leg = el.dataset.leg;
  const newUnit = el.dataset.v;
  const t = _trade[leg];
  if (newUnit === t.unit) return;
  // Convert the value in the input box to the new unit so the user doesn't
  // have to retype. Token↔USDT conversion uses the leg's last price.
  const inp = document.getElementById('trade-size-' + leg);
  const cur = parseFloat(inp.value) || 0;
  if (cur > 0 && t.last > 0) {
    if (t.unit === 'token' && newUnit === 'usdt') {
      inp.value = (cur * t.last).toFixed(2);
    } else if (t.unit === 'usdt' && newUnit === 'token') {
      inp.value = (cur / t.last).toFixed(4);
    }
  }
  t.unit = newUnit;
  document.querySelectorAll(`.trade-unit[data-leg="${leg}"]`).forEach(b => b.classList.toggle('is-active', b === el));
  inp.placeholder = newUnit === 'token' ? `Size in ${SYM}` : 'Size in USDT';
  // Recompute t.qty from the (possibly converted) input and re-render.
  tradeSizeInput(leg);
}

function tradeLevStep(ev, leg, dir){
  ev.preventDefault(); ev.stopPropagation();
  const max = _trade[leg].maxLeverage || 125;
  const cur = _trade[leg].leverage;
  let idx = LEVELS.indexOf(cur);
  if (idx < 0) idx = 2;
  idx = Math.max(0, Math.min(LEVELS.length - 1, idx + dir));
  let next = LEVELS[idx];
  if (next > max) {
    next = max;
    _toast && _toast(`Max leverage on ${EX_LABEL[leg==='long'?LONG:SHORT]||''}: ${max}×`);
  }
  _trade[leg].leverage = next;
  _renderLev(leg);
  tradeRecalc(leg);
}

function _setLevHint(leg, max){
  const el = document.getElementById('trade-lev-val-' + leg);
  if (!el) return;
  el.title = `Max on exchange: ${max}×`;
}

function _renderLev(leg){
  const el = document.getElementById('trade-lev-val-' + leg);
  if (el) el.textContent = _trade[leg].leverage + '×';
}

function tradeSlide(leg, pct){
  document.getElementById('trade-slider-' + leg).value = pct;
  const t = _trade[leg];
  if (!t.balance || !t.last) { tradeRecalc(leg); return; }
  // Usable notional = balance × leverage × (pct/100)
  const notional = t.balance * t.leverage * (pct / 100);
  const qtyTok = t.last > 0 ? notional / t.last : 0;
  const inp = document.getElementById('trade-size-' + leg);
  if (t.unit === 'token') inp.value = qtyTok > 0 ? qtyTok.toFixed(4) : '';
  else                    inp.value = notional > 0 ? notional.toFixed(2) : '';
  t.qty = qtyTok;
  tradeRecalc(leg);
}

function tradeSizeInput(leg){
  const t = _trade[leg];
  const v = parseFloat(document.getElementById('trade-size-' + leg).value) || 0;
  if (t.unit === 'token') t.qty = v;
  else                    t.qty = t.last > 0 ? v / t.last : 0;
  tradeRecalc(leg);
}

function tradeRecalc(leg){
  const t = _trade[leg];
  const posVal = t.qty * t.last;
  const margin = t.leverage > 0 ? posVal / t.leverage : 0;
  setT('trade-posval-' + leg, posVal.toFixed(2) + ' USDT');
  setT('trade-margin-' + leg, margin.toFixed(2) + ' USDT');
  _renderSubmit(leg);
}

// Exchanges where we can't trade programmatically (Paradex Stark sigs not
// yet wired). Submit button becomes a link to the venue's market page.
const _EXTERNAL_TRADE_URLS = {
  paradex: (sym) => `https://app.paradex.trade/trade?market=${encodeURIComponent(sym)}-USD-PERP`,
};

function _renderSubmit(leg){
  const btn = document.getElementById('trade-submit-' + leg);
  if (!btn) return;
  const t = _trade[leg];
  const actionText = leg === 'long' ? 'Open Long' : 'Open Short';

  // External-trade venues (Paradex): turn the button into a redirect.
  const ex = leg === 'long' ? LONG : SHORT;
  const extUrl = _EXTERNAL_TRADE_URLS[ex];
  if (extUrl) {
    btn.disabled = false;
    btn.onclick = () => window.open(extUrl(SYM), '_blank', 'noopener');
    btn.textContent = `${actionText} on ${EX_LABEL[ex] || ex} ↗`;
    _renderArbBtn();
    return;
  }
  btn.onclick = () => tradeOpen(leg);

  let reason = null;
  if (t.keyStatus === 'missing')             reason = 'add API key';
  else if (t.keyStatus === 'admin_blocked')  reason = 'paused by admin';
  else if (t.keyStatus === 'disabled')       reason = 'enable trading';
  else if (!t.last)                          reason = 'waiting for price';
  else if (!t.qty)                           reason = 'enter size';
  btn.disabled = !!reason;
  if (reason) {
    btn.textContent = `${actionText} · ${reason}`;
  } else {
    // Show order size in whichever unit the user is working in. Underneath,
    // quantity sent to the exchange is always TOKEN — the USDT view is just
    // a display convenience, converted from t.last mid price at paint time.
    const usd = t.qty * t.last;
    btn.textContent = t.unit === 'usdt'
      ? `${actionText} · ${usd.toFixed(2)} USDT (${t.qty.toFixed(4)} ${SYM})`
      : `${actionText} · ${t.qty.toFixed(4)} ${SYM} (${usd.toFixed(2)} USDT)`;
  }
  _renderArbBtn();
}

function toggleTradeCard(ev){
  // Don't toggle when the user clicked the Keys button inside the header
  if (ev && ev.target && ev.target.closest('.trade-keys-link')) return;
  const card = document.getElementById('trade-card');
  if (!card) return;
  const nowCollapsed = card.classList.toggle('is-collapsed');
  try { localStorage.setItem('trade-card-open', nowCollapsed ? '0' : '1'); } catch {}
}

// Restore last open/closed state (default: open)
(function _restoreTradeCardState(){
  try {
    if (localStorage.getItem('trade-card-open') === '0') {
      const card = document.getElementById('trade-card');
      if (card) card.classList.add('is-collapsed');
    }
  } catch {}
})();

function _renderArbBtn(){
  const btn = document.getElementById('trade-arb-btn');
  const txt = document.getElementById('trade-arb-text');
  if (!btn || !txt) return;
  // Two-leg open is impossible if either leg is an external-trade venue
  // (Paradex). User has to use the per-leg button to redirect.
  const extLeg = _EXTERNAL_TRADE_URLS[LONG] ? EX_LABEL[LONG]
               : _EXTERNAL_TRADE_URLS[SHORT] ? EX_LABEL[SHORT] : null;
  if (extLeg) {
    btn.disabled = true;
    txt.textContent = `Open Both · ${extLeg} needs a manual order`;
    return;
  }
  const L = _trade.long, S = _trade.short;
  const bothKeysOk = L.keyStatus === 'ok' && S.keyStatus === 'ok';
  const bothQty    = L.qty > 0 && S.qty > 0;
  const enabled    = bothKeysOk && bothQty;
  btn.disabled = !enabled;
  if (!bothKeysOk) { txt.textContent = 'Open Both · need trade keys on both exchanges'; return; }
  if (!bothQty)    { txt.textContent = 'Open Both · enter size on both legs'; return; }
  txt.textContent = `Open Both · ${L.qty.toFixed(4)} long on ${EX_LABEL[LONG]||LONG} / ${S.qty.toFixed(4)} short on ${EX_LABEL[SHORT]||SHORT}`;
}

async function tradeOpenArb(){
  if (_tradeInflight.arb) return;
  const L = _trade.long, S = _trade.short;
  _showLegErr('long', null); _showLegErr('short', null);
  if (L.qty <= 0 || S.qty <= 0) { _toast('Enter size on both legs'); return; }
  if (!L.walletId || !S.walletId) { _toast('Screener API keys missing on one of the exchanges'); return; }
  // Market order on both legs — fire both without a price-confirmation prompt.
  _tradeInflight.arb = true;
  const btn = document.getElementById('trade-arb-btn');
  const txt = document.getElementById('trade-arb-text');
  const prev = txt.textContent;
  btn.disabled = true; txt.innerHTML = '<span class="trade-busy-ring"></span>Submitting both legs…';
  const pending = window.toast && window.toast(
    'Opening pair…', 'loading',
    `<span class="mono">${SYM}</span> · LONG ${EX_LABEL[LONG]||LONG} · SHORT ${EX_LABEL[SHORT]||SHORT}`,
  );
  try {
    const r = await Auth.apiFetch('/trade/open-arb', {
      method: 'POST',
      body: JSON.stringify({
        symbol: SYM,
        long_wallet_id:  L.walletId, long_quantity:  L.qty, long_leverage:  L.leverage, long_margin_mode:  L.margin,
        short_wallet_id: S.walletId, short_quantity: S.qty, short_leverage: S.leverage, short_margin_mode: S.margin,
      }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || 'Request failed');
    if (body.fully_filled) {
      pending && pending.update({title:'Both legs filled ✓', type:'success'});
      document.getElementById('trade-size-long').value = '';
      document.getElementById('trade-size-short').value = '';
      _trade.long.qty = 0; _trade.short.qty = 0;
      tradeRecalc('long'); tradeRecalc('short');
    } else {
      if (body.long?.ok && !body.short?.ok) {
        _showLegErr('short', 'SHORT failed: ' + (body.short.error || 'unknown'));
        pending && pending.update({title:'Long filled, SHORT failed', type:'error', sub: body.short?.error || ''});
      } else if (body.short?.ok && !body.long?.ok) {
        _showLegErr('long',  'LONG failed: '  + (body.long.error  || 'unknown'));
        pending && pending.update({title:'Short filled, LONG failed', type:'error', sub: body.long?.error || ''});
      } else {
        _showLegErr('long',  body.long?.error  || 'Both legs failed');
        _showLegErr('short', body.short?.error || 'Both legs failed');
        pending && pending.update({title:'Both legs failed', type:'error'});
      }
    }
    refreshTradePositions();
    _reloadTradeStatus();
    if (typeof accLoadPositions === 'function') accLoadPositions();
    if (typeof accLoadOrders === 'function') accLoadOrders();
  } catch (e) {
    pending && pending.update({title:'Open failed', type:'error', sub: e.message || ''});
    _showLegErr('long', e.message || 'Order failed');
    _showLegErr('short', e.message || 'Order failed');
    if (typeof accLoadOrders === 'function') accLoadOrders();
  } finally {
    _tradeInflight.arb = false;
    txt.textContent = prev; _renderArbBtn();
  }
}

function _showLegErr(leg, msg){
  const el = document.getElementById('trade-err-' + leg);
  if (!el) return;
  if (!msg) { el.style.display = 'none'; el.innerHTML = ''; return; }
  const s = String(msg);
  // HL max OI exceeded — surface a specific, actionable message.
  const isMaxOI = /max.*open.interest|open.interest.*max|oi.*cap|cap.*oi/i.test(s);
  const ex = leg === 'long' ? LONG : SHORT;
  let body;
  if (isMaxOI && ex === 'hyperliquid') {
    const oiStr = window._hlOiUsd ? ' Current OI: ' + fmtV(window._hlOiUsd) + ' USD.' : '';
    body = `<span class="tx">HL max open interest reached for ${SYM}.${oiStr} New long positions are blocked — wait for OI to decrease or use a different exchange.</span><button class="tc" onclick="_showLegErr('${leg}', null)">×</button>`;
  } else {
    body = `<span class="tx">${s.replace(/</g,'&lt;')}</span><button class="tc" onclick="_showLegErr('${leg}', null)">×</button>`;
  }
  el.innerHTML = body;
  el.style.display = 'flex';
}

const _tradeInflight = { long: false, short: false, arb: false };

async function tradeOpen(leg){
  if (_tradeInflight[leg]) return;
  const t = _trade[leg];
  _showLegErr(leg, null);
  if (!t.qty || t.qty <= 0) { _showLegErr(leg, 'Enter size first'); return; }
  if (!t.walletId)          { _showLegErr(leg, 'Set a Screener API key for this exchange on /profile'); return; }
  const sideText = leg === 'long' ? 'LONG' : 'SHORT';
  // Market order — fire immediately without a price-confirmation prompt.
  // The button itself already shows the size (e.g. "Open Long · 12.3456 TOK"),
  // which is the only confirmation the user needs for a taker order.
  _tradeInflight[leg] = true;
  const btn = document.getElementById('trade-submit-' + leg);
  btn.disabled = true;
  const prev = btn.textContent;
  btn.innerHTML = '<span class="trade-busy-ring"></span>Submitting…';
  const venueLabel = EX_LABEL[leg === 'long' ? LONG : SHORT] || '';
  const pending = window.toast && window.toast(
    `Opening ${sideText}…`, 'loading',
    `<span class="mono">${SYM}</span> · ${venueLabel} · ${t.qty} ${SYM}`,
  );
  try {
    const _orderBody = {
      wallet_id: t.walletId, symbol: SYM,
      side: leg === 'long' ? 'buy' : 'sell',
      quantity: t.qty, leverage: t.leverage, margin_mode: t.margin,
    };
    if (t.orderType && t.orderType !== 'market') {
      _orderBody.order_type = t.orderType;
      const _priceInp = document.getElementById('trade-price-' + leg);
      const _priceVal = parseFloat(_priceInp?.value);
      if (_priceVal > 0) {
        if (t.orderType === 'limit') _orderBody.limit_price = _priceVal;
        else _orderBody.stop_price = _priceVal;
      }
    }
    const r = await Auth.apiFetch('/trade/open', {
      method: 'POST',
      body: JSON.stringify(_orderBody),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || 'Order failed');
    pending && pending.update({
      title: `${sideText} filled ✓`, type:'success',
      sub: body.avg_price ? `@ <span class="mono">${body.avg_price}</span>` : (body.order_id ? `id ${body.order_id}` : ''),
    });
    // Immediate refresh, balance, positions everywhere
    refreshTradePositions();
    _reloadTradeStatus();
    if (typeof accLoadPositions === 'function') accLoadPositions();
    if (typeof accLoadOrders === 'function') accLoadOrders();
    // Reset size
    document.getElementById('trade-size-' + leg).value = '';
    _trade[leg].qty = 0; tradeRecalc(leg);
  } catch (e) {
    pending && pending.update({title: `${sideText} failed`, type:'error', sub: e.message || ''});
    _showLegErr(leg, e.message || 'Order failed');
    if (typeof accLoadOrders === 'function') accLoadOrders();
  } finally {
    _tradeInflight[leg] = false;
    btn.textContent = prev; _renderSubmit(leg);
  }
}

async function _reloadTradeStatus(){
  try {
    const r = await Auth.apiFetch('/trade/status?symbol=' + SYM + '&long_ex=' + LONG + '&short_ex=' + SHORT);
    if (r.ok) _applyTradeStatus(await r.json());
  } catch {}
}

async function tradeClose(wid, posId){
  const ok = await Confirm.ask({
    title: 'Close this position?',
    message: 'The position will be closed at market with a reduce-only order.',
    okText: 'Close at market',
    danger: true,
  });
  if (!ok) return;
  const pending = window.toast && window.toast(
    'Closing position…', 'loading',
    `<span class="mono">${SYM}</span>`,
  );
  try {
    const r = await Auth.apiFetch('/trade/close', {
      method: 'POST',
      body: JSON.stringify({ wallet_id: wid, symbol: SYM }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || 'Close failed');
    pending && pending.update({
      title: 'Position closed', type:'success',
      sub: body.avg_price ? `@ <span class="mono">${body.avg_price}</span>` : '',
    });
    refreshTradePositions();
    _reloadTradeStatus();
    if (typeof accLoadPositions === 'function') accLoadPositions();
    if (typeof accLoadOrders === 'function') accLoadOrders();
  } catch (e) {
    pending && pending.update({title:'Close failed', type:'error', sub: (e.message || 'see Order History').slice(0, 200)});
    if (typeof accLoadOrders === 'function') accLoadOrders();
  }
}

// Sticky for the trade-panel position list — same idea as accLoadPositions.
let _tpLastPositions = null;
let _tpEmptyStreak = 0;

async function refreshTradePositions(){
  let rows, spotPairs = [];
  try {
    const r = await Auth.apiFetch('/trade/positions?symbol=' + SYM);
    if (!r.ok) return;
    rows = await r.json();
  } catch {
    return;
  }
  // Spot/short pair candidates — fire-and-forget; don't block on it.
  try {
    const r2 = await Auth.apiFetch('/trade/spot-short-pairs');
    if (r2.ok) {
      const all = await r2.json();
      spotPairs = (all || []).filter(p => (p.symbol || '').toUpperCase() === SYM);
    }
  } catch {}

  const wrap = document.getElementById('trade-positions');
  const list = document.getElementById('trade-positions-list');
  if (!rows.length){
    _tpEmptyStreak++;
    if (_tpLastPositions && _tpEmptyStreak < _EMPTY_STREAK_LIMIT){
      return;
    }
    _tpLastPositions = [];
    wrap.style.display = 'none';
    return;
  }
  _tpEmptyStreak = 0;
  _tpLastPositions = rows;
  wrap.style.display = 'block';

  const spotByShort = {};
  for (const p of spotPairs){
    const key = `${(p.short.exchange||'').toLowerCase()}|${p.short.wallet_id||0}`;
    spotByShort[key] = p;
  }

  const html = rows.map(p => {
    const pnlCls = p.unrealized_pnl_usd >= 0 ? 'pos' : 'neg';
    const sideCls = p.side === 'buy' ? 'side-long' : 'side-short';
    const qty = Number(p.quantity || 0);
    const entry = Number(p.entry_price || 0);
    const mark = Number(p.mark_price || 0);
    const pnl = Number(p.unrealized_pnl_usd || 0);
    const funding = Number(p.funding_pnl_usd || 0);
    const pnlPct = (entry > 0 && qty > 0) ? (pnl / (entry * qty) * 100) : 0;
    const shareData = _htmlEsc(JSON.stringify({
      symbol: p.symbol, exchange: p.exchange, side: p.side,
      quantity: qty, entry_price: entry, mark_price: mark,
      leverage: Number(p.leverage || 1), margin_mode: p.margin_mode,
      unrealized_pnl_usd: pnl, pnl_pct: pnlPct,
    }));

    let fundingRow = '';
    if (p.funding_pnl_usd !== null && p.funding_pnl_usd !== undefined) {
      const fLabel = funding >= 0 ? 'Funding earned' : 'Funding paid';
      const fColor = funding >= 0 ? 'var(--green)' : 'var(--red)';
      fundingRow = `<div style="grid-column:1/-1;font-size:10px;color:var(--text3);padding:2px 0 0 4px">
        ${fLabel}: <span style="color:${fColor}">${funding>=0?'+':''}$${Math.abs(funding).toFixed(2)}</span>
      </div>`;
    }

    let spotPairRow = '';
    if (p.side !== 'buy') {
      const spKey = `${(p.exchange||'').toLowerCase()}|${p.wallet_id||0}`;
      const sp = spotByShort[spKey];
      if (sp) {
        const spotEx = EX_LABEL[(sp.spot.exchange||'').toLowerCase()] || sp.spot.exchange;
        const spotQty = Number(sp.spot.qty || 0).toFixed(6).replace(/0+$/,'').replace(/\.$/,'');
        const reason = sp.match_reason || '';
        const decided = sp.decision === 'paired';
        const tag = decided ? '✓ confirmed' : '◆ auto-detected';
        const tagColor = decided ? 'var(--green)' : 'var(--text3)';
        const dp = _htmlEsc(JSON.stringify({
          symbol: SYM, spot_wallet_id: sp.spot.wallet_id,
          short_exchange: sp.short.exchange, short_wallet_id: sp.short.wallet_id,
        }));
        const buttonsHtml = decided
          ? `<button data-payload='${dp}' onclick="_ssPairDecide(this,'unpaired')" style="background:transparent;border:1px solid var(--border);color:var(--text3);padding:2px 7px;border-radius:4px;cursor:pointer;font-size:10px;font-family:inherit">Unpair</button>`
          : `<button data-payload='${dp}' onclick="_ssPairDecide(this,'paired')" style="background:transparent;border:1px solid var(--green);color:var(--green);padding:2px 7px;border-radius:4px;cursor:pointer;font-size:10px;font-family:inherit">Confirm pair</button>
             <button data-payload='${dp}' onclick="_ssPairDecide(this,'unpaired')" style="background:transparent;border:1px solid var(--border);color:var(--text3);padding:2px 7px;border-radius:4px;cursor:pointer;font-size:10px;font-family:inherit">Not paired</button>`;
        spotPairRow = `<div style="grid-column:1/-1;display:flex;gap:6px;align-items:center;font-size:10.5px;padding:5px 4px 0;border-top:1px dashed var(--border);margin-top:5px;color:var(--text2);flex-wrap:wrap">
          <span style="color:var(--green);font-weight:600">SPOT</span>
          <span>${spotQty} ${SYM} on ${spotEx}</span>
          <span style="color:${tagColor};font-size:10px">${tag}</span>
          <span style="color:var(--text3);font-size:10px">${_htmlEsc(reason)}</span>
          <span style="margin-left:auto;display:flex;gap:4px">${buttonsHtml}</span>
        </div>`;
      }
    }

    return `
      <div class="trade-position-row" style="display:grid;grid-template-columns:auto auto 1fr auto auto auto">
        <span class="${sideCls}">${p.side === 'buy' ? 'LONG' : 'SHORT'}</span>
        <span style="color:var(--text3);font-size:10px">${EX_LABEL[p.exchange] || p.exchange}</span>
        <span class="pos-qty">${qty.toFixed(4)} ${SYM}</span>
        <span class="pos-pnl ${pnlCls}">${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}</span>
        <button class="pos-close" onclick="tradeClose(${p.wallet_id}, '${p.position_id}')">Close</button>
        <button class="pos-share" title="Share P&amp;L card" data-share='${shareData}'
                onclick='_openShareFromBtn(this)'
                style="background:transparent;border:1px solid var(--border);color:var(--green);padding:3px 7px;border-radius:5px;cursor:pointer;font-size:11px;margin-left:4px">↗</button>
        ${fundingRow}
        ${spotPairRow}
      </div>`;
  }).join('');
  _renderIfChanged('trade-positions-list', html);
}

async function _ssPairDecide(btn, decision){
  let payload;
  try { payload = JSON.parse(btn.getAttribute('data-payload') || '{}'); }
  catch { return; }
  if (!payload.symbol || !payload.spot_wallet_id || !payload.short_exchange || !payload.short_wallet_id) return;
  btn.disabled = true;
  try {
    const path = decision === 'paired' ? '/trade/pair/spot-short/sync' : '/trade/pair/spot-short/unsync';
    const r = await Auth.apiFetch(path, { method: 'POST', body: JSON.stringify(payload) });
    if (!r.ok) { _toast('Pair update failed', 'error'); btn.disabled = false; return; }
    _toast(decision === 'paired' ? 'Pair confirmed' : 'Marked as not paired', 'success');
    refreshTradePositions();
  } catch {
    _toast('Pair update failed', 'error');
    btn.disabled = false;
  }
}

// ═══════════════════════════════════════════════════════════════════════
//  KEYS POPUP — opens from "Keys ⚙" in the trading card and account toolbar
// ═══════════════════════════════════════════════════════════════════════
async function openKeysPopup(){
  const bd = document.getElementById('keys-pop-backdrop');
  bd.classList.add('open');
  await _renderKeysPop();
}
function closeKeysPopup(){
  document.getElementById('keys-pop-backdrop')?.classList.remove('open');
}

async function _renderKeysPop(){
  const body = document.getElementById('keys-pop-body');
  if (!body) return;
  body.innerHTML = '<div class="keys-pop-empty"><span class="spinner"></span> Loading keys…</div>';
  try {
    const r = await Auth.apiFetch('/wallets');
    if (!r.ok) throw new Error();
    const all = await r.json();
    const legs = [
      { side:'long',  exch: LONG,  label: EX_LABEL[LONG]  || LONG  },
      { side:'short', exch: SHORT, label: EX_LABEL[SHORT] || SHORT },
    ];
    body.innerHTML = legs.map(leg => {
      const keys = all.filter(w => w.wallet_type === 'exchange' && !w.is_archived && (w.type_value||'').toLowerCase() === leg.exch);
      const hasTrade = keys.some(k => k.purpose === 'screener' || k.purpose === 'both');
      return `
        <div class="kp-leg">
          <div class="kp-leg-hdr">
            <span class="kp-leg-badge ${leg.side}">${leg.side.toUpperCase()}</span>
            <span class="kp-leg-ex">${leg.label}</span>
            <span class="kp-leg-status ${hasTrade ? 'ok' : 'missing'}">${hasTrade ? 'Trade key ✓' : 'No trade key'}</span>
          </div>
          <div class="kp-leg-body">
            ${keys.length ? keys.map(k => `
              <div class="kp-key-row">
                <span class="kp-key-name">${_htmlEsc(k.name)}</span>
                <span class="kp-key-mask">${_htmlEsc(k.display_info)}</span>
                <span class="kp-key-pills">
                  ${k.purpose === 'both'
                    ? '<span class="kp-pill p">PORTFOLIO</span><span class="kp-pill s">SCREENER</span>'
                    : k.purpose === 'screener'
                      ? '<span class="kp-pill s">SCREENER</span>'
                      : '<span class="kp-pill p">PORTFOLIO</span>'}
                </span>
              </div>`).join('')
              : '<div class="kp-empty">No API key yet for ' + leg.label + '.</div>'}
            <a class="kp-add" href="/profile#keys-card" target="_blank" rel="noopener">
              <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 2v8M2 6h8"/></svg>
              Add ${leg.label} key
            </a>
          </div>
        </div>`;
    }).join('');
  } catch {
    body.innerHTML = '<div class="keys-pop-empty">Could not load keys. Try again.</div>';
  }
}

function _htmlEsc(s){ return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeKeysPopup(); });

/* ── arb.html block #3 ─────────────────────────────────────────── */
  function toggleExStatusBar(){
    const el = document.getElementById('ex-status-strip');
    const hidden = el.classList.toggle('is-hidden');
    try { localStorage.setItem('ex-status-hidden', hidden ? '1' : '0'); } catch(_){}
  }
  (function(){
    try {
      // Default closed — only stored '0' (user explicitly opened it) reveals.
      if (localStorage.getItem('ex-status-hidden') === '0') {
        document.getElementById('ex-status-strip').classList.remove('is-hidden');
      }
    } catch(_){}
  })();

/* ── arb.html block #4 ─────────────────────────────────────────── */
(function(){
  const _FALLBACK = ['binance','bybit','okx','gate','kucoin','mexc','bitget','bingx','htx','whitebit','hyperliquid','aster','ethereal','paradex','extended','lighter'];
  let EXES = (window.EX && window.EX.lists && window.EX.lists.screener_all && window.EX.lists.screener_all.length) ? window.EX.lists.screener_all : _FALLBACK;
  const LABELS = (window.EX && window.EX.labels) || {};
  const FRESH_S = 8, STALE_S = 30;
  const wrap = document.getElementById('ex-status-items');
  function rebuild(){
    // Status strip: dot + venue name only. Age is hidden in the visual
    // strip (kept as title attribute for hover) — clutters the bar
    // without adding actionable info; the dot already encodes
    // fresh/slow/stale.
    wrap.innerHTML = EXES.map(e => `<span class="as-ex" id="exs-${e}"><span class="d"></span><span class="nm">${LABELS[e]||e}</span></span>`).join('');
  }
  rebuild();
  if (window.EX && window.EX.ready) {
    window.EX.ready.then(() => {
      if (window.EX.lists.screener_all.length) {
        EXES = window.EX.lists.screener_all;
        rebuild();
      }
    });
  }

  async function tick(){
    try {
      const r = await Auth.apiFetch('/screener/exchange-health');
      if (!r.ok) return;
      const j = await r.json();
      const exs = (j && j.exchanges) || {};
      const klasses = [];
      for (const ex of EXES) {
        const h = exs[ex] || {};
        const el = document.getElementById('exs-'+ex);
        if (!el) continue;
        const d = el.querySelector('.d');
        const age = (typeof h.age_s === 'number') ? h.age_s : null;
        const kls = (h.via === 'none' || age == null) ? 'unknown' : age <= FRESH_S ? 'live' : age <= STALE_S ? 'slow' : 'stale';
        d.className = 'd ' + kls;
        klasses.push(kls);
        el.title = `${LABELS[ex]||ex} — ${h.via || '?'}${age != null ? ' · '+age.toFixed(1)+'s' : ''}`;
      }
      const overall = klasses.includes('stale') ? 'stale' : klasses.includes('slow') ? 'slow' : klasses.every(k => k === 'unknown') ? 'unknown' : 'live';
      const ldot = document.getElementById('ex-status-lbl-dot');
      if (ldot) ldot.className = 'lbl-dot ' + overall;
      const ldotMini = document.getElementById('ex-status-lbl-dot-mini');
      if (ldotMini) ldotMini.className = 'lbl-dot ' + overall;
    } catch(_) {}
  }
  tick();
  setInterval(tick, 3000);
})();

/* ── arb.html block #5 ─────────────────────────────────────────── */
// _syncRows: futures positions (live, from /trade/positions).
// _syncSpotItems: spot holdings (one row per (wallet, asset)) derived
//   from /trade/spot-short-pairs — we de-dup by spot wallet_id+symbol
//   so a holding that matches multiple shorts only shows once.
// _syncSelected: keys of currently picked items. Keys are prefixed:
//   'f|sym|exchange|side|wallet_id' for futures
//   's|sym|exchange|wallet_id'       for spot
let _syncRows = [], _syncSpotItems = [], _syncSelected = new Set();

async function openSyncPairs(){
  const bd = document.getElementById('sync-backdrop');
  if (!bd) return;
  bd.classList.add('open');
  _syncSelected.clear();
  document.getElementById('sync-list').innerHTML = '<div style="padding:20px;color:var(--text3);font-size:12px;text-align:center">Loading positions…</div>';
  try {
    // Load futures positions and spot-short candidates in parallel.
    // /spot-short-pairs returns rows where the asset matches an open
    // short ticker — exactly the candidates we want to surface in
    // Sync UI as pickable LONG legs.
    const [posR, sspR] = await Promise.all([
      Auth.apiFetch('/trade/positions'),
      Auth.apiFetch('/trade/spot-short-pairs'),
    ]);
    if (!posR.ok) throw new Error('positions ' + posR.status);
    _syncRows = await posR.json() || [];
    if (sspR.ok) {
      const ssp = (await sspR.json()) || [];
      // De-dup by (wallet_id|asset) — one spot holding can match
      // several shorts simultaneously (one per short venue), but we
      // want exactly one Sync row per spot leg.
      const seen = new Set();
      _syncSpotItems = [];
      for (const r of ssp) {
        const sp = r.spot || {};
        const k = `${sp.wallet_id}|${(r.symbol || '').toUpperCase()}`;
        if (seen.has(k)) continue;
        seen.add(k);
        _syncSpotItems.push({
          symbol: r.symbol,
          exchange: sp.exchange,
          wallet_id: sp.wallet_id,
          wallet_name: sp.wallet_name,
          qty: Number(sp.qty || 0),
          // Pre-compute one default short pair-target (the first short
          // we saw for this asset) so confirmSyncPair can resolve the
          // wallet_id of the chosen short leg.
        });
      }
    } else {
      _syncSpotItems = [];
    }
  } catch (e) {
    document.getElementById('sync-list').innerHTML = `<div style="padding:20px;color:var(--red);font-size:12px;text-align:center">Failed to load positions: ${e.message || e}</div>`;
    return;
  }
  _renderSyncList();
}
function closeSyncPairs(){
  document.getElementById('sync-backdrop')?.classList.remove('open');
}

function _renderSyncList(){
  const wrap = document.getElementById('sync-list');
  if (!wrap) return;
  const manual = _loadManualPairs();
  const exLbl = (e) => (window.EX && window.EX.labels && window.EX.labels[e]) || (e || '').toUpperCase();

  // Section 1: existing manual pairs (with "Unpair" button)
  const pairedHTML = manual.length ? `
    <div style="font-size:10px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;padding:4px 0">Active pairs (${manual.length})</div>
    ${manual.map((p, i) => `
      <div style="display:flex;align-items:center;gap:10px;padding:8px 10px;background:var(--surface2);border:1px solid var(--green);border-radius:6px;font-size:12px">
        <span style="color:var(--text)">${p.symbol}</span>
        <span style="color:var(--text3);font-size:10px">${exLbl(p.long_exchange)} <span style="color:var(--green)">⇄</span> ${exLbl(p.short_exchange)}</span>
        <button onclick='_syncUnpair(${i})' style="margin-left:auto;background:transparent;border:1px solid var(--border);color:var(--text3);padding:3px 8px;border-radius:5px;cursor:pointer;font-size:10.5px">Unpair</button>
      </div>
    `).join('')}
    <div style="height:6px"></div>
  ` : '';

  // Section 2: pickable single positions (futures + spot)
  const isInManualPair = (p) => manual.some(m => m.symbol === p.symbol &&
    ((m.long_exchange === p.exchange && p.side === 'buy') ||
     (m.short_exchange === p.exchange && p.side === 'sell')));

  // Futures rows (open positions on perp venues)
  const futurePickable = _syncRows.filter(p =>
    !isInManualPair(p) && Math.abs(Number(p.quantity || 0)) > 0);
  const futureHTML = futurePickable.map(p => {
    const k = `f|${p.symbol}|${p.exchange}|${p.side}|${p.wallet_id || 0}`;
    const isSel = _syncSelected.has(k);
    const sideTxt = p.side === 'buy' ? 'LONG' : 'SHORT';
    const sideCol = p.side === 'buy' ? 'var(--green)' : 'var(--red)';
    const qty = Number(p.quantity || 0).toFixed(4);
    return `
      <div style="display:flex;align-items:center;gap:10px;padding:8px 10px;background:${isSel?'rgba(26,255,171,0.10)':'var(--surface2)'};border:1px solid ${isSel?'var(--green)':'var(--border)'};border-radius:6px;cursor:pointer;font-size:12px;transition:background .12s,border-color .12s" onclick="_syncToggle('${k}')">
        <input type="checkbox" ${isSel?'checked':''} style="cursor:pointer" onclick="event.stopPropagation();_syncToggle('${k}')">
        <span style="color:${sideCol};font-weight:700;font-size:10px">${sideTxt}</span>
        <span style="color:var(--text)">${p.symbol}</span>
        <span style="color:var(--text3);font-size:10px">${exLbl(p.exchange)}</span>
        <span class="mono" style="color:var(--text3);margin-left:auto;font-size:10.5px">${qty}</span>
      </div>`;
  }).join('');

  // Spot rows — same shape but tagged as SPOT, key carries wallet_id so
  // confirmSyncPair can dispatch to /pair/spot-short/sync. Surfaced
  // for any spot holding whose ticker matches an open short, regardless
  // of notional/time match (per user request).
  const spotHTML = _syncSpotItems.map(s => {
    const k = `s|${s.symbol}|${s.exchange}|${s.wallet_id || 0}`;
    const isSel = _syncSelected.has(k);
    const qty = Number(s.qty || 0).toFixed(4);
    return `
      <div style="display:flex;align-items:center;gap:10px;padding:8px 10px;background:${isSel?'rgba(26,255,171,0.10)':'var(--surface2)'};border:1px solid ${isSel?'var(--green)':'var(--border)'};border-radius:6px;cursor:pointer;font-size:12px;transition:background .12s,border-color .12s" onclick="_syncToggle('${k}')">
        <input type="checkbox" ${isSel?'checked':''} style="cursor:pointer" onclick="event.stopPropagation();_syncToggle('${k}')">
        <span style="color:var(--green);font-weight:700;font-size:10px">SPOT</span>
        <span style="color:var(--text)">${s.symbol}</span>
        <span style="color:var(--text3);font-size:10px">${exLbl(s.exchange)}</span>
        <span class="mono" style="color:var(--text3);margin-left:auto;font-size:10.5px">${qty}</span>
      </div>`;
  }).join('');

  const allItems = futureHTML + spotHTML;
  const rowsHTML = allItems || '<div style="padding:14px;color:var(--text3);font-size:12px;text-align:center">No unpaired positions or spot holdings.</div>';

  wrap.innerHTML = pairedHTML + rowsHTML;

  // Enable button only when 2 selected covering one LONG side
  // (futures buy OR spot) + one SHORT (futures sell), same symbol,
  // different venues.
  const sels = [..._syncSelected].map(k => _syncDecodeKey(k)).filter(Boolean);
  const longLeg  = sels.find(s => s.kind === 'spot' || (s.kind === 'futures' && s.side === 'buy'));
  const shortLeg = sels.find(s => s.kind === 'futures' && s.side === 'sell');
  const valid = sels.length === 2 && longLeg && shortLeg &&
                longLeg.sym === shortLeg.sym &&
                longLeg.ex !== shortLeg.ex;
  document.getElementById('sync-pair-btn').disabled = !valid;
}

// Decode the sync key into a tagged record. Returns null on garbage.
function _syncDecodeKey(k) {
  if (!k) return null;
  const parts = k.split('|');
  if (parts[0] === 'f' && parts.length === 5) {
    return { kind: 'futures', sym: parts[1], ex: parts[2], side: parts[3], wallet_id: parseInt(parts[4]||'0', 10) };
  }
  if (parts[0] === 's' && parts.length === 4) {
    return { kind: 'spot', sym: parts[1], ex: parts[2], side: 'buy', wallet_id: parseInt(parts[3]||'0', 10) };
  }
  return null;
}

function _syncToggle(k){
  if (_syncSelected.has(k)) _syncSelected.delete(k);
  else {
    if (_syncSelected.size >= 2){
      // Replace oldest
      const first = _syncSelected.values().next().value;
      _syncSelected.delete(first);
    }
    _syncSelected.add(k);
  }
  _renderSyncList();
}

async function confirmSyncPair(){
  const sels = [..._syncSelected].map(k => _syncDecodeKey(k)).filter(Boolean);
  if (sels.length !== 2) return;
  const longLeg  = sels.find(s => s.kind === 'spot' || (s.kind === 'futures' && s.side === 'buy'));
  const shortLeg = sels.find(s => s.kind === 'futures' && s.side === 'sell');
  if (!longLeg || !shortLeg || longLeg.sym !== shortLeg.sym) return;

  if (longLeg.kind === 'spot') {
    // Spot/short pair: needs both wallet IDs. Hits the dedicated
    // endpoint that stores leg_a_key prefix "spot|<wallet_id>".
    try {
      const r = await Auth.apiFetch('/trade/pair/spot-short/sync', {
        method: 'POST',
        body: JSON.stringify({
          symbol: longLeg.sym,
          spot_wallet_id: longLeg.wallet_id,
          short_exchange: shortLeg.ex,
          short_wallet_id: shortLeg.wallet_id,
        }),
      });
      if (!r.ok) {
        const txt = await r.text().catch(() => '');
        if (typeof toast === 'function') toast('Spot/Short pair failed: ' + (txt || r.status), 'error');
        return;
      }
      if (typeof toast === 'function') toast(`Paired ${longLeg.sym} SPOT ${longLeg.ex.toUpperCase()} ⇄ ${shortLeg.ex.toUpperCase()}`, 'success');
    } catch (e) {
      if (typeof toast === 'function') toast('Spot/Short pair failed: ' + e.message, 'error');
      return;
    }
  } else {
    // Futures L/S pair via the existing /trade/pair/sync helper.
    await _addManualPair(longLeg.sym, longLeg.ex, shortLeg.ex);
    if (typeof toast === 'function') toast(`Paired ${longLeg.sym} ${longLeg.ex.toUpperCase()} ⇄ ${shortLeg.ex.toUpperCase()}`, 'success');
  }
  _syncSelected.clear();
  if (typeof accLoadPositions === 'function') accLoadPositions();
  _renderSyncList();
}

async function _syncUnpair(idx){
  const arr = _loadManualPairs();
  const p = arr[idx];
  if (!p) return;
  await _removeManualPair(p.symbol, p.long_exchange, p.short_exchange);
  if (typeof accLoadPositions === 'function') accLoadPositions();
  _renderSyncList();
}

document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeSyncPairs(); });

// ═══ /ws/positions per-user push channel (Task #4) ══════════════════════
let _posWS = null;
let _posWSReconnectMs = 1500;
function _connectPositionsWS() {
  if (!Auth.isLoggedIn()) return;
  try {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${location.host}/api/screener/ws/positions`;
    _posWS = new WebSocket(url);
    _posWS.onopen = () => {
      _posWS.send(JSON.stringify({ auth: Auth.getToken() }));
      _posWSReconnectMs = 1500;  // reset backoff on success
    };
    _posWS.onmessage = (ev) => {
      try {
        const m = JSON.parse(ev.data);
        if (m.type === 'hello') return;
        // Any push event → refresh dependent panes. Lightweight: backend
        // doesn't ship full state, just nudges us to re-fetch.
        if (m.type === 'refresh' || m.type === 'position_update') {
          accLoadTriggers();
          accLoadPositions();
        }
      } catch {}
    };
    _posWS.onclose = () => {
      _posWS = null;
      // Exponential backoff up to 30s
      setTimeout(_connectPositionsWS, _posWSReconnectMs);
      _posWSReconnectMs = Math.min(_posWSReconnectMs * 2, 30000);
    };
    _posWS.onerror = () => { try { _posWS?.close(); } catch {} };
  } catch (e) {
    console.warn('positions WS failed', e);
  }
}

// ═══ Live Trading unified panel (Task #1+#5) ═══════════════════════════
// State for the new panel. Coexists with the legacy `_trade` object until
// the user opts to fully replace; classic-view toggle persists via
// localStorage.lt_classic.
const LT = {
  mode: 'open',         // open | close
  pairKind: 'long_short', // long_short | spot_short
  margin: 'isolated',
  leverage: 3,
  unit: 'token',        // token (base asset) | usdt (notional)
  longBal:  { total: 0, avail: 0 },
  shortBal: { total: 0, avail: 0 },
  effSpread: null,      // last known effective spread (live)
};

function _ltCurMarkPrice() {
  // Long-leg ask is the price we'd pay; fall back to last known price.
  if (typeof _eeHist !== 'undefined' && Array.isArray(_eeHist) && _eeHist.length) {
    const last = _eeHist[_eeHist.length - 1];
    if (last && last.longAsk) return last.longAsk;
  }
  if (typeof _trade === 'object' && _trade?.long?.last) return _trade.long.last;
  return 0;
}

function ltInit() {
  // Unified panel only — legacy trade-card stays hidden in DOM as
  // fallback. To re-enable user toggle, restore the .lt-classic-link
  // button + the original localStorage-aware branch.
  const lt = document.getElementById('lt-panel');
  const tc = document.getElementById('trade-card');
  if (lt) lt.style.display = '';
  if (tc) tc.style.display = 'none';

  // Auto-derive pair_kind from URL ?type= (long-short | spot-short |
  // dex-short). Spot-short hides leverage on the long leg in sizing
  // math; everything else treats both legs as perp.
  if (typeof TYPE === 'string') {
    LT.pairKind = (TYPE === 'spot') ? 'spot_short' : 'long_short';
  }
  const panel = document.getElementById('lt-panel');
  if (panel) panel.classList.toggle('is-spot-short', LT.pairKind === 'spot_short');

  // Suffix labels
  const sym = (typeof SYM === 'string' ? SYM : 'TOKEN');
  ['lt-portion-suffix','lt-tp-portion-suffix','lt-sl-portion-suffix']
    .forEach(id => { const e = document.getElementById(id); if (e) e.textContent = sym; });
  // Quantity unit toggle: TOKEN button shows the symbol
  const tokBtn = document.getElementById('lt-unit-token');
  if (tokBtn) tokBtn.textContent = sym;
  ltSyncFromTrade();
  ltRefreshBalances();
  // Initialize slider visual fill at 0%
  _ltSetSliderFill(0);
  ltRecalc();
}

// Pull leg labels + wallet ids from the existing _trade state.
// Called from ltInit and from _applyTradeStatus after /trade/status lands.
function ltSyncFromTrade() {
  const longEx  = (typeof LONG  === 'string' ? LONG  : '').toLowerCase();
  const shortEx = (typeof SHORT === 'string' ? SHORT : '').toLowerCase();
  const longLbl  = (typeof EX_LABEL === 'object' && EX_LABEL[longEx])  || longEx.toUpperCase()  || '—';
  const shortLbl = (typeof EX_LABEL === 'object' && EX_LABEL[shortEx]) || shortEx.toUpperCase() || '—';
  const ll = document.getElementById('lt-bal-long-label');
  const sl = document.getElementById('lt-bal-short-label');
  if (ll) ll.textContent = `LONG · ${longLbl}`;
  if (sl) sl.textContent = `SHORT · ${shortLbl}`;
  ltRefreshLeverageOptions();
}

// Re-render the leverage <select> options so it caps at min(long, short)
// max leverage (long_short) or short.max only (spot_short — long leg is
// spot, no leverage). Called from ltSyncFromTrade + ltSetPairKind so the
// cap stays correct when the user toggles pair kind.
function ltRefreshLeverageOptions() {
  const sel = document.getElementById('lt-leverage');
  if (!sel) return;
  const longMax  = (typeof _trade === 'object' && _trade?.long?.maxLeverage)  || 125;
  const shortMax = (typeof _trade === 'object' && _trade?.short?.maxLeverage) || 125;
  const cap = (LT.pairKind === 'spot_short')
    ? shortMax              // spot leg has no leverage; cap by perp leg only
    : Math.min(longMax, shortMax);
  // Standard tier ladder, snapped to cap (anything > cap is dropped).
  const tiers = [1, 2, 3, 5, 10, 20, 50, 100, 125];
  const allowed = tiers.filter(t => t <= cap);
  if (!allowed.length || allowed[allowed.length - 1] !== cap) {
    // Always include the exact cap as the last option, even if not in
    // the standard ladder (e.g. cap=30 for OKX → ..., 20, 30).
    if (!allowed.includes(cap)) allowed.push(cap);
  }
  const cur = parseInt(sel.value, 10) || 3;
  const newCur = Math.min(cur, cap);
  sel.innerHTML = allowed.map(v => `<option value="${v}"${v === newCur ? ' selected' : ''}>${v}x</option>`).join('');
  sel.value = String(newCur);
  // Hint next to the selector — show which venue is the limiting leg
  const lbl = document.getElementById('lt-lev-lbl');
  if (lbl) {
    const baseLbl = (LT.pairKind === 'spot_short') ? 'Leverage (short)' : 'Leverage';
    if (LT.pairKind !== 'spot_short' && longMax !== shortMax) {
      const limiting = (longMax < shortMax) ? (LONG || 'long') : (SHORT || 'short');
      lbl.textContent = `${baseLbl} · max ${cap}x (${limiting})`;
    } else {
      lbl.textContent = `${baseLbl} · max ${cap}x`;
    }
  }
}

function ltToggleClassic() {
  const cur = localStorage.getItem('lt_classic') === '1';
  localStorage.setItem('lt_classic', cur ? '0' : '1');
  ltInit();
}

function ltSwitchMode(mode) {
  LT.mode = mode;
  document.querySelectorAll('.lt-tab').forEach(t => t.classList.toggle('is-active', t.dataset.mode === mode));
  // Submit label tracks mode; keep .is-close styling on close
  const btn = document.getElementById('lt-submit');
  if (btn) {
    btn.classList.toggle('is-close', mode === 'close');
    ltUpdateSubmitLabel();
  }
  // Hide TP/SL on close — they're only created with parent open.
  ['lt-tp-on','lt-sl-on'].forEach(id => {
    const el = document.getElementById(id);
    if (el) {
      const fold = el.closest('.lt-fold');
      if (fold) fold.style.display = mode === 'close' ? 'none' : '';
    }
  });
}

function ltSetPairKind(pk) {
  LT.pairKind = pk;
  document.querySelectorAll('.lt-pk-chip').forEach(c => c.classList.toggle('is-active', c.dataset.pk === pk));
  const panel = document.getElementById('lt-panel');
  if (panel) panel.classList.toggle('is-spot-short', pk === 'spot_short');
  // In spot-short mode, leverage applies only to the short (perp) leg.
  const lev = document.getElementById('lt-leverage');
  if (lev) {
    lev.disabled = false;
    lev.title = (pk === 'spot_short')
      ? 'Spot leg has no leverage; this applies to the short (perp) leg only'
      : '';
  }
  // Re-render leverage options now that pair_kind affects the cap
  ltRefreshLeverageOptions();
  ltRecalc();
}

function ltSetMargin(v) {
  LT.margin = v;
  document.querySelectorAll('#lt-margin-row .lt-seg-btn').forEach(b => b.classList.toggle('is-active', b.dataset.v === v));
  ltRecalc();
}

// Max notional we can put on each leg given balance + leverage. Both
// legs must support the same notional, so the smaller leg-capacity is
// the binding constraint. Returns USDT.
//
//   long_short (both perp)  → min(longBal, shortBal) × leverage
//   spot_short (long=spot)  → min(longBal, shortBal × leverage)
function _ltMaxNotionalUsd() {
  const longAvail  = LT.longBal.avail  || 0;
  const shortAvail = LT.shortBal.avail || 0;
  const lev = parseInt(document.getElementById('lt-leverage')?.value, 10) || 1;
  if (LT.pairKind === 'spot_short') {
    // long leg is spot (no leverage), short leg is perp leveraged
    return Math.min(longAvail, shortAvail * lev);
  }
  // both perp — leverage applies symmetrically
  return Math.min(longAvail, shortAvail) * lev;
}

function ltSetUnit(unit) {
  // Re-interpret the current input value into the new unit so the typed
  // qty stays equivalent. e.g. 100 TOKEN @ $1.50 → 150 USDT after switch.
  const prevUnit = LT.unit;
  if (prevUnit === unit) return;
  const cur = parseFloat(document.getElementById('lt-qty')?.value) || 0;
  const mark = _ltCurMarkPrice();
  let next = cur;
  if (mark > 0 && cur > 0) {
    if (prevUnit === 'token' && unit === 'usdt')   next = cur * mark;
    if (prevUnit === 'usdt'  && unit === 'token')  next = cur / mark;
  }
  LT.unit = unit;
  document.querySelectorAll('.lt-unit-btn').forEach(b => b.classList.toggle('is-active', b.dataset.unit === unit));
  const qtyInput = document.getElementById('lt-qty');
  if (qtyInput) qtyInput.value = next ? next.toFixed(unit === 'usdt' ? 2 : 6) : '';
  ltOnQtyInput();
}

function _ltSetSliderFill(pct) {
  // Set the CSS var that drives the green-fill track gradient. CSS reads
  // --lt-alloc-pct in linear-gradient stops to draw filled-up-to-thumb.
  const slider = document.getElementById('lt-alloc-slider');
  if (slider) slider.style.setProperty('--lt-alloc-pct', `${pct}%`);
  const lbl = document.getElementById('lt-alloc-pct');
  if (lbl) lbl.textContent = `${pct}%`;
}

// Read the qty input as a USD notional, regardless of which unit
// (token / usdt) is currently selected. Backend always wants tokens
// internally — this is the single conversion point.
function _ltInputNotionalUsd() {
  const v = parseFloat(document.getElementById('lt-qty')?.value) || 0;
  if (LT.unit === 'usdt') return v;
  const mark = _ltCurMarkPrice();
  return mark > 0 ? v * mark : 0;
}

// Convert a target notional (USD) into the value to display in the qty
// input given the active unit.
function _ltUsdToInputValue(usd) {
  if (LT.unit === 'usdt') return usd.toFixed(2);
  const mark = _ltCurMarkPrice();
  return mark > 0 ? (usd / mark).toFixed(6) : '0';
}

function ltOnAlloc(e) {
  const pct = parseInt(e.target.value, 10) || 0;
  _ltSetSliderFill(pct);
  const cap = _ltMaxNotionalUsd();
  if (cap > 0) {
    const usd = cap * (pct / 100);
    const qtyInput = document.getElementById('lt-qty');
    if (qtyInput) qtyInput.value = _ltUsdToInputValue(usd);
  }
  ltRecalc();
}

function ltOnQtyInput() {
  // User edited qty directly — sync slider back. Convert to USD for the
  // % calculation regardless of active unit.
  const usd = _ltInputNotionalUsd();
  const cap = _ltMaxNotionalUsd();
  if (cap > 0) {
    const pct = Math.min(100, Math.max(0, Math.round((usd / cap) * 100)));
    const slider = document.getElementById('lt-alloc-slider');
    if (slider) slider.value = pct;
    _ltSetSliderFill(pct);
  }
  ltRecalc();
  if (typeof ltRefreshQtyHint === 'function') ltRefreshQtyHint();
}

// ── Qty limits hint + client-side validation ────────────────────────
function _ltCurTokenQty() {
  // Always returns a TOKEN-unit qty regardless of LT.unit setting,
  // for feeding into venue-spec validators (min/step in coin units).
  const v = parseFloat(document.getElementById('lt-qty')?.value) || 0;
  if (LT.unit === 'token') return v;
  const mark = _ltCurMarkPrice();
  return mark > 0 ? v / mark : 0;
}

function _ltFmtQty(q) {
  if (q == null || !isFinite(q) || q <= 0) return '—';
  // Pick a sensible precision per magnitude
  if (q >= 1)        return q.toFixed(3);
  if (q >= 0.01)     return q.toFixed(4);
  if (q >= 0.0001)   return q.toFixed(6);
  return q.toExponential(2);
}

function ltRefreshQtyHint() {
  const el = document.getElementById('lt-qty-hint');
  if (!el) return;
  const sym = (typeof SYM === 'string' ? SYM : '');
  const legs = [
    { side: 'long',  ex: (typeof LONG  === 'string' ? LONG  : ''), q: _trade?.long?.qtyLimits  },
    { side: 'short', ex: (typeof SHORT === 'string' ? SHORT : ''), q: _trade?.short?.qtyLimits },
  ];
  // In spot_short long leg sometimes has different specs — for now we
  // surface what the perp-side adapter reports; spot venue qty hint is
  // a follow-up.
  const cur = _ltCurTokenQty();
  const parts = legs.map(({ side, ex, q }) => {
    const exLbl = (typeof EX_LABEL === 'object' && EX_LABEL[ex]) || (ex || '').toUpperCase();
    if (!q) {
      // No public qty-limit data from this adapter — show honest dash
      // rather than hiding (user explicitly asked: "ставь прочерк но
      // точно не пиши вранье").
      return `<span class="h-leg" title="${side.toUpperCase()} · ${exLbl} · venue specs not exposed by adapter — runtime preflight will catch sub-min orders">
        <span class="ex">${side[0].toUpperCase()}·${exLbl}</span>min — · step —
      </span>`;
    }
    const min = q.min_qty || 0;
    const step = q.step;
    const bad = (cur > 0 && min > 0 && cur < min);
    const minTxt = min > 0 ? `min ${_ltFmtQty(min)} ${sym}` : 'min —';
    const stepTxt = step ? ` · step ${_ltFmtQty(step)}` : ' · step —';
    const minNotionalTxt = q.min_notional > 0 ? ` · min ${q.min_notional} USDT` : '';
    return `<span class="h-leg${bad ? ' bad' : ''}" title="${side.toUpperCase()} · ${exLbl}">
      <span class="ex">${side[0].toUpperCase()}·${exLbl}</span>${minTxt}${stepTxt}${minNotionalTxt}
    </span>`;
  });
  // Round-up button if step is known and current qty is misaligned
  let roundBtn = '';
  if (cur > 0) {
    const steps = legs.map(l => l.q?.step).filter(Boolean);
    const minStep = steps.length ? Math.min(...steps) : 0;
    const mins = legs.map(l => l.q?.min_qty || 0);
    const maxMin = Math.max(...mins, 0);
    if (minStep > 0) {
      const aligned = Math.max(maxMin || minStep, Math.ceil(cur / minStep) * minStep);
      // Only show if rounding actually changes something
      if (Math.abs(aligned - cur) > 1e-9) {
        roundBtn = `<button type="button" class="h-round-btn" onclick="ltRoundQtyToStep()">↑ ${_ltFmtQty(aligned)}</button>`;
      }
    }
  }
  el.innerHTML = parts.join('') + roundBtn;
}

function ltRoundQtyToStep() {
  const limits = [_trade?.long?.qtyLimits, _trade?.short?.qtyLimits].filter(Boolean);
  if (!limits.length) return;
  const minStep = Math.min(...limits.map(l => l.step || Infinity));
  const minQty  = Math.max(...limits.map(l => l.min_qty || 0));
  if (!isFinite(minStep) || minStep <= 0) return;
  const cur = _ltCurTokenQty();
  if (cur <= 0) return;
  const aligned = Math.max(minQty, Math.ceil(cur / minStep) * minStep);
  // Write back in active unit
  const input = document.getElementById('lt-qty');
  if (LT.unit === 'usdt') {
    const mark = _ltCurMarkPrice();
    input.value = mark > 0 ? (aligned * mark).toFixed(2) : aligned.toFixed(6);
  } else {
    input.value = aligned.toFixed(6);
  }
  ltOnQtyInput();
}

function ltOnQtyBlur() {
  // Subtle auto-snap on blur — only if user typed something obviously
  // sub-min (would be rejected anyway). Avoid surprise rewrites.
  const cur = _ltCurTokenQty();
  if (cur <= 0) return;
  const limits = [_trade?.long?.qtyLimits, _trade?.short?.qtyLimits].filter(Boolean);
  if (!limits.length) return;
  const maxMin = Math.max(...limits.map(l => l.min_qty || 0));
  if (maxMin > 0 && cur < maxMin) ltRoundQtyToStep();
}

function ltOnPortionToggle(e) {
  const open = e.target.checked;
  const fold = e.target.closest('.lt-fold');
  if (fold) fold.open = open;
}
function ltOnTpToggle(e) { e.target.closest('.lt-fold').open = e.target.checked; }
function ltOnSlToggle(e) { e.target.closest('.lt-fold').open = e.target.checked; }
function ltOnScheduleToggle(e) {
  document.getElementById('lt-schedule').style.display = e.target.checked ? '' : 'none';
}

function ltRecalc() {
  const mark = _ltCurMarkPrice();
  const leverage = LT.pairKind === 'spot_short' ? 1 : (parseInt(document.getElementById('lt-leverage')?.value, 10) || 1);
  // Position value = USD notional regardless of input unit
  const posValue = _ltInputNotionalUsd();
  const margin = leverage > 0 ? posValue / leverage : posValue;
  const setText = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
  setText('lt-pos-value', `${posValue.toFixed(2)} USDT`);
  setText('lt-margin-used', `${margin.toFixed(2)} USDT`);
  // Effective spread @ size — reuse arb.html's _liveBasisPct as a baseline.
  const eff = (typeof _liveBasisPct === 'function') ? _liveBasisPct() : null;
  LT.effSpread = eff;
  setText('lt-eff-spread', (eff == null) ? '—' : `${eff.toFixed(3)}%`);
  // Portion cost (one chunk × mark)
  const portion = parseFloat(document.getElementById('lt-portion')?.value) || 0;
  setText('lt-portion-cost', `${(portion * mark).toFixed(2)} USDT`);
  ltUpdateSubmitLabel();
}

function ltUpdateSubmitLabel() {
  const btn = document.getElementById('lt-submit');
  if (!btn) return;
  const hasTrig = (document.getElementById('lt-trig')?.value || '').trim() !== '';
  // If either leg lacks a screener key, the submit can't succeed —
  // disable the button and surface the reason as the label.
  const longBad  = (_trade?.long?.keyStatus  || 'missing') !== 'ok';
  const shortBad = (_trade?.short?.keyStatus || 'missing') !== 'ok';
  if (longBad || shortBad) {
    btn.disabled = true;
    btn.title = 'Add a screener-purpose key on the missing leg before placing trades.';
    if (longBad && shortBad) btn.textContent = 'Add keys to both legs';
    else if (longBad)        btn.textContent = 'Add LONG key';
    else                     btn.textContent = 'Add SHORT key';
    return;
  }
  btn.disabled = false;
  btn.title = '';
  if (LT.mode === 'close') btn.textContent = hasTrig ? 'Place Close Trigger' : 'Close Now';
  else                     btn.textContent = hasTrig ? 'Place Trigger'       : 'Open Now';
}

function ltCheckImmediate() {
  ltUpdateSubmitLabel();
  const trig = parseFloat(document.getElementById('lt-trig').value);
  const eff = LT.effSpread;
  const warn = document.getElementById('lt-warn');
  if (!warn) return;
  if (Number.isFinite(trig) && Number.isFinite(eff)) {
    let met = false;
    if (LT.mode === 'open') met = eff >= trig;
    else                    met = eff <= trig;
    warn.textContent = met
      ? `current effective spread is ${eff.toFixed(3)}% — trigger would fire next tick`
      : '';
    warn.style.display = met ? '' : 'none';
  } else {
    warn.style.display = 'none';
  }
}

// ── Keys / account picker ────────────────────────────────────────────
// Per-leg account override stored in localStorage so the choice
// survives page reloads. Key shape: lt_wallet_<long|short>_<symbol>
// Override is only applied when the user has multiple wallets on the
// leg's exchange — otherwise _trade.<side>.walletId stays as-is.
function _ltSavedWalletId(side) {
  const sym = (typeof SYM === 'string' ? SYM : '');
  try {
    const v = localStorage.getItem(`lt_wallet_${side}_${sym}`);
    return v ? parseInt(v, 10) : null;
  } catch { return null; }
}
function _ltSaveWalletId(side, wid) {
  const sym = (typeof SYM === 'string' ? SYM : '');
  try { localStorage.setItem(`lt_wallet_${side}_${sym}`, String(wid)); } catch {}
}

// Session-level caches — /wallets is essentially static within a tab,
// /balances has 30s TTL server-side, so cache here too. Cold open of
// the Keys picker now shows the list within ~50ms (just /wallets); the
// balances trickle in async and update rows when ready.
let _ltWalletsCache = null;       // Promise | array
let _ltBalancesCache = null;      // { ts, byId }
const _LT_BAL_TTL_MS = 25_000;    // a hair under server's 30s cache

async function _ltGetWallets() {
  if (_ltWalletsCache) return _ltWalletsCache;
  _ltWalletsCache = Auth.apiFetch('/wallets').then(r => r.ok ? r.json() : []).catch(() => []);
  return _ltWalletsCache;
}

async function _ltGetBalances({ force = false } = {}) {
  const now = Date.now();
  if (!force && _ltBalancesCache && (now - _ltBalancesCache.ts) < _LT_BAL_TTL_MS) {
    return _ltBalancesCache.byId;
  }
  try {
    const r = await Auth.apiFetch('/trade/balances');
    if (!r.ok) return {};
    const rows = await r.json();
    const byId = Object.fromEntries(rows.map(b => [b.wallet_id, b]));
    _ltBalancesCache = { ts: now, byId };
    return byId;
  } catch { return {}; }
}

function _ltRenderKeysRows(side, candidates, balances) {
  const curWid = (_trade?.[side]?.walletId) || null;
  return candidates.map(w => {
    const b = balances[w.id] || {};
    const bal = (b.balance_usdt != null) ? b.balance_usdt.toFixed(2) + ' USDT' : '<span style="color:var(--text3)">—</span>';
    const res = (b.reserved_usdt != null && b.reserved_usdt > 0) ? `${b.reserved_usdt.toFixed(0)} locked` : '';
    const avail = (b.available_usdt != null) ? b.available_usdt.toFixed(2) + ' avail' : '';
    const isActive = w.id === curWid;
    const nameJSON = JSON.stringify(w.name).replace(/"/g, '&quot;');
    return `
      <div class="lt-keys-row${isActive ? ' is-active' : ''}" data-wid="${w.id}" onclick="ltSelectKey('${side}', ${w.id}, ${nameJSON})">
        <div class="lt-keys-row-top">
          <span class="lt-keys-row-name">${_htmlEsc(w.name)}</span>
          <span class="lt-keys-row-bal">${bal}</span>
        </div>
        <div class="lt-keys-row-meta">
          <span>${_htmlEsc(w.display_info || '')}</span>
          ${avail ? `<span style="color:var(--text2)">${avail}</span>` : ''}
          ${res ? `<span style="color:var(--yellow)">${res}</span>` : ''}
        </div>
      </div>`;
  }).join('');
}

async function ltOpenKeys(side) {
  const pop = document.getElementById('lt-keys-pop');
  const title = document.getElementById('lt-keys-title');
  const body = document.getElementById('lt-keys-body');
  if (!pop || !body) return;
  const ex = (side === 'long' ? LONG : SHORT) || '';
  title.textContent = `Select ${side.toUpperCase()} account · ${(EX_LABEL && EX_LABEL[ex]) || ex.toUpperCase()}`;
  pop.classList.remove('hidden');
  body.innerHTML = '<div class="lt-keys-empty"><span class="spinner"></span> Loading accounts…</div>';

  // 1) Render the wallet list IMMEDIATELY from cached/fast /wallets.
  //    Balances start as "—" so the user sees the list with names
  //    instantly, then balances fill in asynchronously.
  const wallets = await _ltGetWallets();
  const candidates = wallets.filter(w =>
    !w.is_archived &&
    (w.type_value || '').toLowerCase() === ex.toLowerCase() &&
    (w.purpose === 'screener' || w.purpose === 'both')
  );
  if (candidates.length === 0) {
    body.innerHTML = `<div class="lt-keys-empty">No screener-eligible key on ${ex.toUpperCase()}.<br>Add one in <a href="/portfolio#wallets" target="_blank" style="color:var(--green)">Portfolio</a>.</div>`;
    return;
  }

  // Fast paint with whatever balances we have cached (possibly empty).
  const cached = (_ltBalancesCache && (Date.now() - _ltBalancesCache.ts) < _LT_BAL_TTL_MS)
    ? _ltBalancesCache.byId : {};
  body.innerHTML = _ltRenderKeysRows(side, candidates, cached);

  // 2) Background-refresh balances and update the rows in place when
  //    they arrive. If user already clicked a row by then the popup
  //    is closed; harmless update.
  _ltGetBalances({ force: !_ltBalancesCache }).then(balances => {
    if (pop.classList.contains('hidden')) return;
    body.innerHTML = _ltRenderKeysRows(side, candidates, balances);
  });
}

function ltCloseKeys() {
  document.getElementById('lt-keys-pop')?.classList.add('hidden');
}

async function ltSelectKey(side, wid, name) {
  // Persist + apply. We update the local _trade state directly and
  // refresh balances without re-fetching /trade/status (since that
  // endpoint always picks via _find_wallet — backend doesn't yet
  // accept a wallet-id override). Instead we read /trade/balances
  // for the selected wallet's numbers and stamp them into _trade.
  _ltSaveWalletId(side, wid);
  if (typeof _trade === 'object' && _trade?.[side]) {
    _trade[side].walletId = wid;
    try {
      const r = await Auth.apiFetch('/trade/balances');
      if (r.ok) {
        const rows = await r.json();
        const bal = rows.find(b => b.wallet_id === wid);
        if (bal) {
          _trade[side].balance   = bal.balance_usdt   || 0;
          _trade[side].available = (bal.available_usdt != null) ? bal.available_usdt : (bal.balance_usdt || 0);
          _trade[side].reserved  = bal.reserved_usdt  || 0;
        }
      }
    } catch {}
  }
  // Update the leg's account-name line + close
  const lbl = document.getElementById(`lt-bal-${side}-name`);
  if (lbl) lbl.textContent = name || `wallet #${wid}`;
  ltRefreshBalances();
  ltRecalc();
  ltCloseKeys();
  toast(`Selected ${side.toUpperCase()} account: ${name}`, 'success');
}

async function ltRefreshBalances() {
  // Pull from _trade (populated by /api/trade/status). Wallet = raw
  // exchange balance; Available = balance − reservations from active
  // open-triggers. Slider sizing uses Available (LT.*Bal.avail).
  if (typeof _trade !== 'object' || !_trade) return;
  const longBal   = _trade.long?.balance   || 0;
  const shortBal  = _trade.short?.balance  || 0;
  const longAvail = (_trade.long?.available  != null) ? _trade.long.available  : longBal;
  const shortAvail= (_trade.short?.available != null) ? _trade.short.available : shortBal;
  LT.longBal  = { total: longBal,  avail: longAvail };
  LT.shortBal = { total: shortBal, avail: shortAvail };
  const setT = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
  setT('lt-bal-long-total',  `${longBal.toFixed(2)} USDT`);
  setT('lt-bal-long-avail',  `${longAvail.toFixed(2)} USDT`);
  setT('lt-bal-short-total', `${shortBal.toFixed(2)} USDT`);
  setT('lt-bal-short-avail', `${shortAvail.toFixed(2)} USDT`);
  // Show reservation hint inline if any locked capital exists
  const longRes  = _trade.long?.reserved  || 0;
  const shortRes = _trade.short?.reserved || 0;
  const longLbl  = document.getElementById('lt-bal-long-label');
  const shortLbl = document.getElementById('lt-bal-short-label');
  if (longLbl) {
    const ex = (typeof LONG === 'string') ? (typeof EX_LABEL === 'object' && EX_LABEL[LONG] ? EX_LABEL[LONG] : LONG.toUpperCase()) : '—';
    longLbl.innerHTML = `LONG · ${ex}` + (longRes > 0 ? ` <span style="color:var(--yellow);font-size:9px">· ${longRes.toFixed(0)} USDT locked</span>` : '');
  }
  if (shortLbl) {
    const ex = (typeof SHORT === 'string') ? (typeof EX_LABEL === 'object' && EX_LABEL[SHORT] ? EX_LABEL[SHORT] : SHORT.toUpperCase()) : '—';
    shortLbl.innerHTML = `SHORT · ${ex}` + (shortRes > 0 ? ` <span style="color:var(--yellow);font-size:9px">· ${shortRes.toFixed(0)} USDT locked</span>` : '');
  }
  // Per-leg status indicator: distinguishes 4 cases visually
  //   ok           → real balance numbers (default render above)
  //   missing      → "No trade key" + "Add key →" link in Available row
  //   disabled     → "Trade disabled" (admin or user toggle)
  //   admin_blocked→ "Trading paused by admin"
  //   balance_error→ honest dash (already wired)
  ['long','short'].forEach(s => {
    const totalEl = document.getElementById(`lt-bal-${s}-total`);
    const availEl = document.getElementById(`lt-bal-${s}-avail`);
    const nameEl  = document.getElementById(`lt-bal-${s}-name`);
    const status = _trade[s]?.keyStatus || 'missing';
    const err = _trade[s]?.balanceError;
    if (err) {
      if (totalEl) { totalEl.textContent = '—'; totalEl.title = err; totalEl.style.color = 'var(--text3)'; }
      if (availEl) { availEl.textContent = '—'; availEl.title = err; availEl.style.color = 'var(--text3)'; }
      return;
    }
    if (status === 'missing') {
      if (nameEl)  { nameEl.textContent = 'No trade key';  nameEl.style.color = 'var(--red)'; }
      if (totalEl) { totalEl.innerHTML = '<span style="color:var(--text3)">no key</span>'; totalEl.title = 'Add a screener-purpose key for this exchange in /portfolio'; }
      if (availEl) { availEl.innerHTML = '<a href="/portfolio#wallets" target="_blank" style="color:var(--green);font-size:11px">+ Add key</a>'; availEl.title = ''; }
      return;
    }
    if (status === 'disabled') {
      if (nameEl)  { nameEl.textContent = 'Trade disabled'; nameEl.style.color = 'var(--yellow)'; }
      if (totalEl) { totalEl.innerHTML = '<span style="color:var(--yellow);font-size:11px">trade disabled</span>'; }
      if (availEl) { availEl.innerHTML = '<a href="/portfolio#wallets" target="_blank" style="color:var(--green);font-size:11px">re-enable →</a>'; }
      return;
    }
    if (status === 'admin_blocked') {
      if (nameEl)  { nameEl.textContent = 'Paused by admin'; nameEl.style.color = 'var(--yellow)'; }
      if (totalEl) { totalEl.innerHTML = '<span style="color:var(--yellow);font-size:11px">trading paused</span>'; }
      if (availEl) { availEl.textContent = '—'; }
      return;
    }
    // status === 'ok' → leave the numeric render from above; clear any
    // stale styling that might be left from a previous status flip.
    if (totalEl) { totalEl.title = ''; totalEl.style.color = ''; }
    if (availEl) { availEl.title = ''; availEl.style.color = ''; }
    if (nameEl)  { nameEl.style.color = 'var(--text3)'; }
  });
  // Default account-name display when no override has stamped it yet
  const longNameEl  = document.getElementById('lt-bal-long-name');
  const shortNameEl = document.getElementById('lt-bal-short-name');
  if (longNameEl  && longNameEl.textContent  === '—' && _trade.long?.walletId)  longNameEl.textContent  = `wallet #${_trade.long.walletId}`;
  if (shortNameEl && shortNameEl.textContent === '—' && _trade.short?.walletId) shortNameEl.textContent = `wallet #${_trade.short.walletId}`;
}

// Show a structured submit error as a top-right toast. Maps backend
// error codes (qty_validation_failed / insufficient_balance / trigger_
// limit_exceeded / tp_already_exists) into human-readable title + sub.
function _ltShowSubmitError(httpStatus, body) {
  const d = (body && body.detail) || {};
  const code = (typeof d === 'object' && d.error) || (typeof d === 'string' ? d : null);
  const exLbl = ex => (typeof EX_LABEL === 'object' && EX_LABEL[ex]) || (ex || '').toUpperCase();

  let title = 'Trigger rejected';
  let sub = '';

  if (code === 'qty_validation_failed') {
    title = 'Quantity below minimum';
    sub = `${(d.leg || '').toUpperCase()} · ${exLbl(d.exchange)} · ${d.reason || 'invalid qty'}`;
  } else if (code === 'insufficient_balance') {
    title = 'Insufficient balance';
    sub = `${(d.leg || '').toUpperCase()} · ${exLbl(d.exchange)} · need ${d.required_usdt} USDT, have ${d.available_usdt} USDT (${d.balance_usdt} − ${d.reserved_usdt} locked)`;
  } else if (code === 'trigger_limit_exceeded') {
    title = 'Trigger limit reached';
    sub = `Plan caps at ${d.limit} active triggers — cancel one or upgrade.`;
  } else if (code === 'tp_already_exists' || code === 'sl_already_exists') {
    title = code === 'tp_already_exists' ? 'TP already exists' : 'SL already exists';
    sub = 'PATCH or DELETE the existing one first.';
  } else if (httpStatus === 422 && typeof d === 'object' && d.message) {
    title = 'Validation error';
    sub = d.message;
  } else if (typeof d === 'string') {
    title = 'Trigger rejected';
    sub = d;
  } else if (d && d.message) {
    title = 'Trigger rejected';
    sub = d.message;
  } else {
    sub = `HTTP ${httpStatus}`;
  }
  toast(title, 'error', sub);
}

async function ltSubmit(force = false) {
  const errEl = document.getElementById('lt-err');
  const setErr = m => { if (!errEl) return; if (m) { errEl.textContent = m; errEl.style.display = ''; } else { errEl.style.display = 'none'; } };
  setErr(null);

  if (LT.mode === 'close') {
    setErr('Close mode: select an open arb position from the Positions tab and click "Close at market" — full close-trigger flow lands in v1.1.');
    return;
  }

  const L = (typeof _trade === 'object' && _trade) ? _trade.long  : null;
  const S = (typeof _trade === 'object' && _trade) ? _trade.short : null;
  if (!L || !S || !L.walletId || !S.walletId) {
    setErr('Add screener trade keys for both exchanges first.');
    return;
  }

  // Convert input to token qty regardless of selected unit. Backend
  // wants total_qty_token always in base-asset units.
  const inputVal = parseFloat(document.getElementById('lt-qty').value);
  if (!Number.isFinite(inputVal) || inputVal <= 0) {
    setErr('Enter total qty');
    return;
  }
  const _mark = _ltCurMarkPrice();
  const totalQty = (LT.unit === 'usdt')
    ? (_mark > 0 ? inputVal / _mark : 0)
    : inputVal;
  if (!Number.isFinite(totalQty) || totalQty <= 0) {
    setErr(LT.unit === 'usdt' ? 'Mark price unavailable, switch to TOKEN' : 'Enter total qty');
    return;
  }
  const trigVal = (document.getElementById('lt-trig').value || '').trim();
  const portionOn = document.getElementById('lt-portion-on').checked;
  const portion = portionOn ? parseFloat(document.getElementById('lt-portion').value) : null;
  const tpOn = document.getElementById('lt-tp-on').checked;
  const slOn = document.getElementById('lt-sl-on').checked;
  const tpVal = tpOn ? parseFloat(document.getElementById('lt-tp').value) : null;
  const slVal = slOn ? parseFloat(document.getElementById('lt-sl').value) : null;
  const tpPortion = tpOn ? (parseFloat(document.getElementById('lt-tp-portion').value) || null) : null;
  const slPortion = slOn ? (parseFloat(document.getElementById('lt-sl-portion').value) || null) : null;
  const infinite = document.getElementById('lt-infinite').checked;
  const reduceOnly = document.getElementById('lt-reduce').checked;
  const scheduleOn = document.getElementById('lt-schedule-on').checked;
  const scheduleVal = scheduleOn ? document.getElementById('lt-schedule').value : null;

  if (portionOn && (!Number.isFinite(portion) || portion <= 0)) { setErr('Enter portion size'); return; }
  if (tpOn && !Number.isFinite(tpVal)) { setErr('Enter TP spread'); return; }
  if (slOn && !Number.isFinite(slVal)) { setErr('Enter SL spread'); return; }
  if (infinite && !portionOn) { setErr('Infinite Fill requires Portion Size'); return; }

  const body = {
    kind: 'open',
    pair_kind: LT.pairKind,
    long_exchange:  (typeof LONG  === 'string' ? LONG  : ''),
    long_symbol:    SYM,
    long_wallet_id: L.walletId,
    short_exchange: (typeof SHORT === 'string' ? SHORT : ''),
    short_symbol:   SYM,
    short_wallet_id: S.walletId,
    trigger_spread_pct: trigVal === '' ? null : parseFloat(trigVal),
    total_qty_token: totalQty,
    portion_size_token: portionOn ? portion : null,
    infinite_fill: infinite,
    leverage: parseInt(document.getElementById('lt-leverage').value, 10) || 1,
    margin_mode: LT.margin,
    reduce_only: reduceOnly,
    activate_at: scheduleVal ? new Date(scheduleVal).toISOString() : null,
    force,
  };
  if (tpOn) body.tp = { trigger_spread_pct: tpVal, portion_size_token: tpPortion };
  if (slOn) body.sl = { trigger_spread_pct: slVal, portion_size_token: slPortion };

  const submitBtn = document.getElementById('lt-submit');
  if (submitBtn) submitBtn.disabled = true;
  try {
    const r = await Auth.apiFetch('/trade/arb-orders', {
      method: 'POST', body: JSON.stringify(body),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      _ltShowSubmitError(r.status, j);
      return;
    }
    if (j.warning === 'immediate_execution' && !force) {
      const ok = await Confirm.ask({
        title: 'Fire immediately?',
        text: `Current effective spread is ${j.current_spread}% which already meets your ${j.kind} target of ${j.requested_trigger}%. The trigger will fire on the next tick.`,
        okText: 'Place anyway',
        okVariant: 'primary',
      });
      if (ok) await ltSubmit(true);
      return;
    }
    toast('Trigger placed', 'success');
    accLoadTriggers();
    // Reset form
    document.getElementById('lt-qty').value = '';
    document.getElementById('lt-alloc-slider').value = 0;
    _ltSetSliderFill(0);
    ltRecalc();
  } catch (e) {
    setErr(e.message || 'Network error');
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
}

// Initialise on page load
document.addEventListener('DOMContentLoaded', () => { try { ltInit(); } catch {} });
// Re-recalculate when live spread updates
setInterval(() => { if (document.hidden) return; try { ltRecalc(); } catch {} }, 1500);

// ═══ Trigger orders + Sync arb-positions (Task 7) ═══════════════════════
async function accLoadTriggers() {
  try {
    const r = await Auth.apiFetch('/trade/arb-orders');
    if (!r.ok) return;
    const rows = await r.json();
    const tbody = document.getElementById('acc-triggers-body');
    const empty = document.getElementById('acc-triggers-empty');
    const cnt = document.getElementById('acc-cnt-triggers');
    if (!tbody) return;
    if (cnt) {
      cnt.textContent = rows.length;
      // Highlight count badge when there are active triggers
      cnt.style.color = rows.length > 0 ? 'var(--green)' : '';
      cnt.style.background = rows.length > 0 ? 'var(--green-soft)' : '';
      cnt.style.borderColor = rows.length > 0 ? 'var(--green-edge)' : '';
    }
    if (rows.length === 0) {
      tbody.innerHTML = '';
      if (empty) empty.style.display = '';
      return;
    }
    if (empty) empty.style.display = 'none';
    const fmtSpread = v => v == null ? '<span style="color:var(--text3)">market</span>' : (v >= 0 ? '+' : '') + v.toFixed(3) + '%';
    const fmtFilled = r => r.infinite_fill ? `${r.portions_filled} / ∞` : `${r.portions_filled} / ${r.portions_target ?? 1}`;
    const fmtMode = r => {
      const tags = [];
      if (r.portion_size_token) tags.push(`portion ${r.portion_size_token}`);
      if (r.infinite_fill) tags.push('∞');
      if (r.reduce_only) tags.push('RO');
      return tags.length ? tags.join(', ') : '—';
    };
    const fmtStatus = s => {
      const css = { pending:'#7CB9F7', firing:'#E5C07B', fired:'#1AFFAB', failed:'#F87171', cancelled:'#888', scheduled:'#888' }[s] || '#888';
      return `<span style="color:${css};font-weight:600;text-transform:uppercase;font-size:10px">${s}</span>`;
    };
    const fmtTime = t => {
      if (!t) return '—';
      const d = new Date(t);
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    };
    tbody.innerHTML = rows.map(r => `
      <tr ${r.error_message ? `title="${_htmlEsc(r.error_message)}"` : ''}>
        <td><span style="text-transform:capitalize">${r.kind}</span></td>
        <td class="mono">${(r.long_symbol || '—').toUpperCase()}</td>
        <td><span class="acc-mini-ex">${r.long_exchange || '—'} → ${r.short_exchange || '—'}</span></td>
        <td class="num">${fmtSpread(r.trigger_spread_pct)}</td>
        <td class="num">${fmtFilled(r)}</td>
        <td class="num">${r.total_qty_token ?? '—'}</td>
        <td><span style="color:var(--text3);font-size:10px">${fmtMode(r)}</span></td>
        <td>${fmtStatus(r.status)}</td>
        <td>${fmtTime(r.created_at)}</td>
        <td>
          ${r.status === 'pending' || r.status === 'scheduled' ?
            `<button type="button" class="btn-x" onclick="trigCancel(${r.id})" title="Cancel trigger">✕</button>` : ''}
        </td>
      </tr>
      ${r.error_message ? `
      <tr class="trig-err-row">
        <td colspan="10" style="padding:6px 14px;background:var(--red-soft);border-top:1px solid var(--red-edge);font-size:11px;color:var(--red);line-height:1.4">
          <strong>${(r.error_kind || 'error').toUpperCase()}:</strong> ${_htmlEsc(r.error_message)}
        </td>
      </tr>` : ''}
    `).join('');
  } catch (e) {
    console.warn('triggers load failed', e);
  }
}

async function trigCancel(id) {
  if (!await Confirm.ask({ title: 'Cancel trigger?', text: 'This will also cancel any linked TP/SL.', okText: 'Cancel trigger', okVariant: 'danger' })) return;
  try {
    const r = await Auth.apiFetch('/trade/arb-orders/' + id, { method: 'DELETE' });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      toast(j.detail || 'Cancel failed', 'error');
      return;
    }
    toast('Trigger cancelled', 'success');
    accLoadTriggers();
  } catch {
    toast('Cancel failed', 'error');
  }
}

async function syncArbPositions() {
  const btn = document.getElementById('acc-sync-btn');
  if (btn) { btn.disabled = true; btn.style.opacity = '0.6'; }
  try {
    const r = await Auth.apiFetch('/trade/arb-positions/sync', { method: 'POST' });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      toast(j.detail || 'Sync failed', 'error');
    } else if (j.count === 0) {
      toast('No new pairs to sync', 'info', 'Open positions on opposite venues will be auto-detected when both legs match (±12% notional, ±10 min)');
    } else {
      toast(`Synced ${j.count} pair${j.count > 1 ? 's' : ''}`, 'success');
      accLoadPositions();
    }
  } catch {
    toast('Sync failed', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.style.opacity = ''; }
  }
}

// ── Trigger card form: wire toggles + submit ────────────────────────────
(() => {
  const togglers = [
    ['trig-portion-on', 'trig-portion-wrap'],
    ['trig-tp-on',      'trig-tp-wrap'],
    ['trig-sl-on',      'trig-sl-wrap'],
    ['trig-schedule-on', 'trig-schedule'],
  ];
  togglers.forEach(([cb, wrap]) => {
    const el = document.getElementById(cb);
    const w = document.getElementById(wrap);
    if (!el || !w) return;
    el.addEventListener('change', () => { w.style.display = el.checked ? '' : 'none'; });
  });
  // Suffix labels (token name) for qty inputs
  const sym = (typeof SYM === 'string' ? SYM : '');
  ['trig-qty-suffix', 'trig-portion-suffix'].forEach(id => {
    const e = document.getElementById(id);
    if (e) e.textContent = sym || 'TOKEN';
  });

  // Live spread → warn if trigger is already met (cosmetic; backend
  // re-checks authoritatively on POST)
  const spreadInput = document.getElementById('trig-spread');
  const spreadWarn  = document.getElementById('trig-warn-spread');
  if (spreadInput && spreadWarn) {
    spreadInput.addEventListener('input', () => {
      const v = parseFloat(spreadInput.value);
      const cur = (typeof _liveBasisPct === 'function') ? _liveBasisPct() : null;
      if (Number.isFinite(v) && Number.isFinite(cur) && v <= cur) {
        spreadWarn.textContent = `current spread is ${cur.toFixed(3)}% — would fire next tick`;
        spreadWarn.style.display = '';
      } else {
        spreadWarn.style.display = 'none';
      }
    });
  }
})();

async function trigSubmit(force = false) {
  const errEl = document.getElementById('trig-err');
  const setErr = m => { if (!errEl) return; if (m) { errEl.textContent = m; errEl.style.display = ''; } else { errEl.style.display = 'none'; } };
  setErr(null);

  const L = (typeof _trade === 'object' && _trade) ? _trade.long  : null;
  const S = (typeof _trade === 'object' && _trade) ? _trade.short : null;
  if (!L || !S || !L.walletId || !S.walletId) {
    setErr('Add screener trade keys for both exchanges first.');
    return;
  }

  const totalQty = parseFloat(document.getElementById('trig-qty').value);
  if (!Number.isFinite(totalQty) || totalQty <= 0) {
    setErr('Enter total qty');
    return;
  }
  const trigSpread = document.getElementById('trig-spread').value.trim();
  const portionOn = document.getElementById('trig-portion-on').checked;
  const portion = portionOn ? parseFloat(document.getElementById('trig-portion').value) : null;
  const tpOn = document.getElementById('trig-tp-on').checked;
  const tp = tpOn ? parseFloat(document.getElementById('trig-tp').value) : null;
  const slOn = document.getElementById('trig-sl-on').checked;
  const sl = slOn ? parseFloat(document.getElementById('trig-sl').value) : null;
  const infinite = document.getElementById('trig-infinite').checked;
  const reduceOnly = document.getElementById('trig-reduce').checked;
  const scheduleOn = document.getElementById('trig-schedule-on').checked;
  const scheduleVal = scheduleOn ? document.getElementById('trig-schedule').value : null;

  if (portionOn && (!Number.isFinite(portion) || portion <= 0)) { setErr('Enter portion size'); return; }
  if (tpOn && !Number.isFinite(tp)) { setErr('Enter TP spread'); return; }
  if (slOn && !Number.isFinite(sl)) { setErr('Enter SL spread'); return; }
  if (infinite && !portionOn) { setErr('Infinite Fill requires Portion Size'); return; }

  const body = {
    kind: 'open',
    pair_kind: 'long_short',
    long_exchange:  (typeof LONG  === 'string' ? LONG  : ''),
    long_symbol:    SYM,
    long_wallet_id: L.walletId,
    short_exchange: (typeof SHORT === 'string' ? SHORT : ''),
    short_symbol:   SYM,
    short_wallet_id: S.walletId,
    trigger_spread_pct: trigSpread === '' ? null : parseFloat(trigSpread),
    total_qty_token: totalQty,
    portion_size_token: portionOn ? portion : null,
    infinite_fill: infinite,
    leverage: L.leverage || 1,
    margin_mode: L.margin || 'isolated',
    reduce_only: reduceOnly,
    activate_at: scheduleVal ? new Date(scheduleVal).toISOString() : null,
    force,
  };
  if (tpOn) body.tp = { trigger_spread_pct: tp };
  if (slOn) body.sl = { trigger_spread_pct: sl };

  const submitBtn = document.getElementById('trig-submit');
  if (submitBtn) submitBtn.disabled = true;
  try {
    const r = await Auth.apiFetch('/trade/arb-orders', {
      method: 'POST', body: JSON.stringify(body),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      _ltShowSubmitError(r.status, j);
      return;
    }
    if (j.warning === 'immediate_execution' && !force) {
      const ok = await Confirm.ask({
        title: 'Fire immediately?',
        text: `Current effective spread is ${j.current_spread}% which already meets your ${j.kind} target of ${j.requested_trigger}%. The trigger will fire on the next tick.`,
        okText: 'Place anyway',
        okVariant: 'primary',
      });
      if (ok) {
        await trigSubmit(true);
      }
      return;
    }
    toast('Trigger placed', 'success');
    accLoadTriggers();
    document.getElementById('trig-card').open = false;
  } catch (e) {
    setErr(e.message || 'Network error');
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
}

/* ── arb.html block #6 ─────────────────────────────────────────── */
// ═══ Share P&L card ═══════════════════════════════════════════════════════
let _sharePos = null;
let _sharePair = null;            // populated when sharing a paired position
let _sharePairView = 'combined';  // 'combined' | 'long' | 'short'
let _shareMode = 'both'; // 'pct' (ROI %), 'usd' (PNL $), or 'both' (% big + $ small)

// Cached referral code for the current user. The card stamps "ref: <CODE>"
// next to the avalant.xyz watermark so anyone who downloads + reposts the
// image carries a credit-to-the-original-poster handle. We fetch lazily
// (first render attempt) and cache for the life of the page — cheap.
let _shareRefCode = null;
let _shareRefFetching = false;
async function _ensureRefCode(){
  if (_shareRefCode || _shareRefFetching) return;
  _shareRefFetching = true;
  try {
    const r = await Auth.apiFetch('/referrals/me');
    if (r.ok) {
      const j = await r.json();
      _shareRefCode = (j && j.code) ? j.code : '';
    }
  } catch (_) {
    // Silently swallow — the card just renders without a ref tag if we
    // couldn't fetch (e.g. user signed out mid-session).
  } finally {
    _shareRefFetching = false;
  }
}

// Reads the position from the button's data-share attribute. Browsers
// auto-decode HTML entities when reading data-* via JS, so we get clean JSON.
function _openShareFromBtn(btn){
  try {
    const raw = btn.getAttribute('data-share');
    const pos = JSON.parse(raw);
    openShareCard(pos);
  } catch (e) {
    console.error('share-card data parse failed', e, btn.getAttribute('data-share'));
    if (typeof toast === 'function') toast('Could not open share card', 'error');
  }
}

// Closed-pnl row → share card. Reshapes the PnL row (which carries
// realized_pnl_usd / exit_price / etc.) into the share-card pos/pair
// shape so the existing renderer works unchanged.
function _openShareFromPnl(btn){
  try {
    const raw = btn.getAttribute('data-share-pnl');
    const r = JSON.parse(raw);
    if (r.kind === 'pair'){
      const totalPnl = Number(r.total_pnl_usd || 0);
      const lq = Number(r.long.qty||0) * Number(r.long.entry_price||0);
      const sq = Number(r.short.qty||0) * Number(r.short.entry_price||0);
      const pairUsd = (lq + sq) / 2;
      const combinedPct = pairUsd > 0 ? (totalPnl / pairUsd * 100) : 0;
      openSharePairCard({
        symbol: r.symbol,
        long: {
          exchange: r.long.exchange, side: 'buy',
          quantity: Number(r.long.qty||0),
          entry_price: Number(r.long.entry_price||0),
          mark_price: Number(r.long.exit_price||r.long.entry_price||0),
          leverage: 1,
          unrealized_pnl_usd: Number(r.long.realized_pnl_usd||0),
        },
        short: {
          exchange: r.short.exchange, side: 'sell',
          quantity: Number(r.short.qty||0),
          entry_price: Number(r.short.entry_price||0),
          mark_price: Number(r.short.exit_price||r.short.entry_price||0),
          leverage: 1,
          unrealized_pnl_usd: Number(r.short.realized_pnl_usd||0),
        },
        total_pnl_usd: totalPnl,
        total_funding_usd: Number(r.total_funding_pnl_usd||0),
        pair_size_usd: pairUsd,
        combined_pct: combinedPct,
        entry_spread_pct: Number(r.entry_spread_pct||0),
        _closed: true,
      });
      return;
    }
    // Single closed row
    const realized = Number(r.realized_pnl_usd||0)
                    + Number(r.funding_pnl_usd||0)
                    - Number(r.fees_usd||0);
    const notional = Number(r.qty||0) * Number(r.entry_price||0);
    const pnlPct = notional > 0 ? (realized / notional * 100) : 0;
    openShareCard({
      symbol: r.symbol,
      exchange: r.exchange,
      side: r.side,
      quantity: Number(r.qty||0),
      entry_price: Number(r.entry_price||0),
      mark_price: Number(r.exit_price||r.entry_price||0),
      leverage: 1,
      unrealized_pnl_usd: Number(r.realized_pnl_usd||0),
      pnl_pct: pnlPct,
      funding_pnl_usd: Number(r.funding_pnl_usd||0),
      _closed: true,
    });
  } catch (e) {
    console.error('share-pnl parse failed', e);
    if (typeof toast === 'function') toast('Could not open share card', 'error');
  }
}

function openShareCard(pos){
  _sharePos = pos;
  _sharePair = null;
  // Kick off html2canvas load in background — by the time the user
  // clicks download/copy (~3-5s later) it's already cached.
  _loadHtml2Canvas().catch(()=>{});
  // Pick illustration once per modal session — re-rolling on every
  // mode toggle (ROI / PNL / ROI·PNL) felt jittery.
  _scLockNewImg();
  const bd = document.getElementById('sc-backdrop');
  if (!bd) { console.error('sc-backdrop missing'); return; }
  bd.classList.add('open');
  // Hide pair-only view chooser
  const pv = document.getElementById('sc-pair-view'); if (pv) pv.style.display = 'none';
  document.querySelectorAll('.sc-tog-btn').forEach(b => b.classList.toggle('is-active', b.dataset.mode === _shareMode));
  _renderShareCard();
}

function _openSharePairFromBtn(btn){
  try {
    const raw = btn.getAttribute('data-share-pair');
    const pair = JSON.parse(raw);
    openSharePairCard(pair);
  } catch(e){
    console.error('share-pair parse failed', e);
    if (typeof toast === 'function') toast('Could not open share card', 'error');
  }
}

function openSharePairCard(pair){
  _sharePair = pair;
  _sharePos = null;
  _sharePairView = 'combined';
  _loadHtml2Canvas().catch(()=>{});
  _scLockNewImg();   // lock image for the modal session
  const bd = document.getElementById('sc-backdrop');
  if (!bd) return;
  bd.classList.add('open');
  // Show pair-view chooser
  const pv = document.getElementById('sc-pair-view'); if (pv) pv.style.display = 'inline-flex';
  document.querySelectorAll('.sc-pair-view-btn').forEach(b => b.classList.toggle('is-active', b.dataset.view === _sharePairView));
  document.querySelectorAll('.sc-tog-btn').forEach(b => b.classList.toggle('is-active', b.dataset.mode === _shareMode));
  _renderShareCard();
}

function setSharePairView(v){
  if (v === _sharePairView) return;
  _sharePairView = v;
  document.querySelectorAll('.sc-pair-view-btn').forEach(b => b.classList.toggle('is-active', b.dataset.view === v));
  _renderShareCard();
}
function closeShareCard(){
  document.getElementById('sc-backdrop').classList.remove('open');
}
function setShareMode(m){
  if (m === _shareMode) return;
  _shareMode = m;
  document.querySelectorAll('.sc-tog-btn').forEach(b => b.classList.toggle('is-active', b.dataset.mode === m));
  _renderShareCard();
}

function _shareEx(ex){
  return (window.EX && window.EX.labels && window.EX.labels[ex]) || (ex || '').toUpperCase();
}
function _shareExColor(ex){
  return (window.EX && window.EX.colors && window.EX.colors[ex]) || '#1AFFAB';
}
function _fmtPx(p){
  if (p == null || !isFinite(p)) return '—';
  if (p >= 1000) return p.toLocaleString('en-US', {maximumFractionDigits:2});
  if (p >= 1) return (+p).toFixed(4);
  if (p >= 0.01) return (+p).toFixed(5);
  return (+p).toPrecision(4);
}

// Wait for fonts before rendering — otherwise canvas falls back to a
// generic system font and the card looks generic.
let _scFontsReady = false;
async function _scEnsureFonts(){
  if (_scFontsReady) return;
  try {
    if (document.fonts && document.fonts.load){
      await Promise.all([
        document.fonts.load('800 36px "Inter"'),
        document.fonts.load('700 28px "Inter"'),
        document.fonts.load('500 16px "JetBrains Mono"'),
        document.fonts.load('600 56px "Fraunces"'),
        document.fonts.load('400 110px "Fraunces"'),
      ]);
    }
  } catch(_) {}
  _scFontsReady = true;
}

function _renderShareCard(){
  // Bail only when neither single-position nor pair data is set —
  // checking `!_sharePos` alone silently dropped pair-card renders since
  // openSharePairCard sets `_sharePair` and clears `_sharePos`.
  if (!_sharePos && !_sharePair) return;
  // Fire-and-forget the ref-code fetch so the second render lands with
  // the code stamped. The first render runs immediately so the card
  // never feels laggy — the ref line just appears on the next tick.
  if (!_shareRefCode && !_shareRefFetching) {
    _ensureRefCode().then(() => _scEnsureFonts().then(() => _renderShareCardNow()));
  }
  _scEnsureFonts().then(() => _renderShareCardNow());
}

function _renderShareCardNow(){
  if (_sharePair){ _renderPairCombined(); return; }
  if (!_sharePos) return;
  _renderSinglePositionCard();
}

/* ═════════════════════════════════════════════════════════════════
   Share-card render — DOM-based.
   Mirrors frontend/_pnl_card_reference.html. Card lives in
   #sc-card-host as live HTML; download/copy snapshot it through
   html2canvas. No canvas-2D drawing code anymore.
   ═════════════════════════════════════════════════════════════════ */

// Image bundle on disk (frontend/_pnl_card_imgs/*.svg). Pick at random.
// Locked per-modal-session: openShareCard / openSharePairCard sets it
// once so toggling ROI / PNL / ROI·PNL doesn't reroll the illustration.
const _SC_IMGS = ['1','2','3','5','6','7','8','9','10','11','12','13','14','15','16','17','18','19','21','22','23','24','25','26','27','28','29','30'];
let _scLockedImg = null;
function _scLockNewImg(){ _scLockedImg = _SC_IMGS[Math.floor(Math.random() * _SC_IMGS.length)]; }
function _scImg(){ return _scLockedImg || _SC_IMGS[0]; }

// Format helpers (comma-decimal in line with the spec).
function _scFmtPctStr(v){
  const n = +v || 0;
  return `${n >= 0 ? '+' : '−'}${Math.abs(n).toFixed(2).replace('.', ',')}%`;
}
function _scFmtUsdStr(v){
  const n = +v || 0;
  return `${n >= 0 ? '+$' : '−$'}${Math.abs(n).toFixed(2)}`;
}
function _scFmtPriceStr(p){
  if (p == null || !isFinite(p)) return '—';
  p = +p;
  if (p >= 1000)  return p.toLocaleString('en-US', {maximumFractionDigits: 2}).replace(/,/g, ' ').replace('.', ',');
  if (p >= 1)     return p.toFixed(4).replace('.', ',');
  if (p >= 0.01)  return p.toFixed(5).replace('.', ',');
  return p.toPrecision(4).replace('.', ',');
}
function _scFmtFundingFor(mode, fundUsd, fundPct){
  const sign = (n) => (n >= 0 ? '+' : '−');
  const usd = `${sign(fundUsd)}$${Math.abs(+fundUsd || 0).toFixed(2)}`;
  const pct = `${sign(fundPct)}${Math.abs(+fundPct || 0).toFixed(2).replace('.', ',')}%`;
  if (mode === 'pct') return pct;
  if (mode === 'usd') return usd;
  return `${usd} <span style="font-size:13px;opacity:0.55">(${pct})</span>`;
}
function _scHeroHtml(mode, pct, usd, isLoss){
  const pctTxt = _scFmtPctStr(pct);
  const usdTxt = _scFmtUsdStr(usd);
  const cls = isLoss ? ' red' : '';
  if (mode === 'pct') return `<div class="sc-pct${cls}">${pctTxt}</div>`;
  if (mode === 'usd') return `<div class="sc-pct${cls}">${usdTxt}</div>`;
  return `<div class="sc-pct${cls}">${usdTxt}<span class="sc-pct-sub">(${pctTxt})</span></div>`;
}

// Logo block — uses /avalant-logo.svg shipped with the app.
const _SC_LOGO_HTML = `<span class="sc-brand-logo"><img src="/avalant-logo.svg" alt=""></span>`;

// ─── Chevron wallpaper (mulberry32-seeded scatter) ───
const _SC_SHAPES = {
  up:   [[0,-22],[28,2],[14,2],[14,22],[-14,22],[-14,2],[-28,2]],
  down: [[0, 22],[28,-2],[14,-2],[14,-22],[-14,-22],[-14,-2],[-28,-2]],
};
function _scRoundedPoly(verts, r){
  const n = verts.length;
  const seg = (a,b) => Math.hypot(b[0]-a[0], b[1]-a[1]);
  const unit = (a,b) => { const d = seg(a,b); return [(b[0]-a[0])/d, (b[1]-a[1])/d]; };
  const arcs = verts.map((v, i) => {
    const prev = verts[(i - 1 + n) % n], next = verts[(i + 1) % n];
    const inLen = seg(prev, v), outLen = seg(v, next);
    const rEff = Math.min(r, inLen * 0.5, outLen * 0.5);
    const inDir = unit(prev, v), outDir = unit(v, next);
    return {
      start: [v[0] - rEff * inDir[0], v[1] - rEff * inDir[1]],
      end:   [v[0] + rEff * outDir[0], v[1] + rEff * outDir[1]],
      ctrl:  v,
    };
  });
  let d = `M ${arcs[0].start[0].toFixed(2)},${arcs[0].start[1].toFixed(2)}`;
  for (let i = 0; i < n; i++){
    const a = arcs[i], next = arcs[(i + 1) % n];
    d += ` Q ${a.ctrl[0].toFixed(2)},${a.ctrl[1].toFixed(2)} ${a.end[0].toFixed(2)},${a.end[1].toFixed(2)}`;
    d += ` L ${next.start[0].toFixed(2)},${next.start[1].toFixed(2)}`;
  }
  return d + ' Z';
}
function _scMulberry32(seed){
  return function(){
    let t = (seed += 0x6D2B79F5);
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
// Returns the inner SVG markup for the chevron wallpaper.
function _scChevronsSvg(kind, color, seed){
  const W = 1200, H = 750;
  const COUNT = 55;
  const rand = _scMulberry32(0xA1B2 + seed * 0x10093);
  let paths = '';
  for (let i = 0; i < COUNT; i++){
    const k = (kind === 'mix') ? (rand() < 0.5 ? 'up' : 'down') : kind;
    const bucket = rand();
    let size;
    if      (bucket < 0.62) size = 14 + rand() * 18;
    else if (bucket < 0.90) size = 32 + rand() * 26;
    else                    size = 60 + rand() * 30;
    const x = 30 + rand() * (W - 60);
    const y = 30 + rand() * (H - 60);
    const rotation = (rand() - 0.5) * 50;
    const opacity = (size > 48) ? 0.05 + rand() * 0.10 : 0.10 + rand() * 0.18;
    const scale = size / 44;
    const verts = _SC_SHAPES[k].map(([vx, vy]) => [vx * scale, vy * scale]);
    const d = _scRoundedPoly(verts, 9 * scale);
    const sw = Math.max(1.2, 1.6 * (size / 36)).toFixed(2);
    paths += `<path d="${d}" transform="translate(${x.toFixed(1)},${y.toFixed(1)}) rotate(${rotation.toFixed(1)})" fill="none" stroke="${color}" stroke-width="${sw}" stroke-linejoin="round" stroke-linecap="round" opacity="${opacity.toFixed(2)}"/>`;
  }
  return `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid slice">
      <g>${paths}</g>
    </svg>`;
}

// Return referral URL (cached). Uses _shareRefCode populated by
// _ensureRefCode(). If the code hasn't loaded yet, render the bare URL.
function _scRefUrl(){
  return _shareRefCode ? `avalant.xyz/?ref=${_shareRefCode}` : 'avalant.xyz';
}

function _renderSinglePositionCard(){
  const host = document.getElementById('sc-card-host');
  if (!host || !_sharePos) return;
  const p = _sharePos;
  const isLong = p.side === 'buy';
  const arrowKind = isLong ? 'up' : 'down';

  const sideText = isLong ? 'LONG' : 'SHORT';
  const sideCls = isLong ? '' : 'short';
  const lev = (p.leverage && p.leverage > 0) ? `${p.leverage}X` : '';
  const sidePill = lev ? `${sideText} ${lev}` : sideText;

  // ROI denominator = margin (deployed capital) = notional / leverage.
  // All three hero numbers (usd / pct / isLoss) derive from the SAME
  // totalUsd so the card colour, the hero number sign, and the hero
  // pct sign can never disagree.
  const _qty = +p.quantity || 0;
  const _entry = +p.entry_price || 0;
  const _lev = +p.leverage || 1;
  const _margin = (_qty > 0 && _entry > 0 && _lev > 0) ? (_qty * _entry / _lev) : 0;
  const priceUsd = +p.unrealized_pnl_usd || 0;
  const fundUsd = (p.funding_pnl_usd != null) ? Number(p.funding_pnl_usd) : 0;
  const fundPct = _margin > 0 ? (fundUsd / _margin * 100) : 0;
  const usd = priceUsd + fundUsd;
  const pct = _margin > 0 ? (usd / _margin * 100) : (+p.pnl_pct || 0);
  const isLoss = usd < 0;
  const kindClass = isLoss ? 'loss' : 'win';
  const accent = isLoss ? '#F87171' : '#1AFFAB';
  const fundCol = fundUsd >= 0 ? '#1AFFAB' : '#F87171';
  const fundCell = (_shareMode === 'pct')
    ? _scFmtFundingFor('pct', fundUsd, fundPct)
    : (_shareMode === 'usd')
      ? _scFmtFundingFor('usd', fundUsd, fundPct)
      : _scFmtFundingFor('both', fundUsd, fundPct);

  const seed = (p.symbol || 'x').split('').reduce((s, c) => s + c.charCodeAt(0), 0) + (p.exchange || '').length;
  const img = _scImg();

  host.innerHTML = `
    <div class="sc-card ${kindClass}">
      <div class="sc-bg-chevrons">${_scChevronsSvg(arrowKind, accent, seed)}</div>
      <div class="sc-art"><img src="/_pnl_card_imgs/${img}.svg" alt=""></div>
      <div class="sc-content">
        <div class="sc-brand">${_SC_LOGO_HTML}<span>avalant</span></div>
        <div class="sc-ticker-row">
          <span class="sc-ticker">${p.symbol}</span>
          <span class="sc-side-pill ${sideCls}">${sidePill}</span>
        </div>
        ${_scHeroHtml(_shareMode, pct, usd, isLoss)}
        <div class="sc-row-prices">
          <div><div class="sc-pb-lbl">Entry Price</div><div class="sc-pb-val">${_scFmtPriceStr(p.entry_price)}</div></div>
          <div><div class="sc-pb-lbl">Mark Price</div><div class="sc-pb-val">${_scFmtPriceStr(p.mark_price)}</div></div>
          <div><div class="sc-pb-lbl">Funding P&amp;L</div><div class="sc-pb-val" style="color:${fundCol}">${fundCell}</div></div>
        </div>
        <div class="sc-ref">
          <div class="sc-ref-lbl">Referral code:</div>
          <div class="sc-ref-url">https://${_scRefUrl()}</div>
        </div>
      </div>
    </div>`;
}

function _renderPairCombined(){
  const host = document.getElementById('sc-card-host');
  if (!host || !_sharePair) return;
  const pp = _sharePair;
  const totalPnl = +pp.total_pnl_usd || 0;
  const combinedPct = +pp.combined_pct || 0;
  const isLoss = totalPnl < 0;
  const kindClass = isLoss ? 'loss' : 'win';
  const accent = isLoss ? '#F87171' : '#1AFFAB';

  const lEx = (pp.long?.exchange || '').toLowerCase();
  const sEx = (pp.short?.exchange || '').toLowerCase();
  const lLbl = _shareEx(lEx);
  const sLbl = _shareEx(sEx);
  const lCol = _shareExColor(lEx);
  const sCol = _shareExColor(sEx);

  const totalFund = +pp.total_funding_usd || +pp.total_funding || 0;
  // Funding-ROI = total funding ÷ deployed capital (pair_size_usd is
  // single-leg notional, the actual money put up). Without this the
  // ROI mode shows +0,00% even when funding is non-zero.
  const pairSize = +pp.pair_size_usd || 0;
  const fundPct = (pp.total_funding_pct != null)
    ? (+pp.total_funding_pct)
    : (pairSize > 0 ? (totalFund / pairSize * 100) : 0);
  const fundCol = totalFund >= 0 ? '#1AFFAB' : '#F87171';

  const fundCell = _scFmtFundingFor(_shareMode, totalFund, fundPct);
  const entrySpread = +pp.entry_spread_pct || 0;
  const entrySpreadStr = `${entrySpread >= 0 ? '+' : '−'}${Math.abs(entrySpread).toFixed(2).replace('.', ',')}%`;

  const seed = (pp.symbol || '').split('').reduce((s, c) => s + c.charCodeAt(0), 0) + lEx.length + sEx.length;
  const img = _scImg();

  host.innerHTML = `
    <div class="sc-card pair ${kindClass}">
      <div class="sc-bg-chevrons">${_scChevronsSvg('mix', accent, seed)}</div>
      <div class="sc-art"><img src="/_pnl_card_imgs/${img}.svg" alt=""></div>
      <div class="sc-content">
        <div class="sc-brand">${_SC_LOGO_HTML}<span>avalant</span></div>
        <div class="sc-ticker-row">
          <span class="sc-ticker">${pp.symbol}</span>
          <span class="sc-pair-pill">FUTURES PAIR</span>
        </div>
        <div class="sc-ex-line">
          <span class="sc-ex"><span class="sc-dot" style="background:${lCol}"></span>${lLbl} <span class="sc-L">LONG</span></span>
          <span class="sc-arrow">⇄</span>
          <span class="sc-ex"><span class="sc-dot" style="background:${sCol}"></span>${sLbl} <span class="sc-S">SHORT</span></span>
        </div>
        ${_scHeroHtml(_shareMode, combinedPct, totalPnl, isLoss)}
        <div class="sc-row-prices">
          <div><div class="sc-pb-lbl">Entry Spread</div><div class="sc-pb-val">${entrySpreadStr}</div></div>
          <div><div class="sc-pb-lbl">${_shareMode === 'pct' ? 'ROI' : 'uPnL'}</div><div class="sc-pb-val" style="color:${totalPnl >= 0 ? '#1AFFAB' : '#F87171'}">${_shareMode === 'pct' ? _scFmtPctStr(combinedPct) : _scFmtUsdStr(totalPnl)}</div></div>
          <div><div class="sc-pb-lbl">Funding</div><div class="sc-pb-val" style="color:${fundCol}">${fundCell}</div></div>
        </div>
        <div class="sc-ref">
          <div class="sc-ref-lbl">Referral code:</div>
          <div class="sc-ref-url">https://${_scRefUrl()}</div>
        </div>
      </div>
    </div>`;
}

// ─── html2canvas lazy loader ─────────────────────────────────────────
// 200KB / 46KB gzip vendor lib used only by the share-card PNG snapshot.
// We inject the <script> on first need and cache the in-flight Promise.
let _html2canvasPromise = null;
function _loadHtml2Canvas(){
  if (typeof html2canvas === 'function') return Promise.resolve();
  if (_html2canvasPromise) return _html2canvasPromise;
  _html2canvasPromise = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = '/vendor/html2canvas-1.4.1.min.js';
    s.async = true;
    s.onload = () => resolve();
    s.onerror = () => { _html2canvasPromise = null; reject(new Error('html2canvas load failed')); };
    document.head.appendChild(s);
  });
  return _html2canvasPromise;
}

// ─── Snapshot helpers ────────────────────────────────────────────────
// html2canvas captures the live DOM. We build a 1080×675 PNG (2× the
// 540×337 viewport target) for sharp social previews.
async function _scSnapshot(){
  try { await _loadHtml2Canvas(); } catch(e) { console.warn('[share]', e); return null; }
  const host = document.getElementById('sc-card-host');
  if (!host || typeof html2canvas !== 'function') {
    console.warn('[share] html2canvas missing or host gone');
    return null;
  }
  const card = host.querySelector('.sc-card');
  if (!card) return null;
  // Wait for any <img> inside to fully decode — html2canvas otherwise
  // races and renders a blank background where the SVG should be.
  const imgs = Array.from(card.querySelectorAll('img'));
  await Promise.all(imgs.map(im => (im.complete && im.naturalWidth) ? Promise.resolve() :
    new Promise(res => { im.onload = im.onerror = () => res(); })));
  const cardW = card.offsetWidth;
  const cardH = card.offsetHeight;
  const scale = Math.max(2, Math.min(4, 1080 / cardW));
  return await html2canvas(card, {
    backgroundColor: null,
    scale,
    width: cardW,
    height: cardH,
    useCORS: false,
    allowTaint: true,
    logging: false,
  });
}

async function downloadShareCard(){
  try {
    const canvas = await _scSnapshot();
    if (!canvas) {
      if (typeof toast === 'function') toast('Snapshot failed', 'error');
      return;
    }
    const sym = (_sharePair && _sharePair.symbol) || (_sharePos && _sharePos.symbol) || 'pnl';
    const tag = _sharePair ? 'pair' : 'pnl';
    const fname = `avalant-${sym.toLowerCase()}-${tag}-${Date.now()}.png`;
    canvas.toBlob(blob => {
      if (!blob) {
        if (typeof toast === 'function') toast('PNG encode failed', 'error');
        return;
      }
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.download = fname;
      a.href = url;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }, 'image/png');
  } catch (e) {
    console.error('[share] download failed', e);
    if (typeof toast === 'function') toast('Download failed: ' + (e.message || e), 'error');
  }
}

async function copyShareCard(){
  try {
    const canvas = await _scSnapshot();
    if (!canvas) throw new Error('snapshot fail');
    const blob = await new Promise(r => canvas.toBlob(r, 'image/png'));
    if (!blob) throw new Error('blob fail');
    if (navigator.clipboard && window.ClipboardItem){
      await navigator.clipboard.write([new ClipboardItem({'image/png': blob})]);
      _scFlash('sc-copy-btn', 'Copied!');
    } else {
      throw new Error('Clipboard API unavailable');
    }
  } catch (e) {
    _scFlash('sc-copy-btn', 'Use Download');
  }
}
async function copyShareText(){
  // Make sure we've fetched the ref code at least once before the user
  // clicks Copy, otherwise the snippet would lack their handle on first
  // click. Cheap on the second call (cached).
  await _ensureRefCode();
  const url = _shareRefCode
    ? `avalant.xyz/register?ref=${_shareRefCode}`
    : 'avalant.xyz';
  let text;
  if (_sharePair){
    const pp = _sharePair;
    const tot = +pp.total_pnl_usd || 0;
    const pct = +pp.combined_pct || 0;
    const pctTxt = `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
    const usdTxt = `${tot >= 0 ? '+$' : '−$'}${Math.abs(tot).toFixed(2)}`;
    text = `PAIR ${pp.symbol} ${_shareEx(pp.long.exchange)}⇄${_shareEx(pp.short.exchange)} — ${pctTxt} (${usdTxt}) · ${url}`;
  } else if (_sharePos){
    const p = _sharePos;
    const isLong = p.side === 'buy';
    const profit = (p.unrealized_pnl_usd || 0) >= 0;
    const pctTxt = `${profit ? '+' : ''}${(+p.pnl_pct).toFixed(2)}%`;
    const usdTxt = `${profit ? '+$' : '−$'}${Math.abs(+p.unrealized_pnl_usd).toFixed(2)}`;
    text = `${isLong ? 'LONG' : 'SHORT'} ${p.symbol} ${p.leverage}× on ${_shareEx(p.exchange)} — ${pctTxt} (${usdTxt}) · ${url}`;
  } else {
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    if (typeof toast === 'function') toast('Copied to clipboard', 'success');
  } catch {
    if (typeof toast === 'function') toast('Copy failed', 'error');
  }
}

function _scFlash(id, msg){
  const b = document.getElementById(id);
  if (!b) return;
  const orig = b.innerHTML;
  b.innerHTML = msg;
  setTimeout(() => { b.innerHTML = orig; }, 1500);
}

document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeShareCard(); });

/* ── arb.html block #7 ─────────────────────────────────────────── */
function alTriggerPick(btn){
  const v = btn.dataset.v;
  document.getElementById('al-trigger-mode').value = v;
  document.querySelectorAll('.al-trigger-btn').forEach(b => b.classList.toggle('is-active', b.dataset.v === v));
  const hints = {
    speed: 'Fires immediately when spread crosses threshold.',
    protected: 'Waits 3 s then re-checks — only fires if the spread still holds.'
  };
  document.getElementById('al-trigger-hint').textContent = hints[v] || '';
}

