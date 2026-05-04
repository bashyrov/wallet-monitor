/**
 * <app-navbar page="app|screener|archive|profile|index|pricing|login|register|checkout">
 *
 * Variants by page:
 *   app        — all nav links, active: Portfolio, right: + Add Wallet + avatar
 *   screener   — all nav links, active: Screener,  right: avatar
 *   archive    — all nav links, active: Archive,   right: avatar
 *   profile    — all nav links, active: none,      right: avatar
 *   index      — all nav links, active: none,      right: guest(Sign In + Get Started) / user(Open App + avatar)
 *   pricing    — all nav links (Portfolio+Archive auth-gated), active: Pricing, right: guest(Sign In) / user(avatar)
 *   login      — no nav,        right: Register + Open App
 *   register   — no nav,        right: Sign In + Open App
 *   checkout   — Portfolio+Pricing nav, right: avatar
 */

const _ICONS = {
  portfolio: `<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><rect x="1" y="6" width="3" height="5" rx="0.7" fill="currentColor" opacity=".5"/><rect x="4.5" y="3.5" width="3" height="7.5" rx="0.7" fill="currentColor" opacity=".75"/><rect x="8" y="1" width="3" height="10" rx="0.7" fill="currentColor"/></svg>`,
  archive:   `<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><rect x="1" y="4" width="10" height="7" rx="1" stroke="currentColor" stroke-width="1.35"/><path d="M1 4l1.5-2.5h7L11 4" stroke="currentColor" stroke-width="1.35" stroke-linejoin="round"/><path d="M4.5 6.5h3" stroke="currentColor" stroke-width="1.35" stroke-linecap="round"/></svg>`,
  screener:  `<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M1.5 6h9M1.5 3h9M1.5 9h5" stroke="currentColor" stroke-width="1.35" stroke-linecap="round"/></svg>`,
  pricing:   `<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M1.5 1.5h3.8l5 5-3.8 3.8-5-5V1.5z" stroke="currentColor" stroke-width="1.35" stroke-linejoin="round"/><circle cx="4" cy="4" r="0.9" fill="currentColor"/></svg>`,
  watchlist: `<svg width="13" height="13" viewBox="0 0 14 14" fill="none"><defs><linearGradient id="wl-g-${Math.random().toString(36).slice(2,7)}" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="currentColor"/><stop offset="1" stop-color="currentColor" stop-opacity="0.55"/></linearGradient></defs><path d="M7 1.3l1.85 3.75 4.15.6-3 2.93.71 4.13L7 10.77 3.29 12.7 4 8.57l-3-2.92 4.15-.6z" fill="currentColor" stroke="currentColor" stroke-width="0.8" stroke-linejoin="round"/></svg>`,
  login:     `<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M8 2H10a1 1 0 011 1v6a1 1 0 01-1 1H8M5 9l3-3-3-3M1 6h7" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
};

// All standard nav links
const _ALL_LINKS = [
  { id: 'app',       href: '/app',       label: 'Portfolio', icon: _ICONS.portfolio, authOnly: false },
  { id: 'archive',   href: '/archive',   label: 'Archive',   icon: _ICONS.archive,   authOnly: false },
  { id: 'screener',  href: '/screener',  label: 'Screener',  icon: _ICONS.screener,  authOnly: false },
  { id: 'watchlist', href: '/watchlist', label: 'Watchlist', icon: _ICONS.watchlist, authOnly: false },
  { id: 'pricing',   href: '/pricing',   label: 'Pricing',   icon: _ICONS.pricing,   authOnly: false },
];

// Links shown per page variant
const _NAV_SET = {
  app:      ['app', 'archive', 'screener', 'pricing'],
  archive:  ['app', 'archive', 'screener', 'pricing'],
  profile:  ['app', 'archive', 'screener', 'pricing'],
  index:    ['app', 'archive', 'screener', 'pricing'],
  pricing:  ['app', 'archive', 'screener', 'pricing'],
  // Screener service (screener + arb + watchlist) — trimmed nav.
  // Arb pages (long-short, spot-short, dex-short) include a Screener link
  // to replace the deprecated ib-back-link arrow on the infobar.
  screener: ['app', 'pricing'],
  arb:      ['app', 'screener', 'pricing'],
  watchlist:['app', 'screener', 'pricing'],
  login:    [],
  register: [],
  checkout: ['app', 'pricing'],
};

// Which link is active per page
const _ACTIVE = {
  app:      'app',
  screener: 'screener',
  archive:  'archive',
  pricing:  'pricing',
  watchlist:'watchlist',
  profile:  null,
  index:    null,
  login:    null,
  register: null,
  checkout: null,
  arb:      'screener',
};

function _navLink(link, active) {
  const cls = 'nav-lnk' + (link.id === active ? ' active' : '');
  // Wrap the text label in a span so CSS can hide it on narrow viewports
  // (mobile arb pages show icon-only nav).
  return `<a href="${link.href}" class="${cls}">${link.icon}<span class="nav-lnk-label">${link.label}</span></a>`;
}

function _avatarBtn() {
  return `<a href="/profile" class="avatar-btn" id="nav-avatar" title="Profile">U</a>`;
}

function _rightHtml(page) {
  switch (page) {
    case 'app':
      return `<button class="btn btn-primary btn-sm" onclick="openAddWalletModal()">+ Add Wallet</button>${_avatarBtn()}`;
    case 'archive':
    case 'profile':
    case 'checkout':
      return _avatarBtn();
    // Screener service — consistent icon toolbar across screener / arb / watchlist.
    // Auth-only icons (watchlist, alerts, avatar) live inside #_nb-user;
    // anonymous visitors see Sign In + Get Started in #_nb-guest. _applyAuth
    // flips the display: between the two blocks based on Auth.isLoggedIn().
    case 'screener':
    case 'arb':
    case 'watchlist': {
      const wlActive = page === 'watchlist' ? ' active' : '';
      return `
        <div id="_nb-guest" style="display:flex;align-items:center;gap:8px">
          <a href="/login" class="nav-lnk">${_ICONS.login}Sign In</a>
          <a href="/register" class="btn btn-primary btn-sm">Get Started</a>
        </div>
        <div id="_nb-user" style="display:none;align-items:center;gap:8px">
          <a href="/watchlist" class="nav-lnk nav-lnk-icon${wlActive}" title="Watchlist" aria-label="Watchlist">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor" stroke="currentColor" stroke-width="0.8" stroke-linejoin="round"><path d="M7 1.3l1.85 3.75 4.15.6-3 2.93.71 4.13L7 10.77 3.29 12.7 4 8.57l-3-2.92 4.15-.6z"/></svg>
          </a>
          <button class="nav-lnk nav-lnk-bell" onclick="openAlertsPopover(event)" title="Alerts" aria-label="Alerts">
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2a5 5 0 0 1 5 5v3l1 2H2l1-2V7a5 5 0 0 1 5-5z"/><path d="M6.5 13.5a1.5 1.5 0 0 0 3 0"/></svg>
            <span class="nav-dot" id="nb-alerts-dot" style="display:none"></span>
          </button>
          ${_avatarBtn()}
        </div>`;
    }
    case 'index':
      return `
        <div id="_nb-guest" style="display:flex;align-items:center;gap:8px">
          <a href="/login" class="nav-lnk">${_ICONS.login}Sign In</a>
          <a href="/register" class="btn btn-primary btn-sm">Get Started</a>
        </div>
        <div id="_nb-user" style="display:none;align-items:center;gap:8px">
          <a href="/app" class="btn btn-primary btn-sm">Open App</a>
          ${_avatarBtn()}
        </div>`;
    case 'pricing':
      return `
        <div id="_nb-guest" style="display:flex;align-items:center;gap:8px">
          <a href="/login" class="btn btn-primary btn-sm" id="topbar-cta">Sign In</a>
        </div>
        <div id="_nb-user" style="display:none;align-items:center;gap:8px">
          ${_avatarBtn()}
        </div>`;
    case 'login':
      return `<a href="/register" class="nav-lnk">${_ICONS.login}Register</a>
              <a href="/app" class="btn btn-primary btn-sm">Open App</a>`;
    case 'register':
      return `<a href="/login" class="nav-lnk">${_ICONS.login}Sign in</a>
              <a href="/app" class="btn btn-primary btn-sm">Open App</a>`;
    default:
      return '';
  }
}

class AppNavbar extends HTMLElement {
  connectedCallback() {
    const page = this.getAttribute('page') || 'index';
    const active = _ACTIVE[page] ?? null;
    const linkIds = _NAV_SET[page] ?? [];
    const links = _ALL_LINKS.filter(l => linkIds.includes(l.id));

    // For pricing: Portfolio+Archive are auth-gated (hidden initially)
    const authGated = page === 'pricing' ? ['app', 'archive'] : [];

    const navHtml = links.map(l => {
      const hidden = authGated.includes(l.id) ? ' style="display:none"' : '';
      const cls = 'nav-lnk' + (l.id === active ? ' active' : '');
      // Wrap the text label so CSS can collapse it to icon-only on mobile.
      return `<a href="${l.href}" class="${cls}" data-nb-id="${l.id}"${hidden}>${l.icon}<span class="nav-lnk-label">${l.label}</span></a>`;
    }).join('');

    this.innerHTML = `
      <a href="/" class="brand">avalant<span class="brand-cursor">_</span></a>
      <nav class="topbar-nav">${navHtml}</nav>
      <div class="topbar-right">${_rightHtml(page)}</div>
    `;

    this._initAuth(page);
  }

  _initAuth(page) {
    if (typeof Auth === 'undefined') {
      // auth.js not loaded yet — wait for it
      document.addEventListener('DOMContentLoaded', () => this._applyAuth(page));
      return;
    }
    this._applyAuth(page);
  }

  _applyAuth(page) {
    if (typeof Auth === 'undefined') return;
    const loggedIn = Auth.isLoggedIn();
    const user = Auth.getUser();

    // Avatar initial
    if (loggedIn && user) {
      const av = this.querySelector('#nav-avatar');
      if (av) av.textContent = (user.username || user.email || 'U')[0].toUpperCase();
    }

    // Guest/user toggle: index, pricing, screener, arb, watchlist all
    // ship both blocks now and pick the right one based on auth state.
    if (['index', 'pricing', 'screener', 'arb', 'watchlist'].includes(page)) {
      const guestEl = this.querySelector('#_nb-guest');
      const userEl  = this.querySelector('#_nb-user');
      if (loggedIn) {
        if (guestEl) guestEl.style.display = 'none';
        if (userEl)  userEl.style.display = 'flex';
      } else {
        if (guestEl) guestEl.style.display = 'flex';
        if (userEl)  userEl.style.display = 'none';
      }
    }

    // Auth-gated nav links (pricing page)
    if (loggedIn) {
      this.querySelectorAll('[data-nb-id]').forEach(el => {
        if (el.style.display === 'none') el.style.display = '';
      });
    }
  }
}

customElements.define('app-navbar', AppNavbar);

// ── Shared alerts popover (used by screener + watchlist) ─────────────────────
(function(){
  if (window.openAlertsPopover) return;

  const EX_LABEL = {binance:'Binance',bybit:'Bybit',okx:'OKX',gate:'Gate',kucoin:'KuCoin',mexc:'MEXC',bitget:'Bitget',hyperliquid:'Hyperliquid',aster:'Aster',ethereal:'Ethereal',whitebit:'WhiteBIT',bingx:'BingX',lighter:'Lighter',paradex:'Paradex'};

  const CSS = `
.nb-alerts-pop{position:fixed;background:var(--surface,#131217);border:1px solid var(--border,#22222A);border-radius:12px;box-shadow:0 18px 48px rgba(0,0,0,.5);min-width:320px;max-width:380px;max-height:70vh;display:flex;flex-direction:column;z-index:500;overflow:hidden;font-family:Inter,sans-serif;opacity:0;transform:translateY(-4px);transition:opacity .16s,transform .16s;}
.nb-alerts-pop.open{opacity:1;transform:translateY(0);}
.nb-alerts-hdr{display:flex;align-items:center;gap:8px;padding:12px 14px;border-bottom:1px solid var(--border,#22222A);}
.nb-alerts-hdr-title{font-size:13px;font-weight:700;flex:1;letter-spacing:-0.01em;color:var(--text,#E6E8E3);}
.nb-alerts-hdr-count{padding:2px 7px;border-radius:999px;background:var(--surface3,#202028);color:var(--text3,#676B7E);font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:700;}
.nb-alerts-body{flex:1;overflow-y:auto;padding:6px;}
.nb-alert-row{display:flex;align-items:center;gap:8px;padding:9px 10px;border-radius:8px;cursor:pointer;transition:background .12s;text-decoration:none;color:inherit;}
.nb-alert-row:hover{background:var(--surface2,#17171C);}
.nb-alert-sym{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:12.5px;min-width:50px;color:var(--text,#E6E8E3);}
.nb-alert-pair{font-size:11px;color:var(--text3,#676B7E);flex:1;letter-spacing:0.01em;}
.nb-alert-thr{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--yellow,#E5C07B);font-weight:600;background:rgba(229,192,123,0.08);padding:2px 7px;border-radius:6px;}
.nb-alert-toggle{width:30px;height:16px;border-radius:8px;background:var(--surface3,#202028);position:relative;flex-shrink:0;transition:background .15s;cursor:pointer;}
.nb-alert-toggle::after{content:'';position:absolute;top:2px;left:2px;width:12px;height:12px;border-radius:50%;background:var(--text3,#676B7E);transition:transform .16s,background .15s;}
.nb-alert-toggle.on{background:rgba(26,255,171,0.2);}
.nb-alert-toggle.on::after{transform:translateX(14px);background:var(--green,#1AFFAB);}
.nb-alert-del{display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;border:none;background:transparent;color:var(--text3,#676B7E);border-radius:5px;cursor:pointer;flex-shrink:0;transition:color .12s,background .12s;opacity:0;font-family:inherit;}
.nb-alert-row:hover .nb-alert-del{opacity:1;}
.nb-alert-del:hover{color:var(--red,#F87171);background:rgba(248,113,113,0.08);}
.nb-alerts-empty{padding:28px 16px;text-align:center;color:var(--text3,#676B7E);font-size:12.5px;}
.nb-alerts-empty-icon{margin:0 auto 10px;width:38px;height:38px;display:flex;align-items:center;justify-content:center;border-radius:10px;background:var(--surface2,#17171C);color:var(--text3,#676B7E);}
.nb-alerts-empty .nb-hint{color:var(--text2,#9B9FAB);font-size:11.5px;margin-top:4px;}
`;
  const style = document.createElement('style'); style.id='nb-alerts-pop-css'; style.textContent=CSS; document.head.appendChild(style);

  let _pop = null;
  async function openAlertsPopover(ev){
    if (ev) ev.stopPropagation();
    if (_pop) { closePopover(); return; }
    const btn = ev?.currentTarget || document.querySelector('.nav-lnk-bell');
    const rect = btn ? btn.getBoundingClientRect() : { right: window.innerWidth-20, bottom: 56 };

    _pop = document.createElement('div');
    _pop.className = 'nb-alerts-pop';
    _pop.innerHTML = `
      <div class="nb-alerts-hdr">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2a5 5 0 0 1 5 5v3l1 2H2l1-2V7a5 5 0 0 1 5-5z"/><path d="M6.5 13.5a1.5 1.5 0 0 0 3 0"/></svg>
        <span class="nb-alerts-hdr-title">Alerts</span>
        <span class="nb-alerts-hdr-count" id="nb-alerts-count">—</span>
      </div>
      <div class="nb-alerts-body" id="nb-alerts-body"><div class="nb-alerts-empty">Loading…</div></div>
    `;
    document.body.appendChild(_pop);
    const popW = _pop.offsetWidth;
    const left = Math.max(12, Math.min(rect.right - popW, window.innerWidth - popW - 12));
    const top  = rect.bottom + 6;
    _pop.style.left = left + 'px';
    _pop.style.top  = top + 'px';
    requestAnimationFrame(() => _pop.classList.add('open'));

    // outside click
    setTimeout(() => document.addEventListener('click', _outside, { once: false }), 0);

    try {
      const r = await Auth.apiFetch('/alerts');
      const rows = r.ok ? await r.json() : [];
      renderPop(rows);
    } catch { renderPop([]); }
  }

  function _outside(e){ if (_pop && !_pop.contains(e.target) && !e.target.closest('.nav-lnk-bell')) closePopover(); }
  function closePopover(){
    if (!_pop) return;
    document.removeEventListener('click', _outside);
    _pop.classList.remove('open');
    const p = _pop; _pop = null;
    setTimeout(() => p.remove(), 160);
  }

  function renderPop(rows){
    if (!_pop) return;
    const body = _pop.querySelector('#nb-alerts-body');
    const cnt  = _pop.querySelector('#nb-alerts-count');
    cnt.textContent = rows.length;
    if (!rows.length) {
      body.innerHTML = `
        <div class="nb-alerts-empty">
          <div class="nb-alerts-empty-icon">
            <svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2a5 5 0 0 1 5 5v3l1 2H2l1-2V7a5 5 0 0 1 5-5z"/><path d="M6.5 13.5a1.5 1.5 0 0 0 3 0"/></svg>
          </div>
          No alerts yet
          <div class="nb-hint">Open a pair on the Screener and tap the alert button to add one.</div>
        </div>`;
      return;
    }
    body.innerHTML = rows.map(a => {
      const arrow = a.direction==='above' ? '≥' : a.direction==='below' ? '≤' : '±';
      return `
      <a class="nb-alert-row" href="/arb?symbol=${a.symbol}&long=${a.long_exchange}&short=${a.short_exchange}" target="_blank" data-alert-id="${a.id}">
        <span class="nb-alert-sym">${a.symbol}</span>
        <span class="nb-alert-pair">${EX_LABEL[a.long_exchange]||a.long_exchange} → ${EX_LABEL[a.short_exchange]||a.short_exchange}</span>
        <span class="nb-alert-thr">${arrow}${(a.threshold).toFixed(3)}%</span>
        <span class="nb-alert-toggle ${a.enabled?'on':''}" title="Enable/disable" onclick="event.preventDefault();event.stopPropagation();_nbToggleAlert(${a.id},this)"></span>
        <button class="nb-alert-del" title="Delete alert" onclick="event.preventDefault();event.stopPropagation();_nbDeleteAlert(${a.id},this)">
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M3 5h10M6 5V3.5a1 1 0 011-1h2a1 1 0 011 1V5m-4 0v8a1 1 0 001 1h2a1 1 0 001-1V5"/></svg>
        </button>
      </a>`;
    }).join('');
  }

  async function _nbToggleAlert(id, el){
    try {
      const r = await Auth.apiFetch(`/alerts/${id}/toggle`, { method:'PATCH' });
      if (r.ok) el.classList.toggle('on');
    } catch {}
  }

  async function _nbDeleteAlert(id, btn){
    if (window.Confirm) {
      const ok = await window.Confirm.ask({
        title: 'Delete alert?',
        message: 'This alert will stop triggering Telegram notifications.',
        okText: 'Delete', danger: true,
      });
      if (!ok) return;
    }
    try {
      const r = await Auth.apiFetch(`/alerts/${id}`, { method:'DELETE' });
      if (!r.ok) throw new Error();
      // remove row + update count
      const row = btn.closest('.nb-alert-row');
      if (row) row.remove();
      const cnt = _pop?.querySelector('#nb-alerts-count');
      if (cnt) cnt.textContent = Math.max(0, parseInt(cnt.textContent||'0') - 1);
      // refresh bell dot
      window.refreshAlertsDot?.();
      // if empty now, render empty state
      const body = _pop?.querySelector('#nb-alerts-body');
      if (body && !body.querySelector('.nb-alert-row')) renderPop([]);
    } catch {
      if (window.toast) toast('Failed to delete', 'error');
    }
  }

  window.openAlertsPopover = openAlertsPopover;
  window._nbToggleAlert = _nbToggleAlert;
  window._nbDeleteAlert = _nbDeleteAlert;

  // Alerts count dot next to bell — lightweight badge
  window.refreshAlertsDot = async function(){
    try {
      const r = await Auth.apiFetch('/alerts');
      if (!r.ok) return;
      const rows = await r.json();
      const dot = document.getElementById('nb-alerts-dot');
      if (!dot) return;
      const n = rows.filter(a => a.enabled).length;
      dot.style.display = n > 0 ? 'inline-block' : 'none';
    } catch {}
  };
  document.addEventListener('DOMContentLoaded', () => {
    const p = window.location.pathname;
    if (p === '/login' || p === '/register' || p === '/') return;
    setTimeout(() => window.refreshAlertsDot?.(), 800);
  });
})();
