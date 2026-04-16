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
  app:      ['app', 'archive', 'screener', 'watchlist', 'pricing'],
  screener: ['app', 'archive', 'screener', 'watchlist', 'pricing'],
  archive:  ['app', 'archive', 'screener', 'watchlist', 'pricing'],
  profile:  ['app', 'archive', 'screener', 'watchlist', 'pricing'],
  index:    ['app', 'archive', 'screener', 'watchlist', 'pricing'],
  pricing:  ['app', 'archive', 'screener', 'watchlist', 'pricing'], // Portfolio+Archive hidden until auth
  arb:      ['app', 'pricing'],
  watchlist:['app', 'archive', 'screener', 'watchlist', 'pricing'],
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
  return `<a href="${link.href}" class="${cls}">${link.icon}${link.label}</a>`;
}

function _avatarBtn() {
  return `<a href="/profile" class="avatar-btn" id="nav-avatar" title="Profile">U</a>`;
}

function _rightHtml(page) {
  switch (page) {
    case 'app':
      return `<button class="btn btn-primary btn-sm" onclick="openAddWalletModal()">+ Add Wallet</button>${_avatarBtn()}`;
    case 'screener':
    case 'archive':
    case 'profile':
    case 'checkout':
    case 'watchlist':
      return _avatarBtn();
    case 'arb':
      return `
        <a href="/watchlist" class="nav-lnk nav-lnk-icon" title="Watchlist" aria-label="Watchlist">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor" stroke="currentColor" stroke-width="0.8" stroke-linejoin="round"><path d="M7 1.3l1.85 3.75 4.15.6-3 2.93.71 4.13L7 10.77 3.29 12.7 4 8.57l-3-2.92 4.15-.6z"/></svg>
        </a>
        <button class="nav-lnk" onclick="openAlertModal&&openAlertModal()" title="Alerts">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M8 2a5 5 0 0 1 5 5v3l1 2H2l1-2V7a5 5 0 0 1 5-5z"/><path d="M6.5 13.5a1.5 1.5 0 0 0 3 0"/></svg>
          Alerts
        </button>
        ${_avatarBtn()}`;
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
      return `<a href="${l.href}" class="${cls}" data-nb-id="${l.id}"${hidden}>${l.icon}${l.label}</a>`;
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

    // Guest/user toggle for index and pricing
    if (page === 'index' || page === 'pricing') {
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
