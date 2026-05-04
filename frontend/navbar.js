/* ═══════════════════════════════════════════════════════════════════════
 * Avalant — navbar Web Component (landing-style redesign)
 *
 * Usage: <app-navbar page="app|screener|archive|profile|index|pricing|
 *                        login|register|checkout|arb|watchlist|admin">
 *
 * Layout:
 *   [brand] ─────── [center menu] ─────── [right cluster | avatar]
 *
 * On mobile (≤900px) the center + right blocks collapse into a single
 * burger that opens a fullscreen drawer with serif menu items + CTA.
 * ═══════════════════════════════════════════════════════════════════════ */

const _ICONS = {
  portfolio: `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M2 11V7M5 11V4M8 11V8M11 11V2"/></svg>`,
  screener:  `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M2 7h10M2 4h10M2 10h6"/></svg>`,
  archive:   `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M1.5 4.5h11v7a1 1 0 0 1-1 1h-9a1 1 0 0 1-1-1v-7zM.5 1.5h13v3H.5zM5.5 7.5h3"/></svg>`,
  pricing:   `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M1.5 1.5h5l6 6-5 5-6-6v-5z"/><circle cx="4.5" cy="4.5" r="1" fill="currentColor"/></svg>`,
  login:     `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M9 5L13 9 9 13M13 9H4M4 1H2a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h2"/></svg>`,
};

const _ALL_LINKS = [
  { id: 'app',       href: '/app',       label: 'Portfolio', icon: _ICONS.portfolio, authOnly: false },
  { id: 'archive',   href: '/archive',   label: 'Archive',   icon: _ICONS.archive,   authOnly: false },
  { id: 'screener',  href: '/screener',  label: 'Screener',  icon: _ICONS.screener,  authOnly: false },
  { id: 'pricing',   href: '/pricing',   label: 'Pricing',   icon: _ICONS.pricing,   authOnly: false },
];

const _NAV_SET = {
  app:       ['app', 'archive', 'screener', 'pricing'],
  archive:   ['app', 'archive', 'screener', 'pricing'],
  profile:   ['app', 'archive', 'screener', 'pricing'],
  index:     ['app', 'archive', 'screener', 'pricing'],
  pricing:   ['app', 'archive', 'screener', 'pricing'],
  // Screener service — trimmed nav so the workspace gets max width.
  screener:  ['app', 'screener', 'pricing'],
  arb:       ['app', 'screener', 'pricing'],
  watchlist: ['app', 'screener', 'pricing'],
  // Auth flows — no nav strip
  login:    [],
  register: [],
  checkout: ['app', 'pricing'],
};

const _ACTIVE = {
  app: 'app', screener: 'screener', archive: 'archive',
  pricing: 'pricing', watchlist: 'watchlist',
  profile: null, index: null, login: null, register: null, checkout: null,
  arb: 'screener',
};

function _navLink(link, active) {
  const cls = 'nav-lnk' + (link.id === active ? ' active' : '');
  return `<a href="${link.href}" class="${cls}" data-nb-id="${link.id}">${link.icon}<span class="nav-lnk-label">${link.label}</span></a>`;
}

function _drawerLink(link, active, idx) {
  const cls = link.id === active ? ' class="active"' : '';
  const num = String(idx + 1).padStart(2, '0');
  return `<a href="${link.href}"${cls} data-nb-drawer="${link.id}">${link.label}<span class="num">${num}</span></a>`;
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

    // Screener service — guest sees Sign In + Get Started; authed sees
    // watchlist icon + alerts bell + avatar.
    case 'screener':
    case 'arb':
    case 'watchlist': {
      const wlActive = page === 'watchlist' ? ' active' : '';
      return `
        <div id="_nb-guest" style="display:flex;align-items:center;gap:10px">
          <a href="/login" class="btn btn-ghost btn-sm">Sign in</a>
          <a href="/register" class="btn btn-primary btn-sm">Get started</a>
        </div>
        <div id="_nb-user" style="display:none;align-items:center;gap:8px">
          <a href="/watchlist" class="nav-lnk-icon${wlActive}" title="Watchlist" aria-label="Watchlist">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M8 1.3l1.85 3.75 4.15.6-3 2.93.71 4.13L8 10.77 4.29 12.7 5 8.57l-3-2.92 4.15-.6z"/></svg>
          </a>
          <button class="nav-lnk-bell" onclick="openAlertsPopover(event)" title="Alerts" aria-label="Alerts">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2a5 5 0 0 1 5 5v3l1 2H2l1-2V7a5 5 0 0 1 5-5z"/><path d="M6.5 13.5a1.5 1.5 0 0 0 3 0"/></svg>
            <span class="nav-dot" id="nb-alerts-dot" style="display:none"></span>
          </button>
          ${_avatarBtn()}
        </div>`;
    }

    case 'index':
      return `
        <div id="_nb-guest" style="display:flex;align-items:center;gap:10px">
          <a href="/login" class="btn btn-ghost btn-sm">Sign in</a>
          <a href="/register" class="btn btn-primary btn-sm">Get started</a>
        </div>
        <div id="_nb-user" style="display:none;align-items:center;gap:10px">
          <a href="/app" class="btn btn-primary btn-sm">Open app</a>
          ${_avatarBtn()}
        </div>`;

    case 'pricing':
      return `
        <div id="_nb-guest" style="display:flex;align-items:center;gap:10px">
          <a href="/login" class="btn btn-ghost btn-sm">Sign in</a>
          <a href="/register" class="btn btn-primary btn-sm">Get started</a>
        </div>
        <div id="_nb-user" style="display:none;align-items:center;gap:10px">${_avatarBtn()}</div>`;

    case 'login':
      return `<a href="/register" class="btn btn-ghost btn-sm">Register</a>
              <a href="/app" class="btn btn-primary btn-sm">Open app</a>`;
    case 'register':
      return `<a href="/login" class="btn btn-ghost btn-sm">Sign in</a>
              <a href="/app" class="btn btn-primary btn-sm">Open app</a>`;

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

    const navHtml = links.map(l => _navLink(l, active)).join('');
    const drawerHtml = links.map((l, i) => _drawerLink(l, active, i)).join('');

    this.innerHTML = `
      <a href="/" class="brand">avalant<span class="brand-cursor">_</span></a>
      <nav class="topbar-nav">${navHtml}</nav>
      <div class="topbar-right">${_rightHtml(page)}</div>
      <button class="nav-burger" id="nb-burger" aria-label="Open menu">
        <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M3 6h14M3 10h14M3 14h14"/></svg>
      </button>
    `;

    // Drawer (separate root so it sits on top of everything)
    if (!document.getElementById('nav-drawer-root')) {
      const drawer = document.createElement('div');
      drawer.id = 'nav-drawer-root';
      drawer.className = 'nav-drawer';
      drawer.innerHTML = `
        <div class="drawer-top">
          <a href="/" class="brand">avalant<span class="brand-cursor">_</span></a>
          <button class="nav-burger" id="nb-close" aria-label="Close menu">
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M5 5l10 10M15 5L5 15"/></svg>
          </button>
        </div>
        <nav class="drawer-menu">${drawerHtml}</nav>
        <div class="drawer-cta">
          <a href="/login" class="btn btn-outline btn-lg" id="nb-drawer-signin">Sign in</a>
          <a href="/register" class="btn btn-primary btn-lg" id="nb-drawer-register">Get started</a>
        </div>
      `;
      document.body.appendChild(drawer);
    }

    this._wireBurger();
    this._wireScrollState();
    this._initAuth(page);
  }

  _wireBurger() {
    const drawer = document.getElementById('nav-drawer-root');
    const burger = this.querySelector('#nb-burger');
    if (!drawer || !burger) return;
    const close = drawer.querySelector('#nb-close');
    const open = () => { drawer.classList.add('open'); burger.classList.add('open'); document.body.style.overflow = 'hidden'; };
    const shut = () => { drawer.classList.remove('open'); burger.classList.remove('open'); document.body.style.overflow = ''; };
    burger.addEventListener('click', open);
    close && close.addEventListener('click', shut);
    drawer.querySelectorAll('a[data-nb-drawer]').forEach(a => a.addEventListener('click', shut));
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') shut(); });
  }

  _wireScrollState() {
    const topbar = this.closest('.topbar');
    if (!topbar) return;
    const onScroll = () => {
      if (window.scrollY > 4) topbar.classList.add('scrolled');
      else topbar.classList.remove('scrolled');
    };
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
  }

  _initAuth(page) {
    if (typeof Auth === 'undefined') {
      document.addEventListener('DOMContentLoaded', () => this._applyAuth(page));
      return;
    }
    this._applyAuth(page);
  }

  _applyAuth(page) {
    if (typeof Auth === 'undefined') return;
    const loggedIn = Auth.isLoggedIn();
    const user = Auth.getUser();

    if (loggedIn && user) {
      const av = this.querySelector('#nav-avatar');
      if (av) av.textContent = (user.username || user.email || 'U')[0].toUpperCase();
    }

    // Guest/user toggle for pages that ship both blocks
    if (['index', 'pricing', 'screener', 'arb', 'watchlist'].includes(page)) {
      const guestEl = this.querySelector('#_nb-guest');
      const userEl  = this.querySelector('#_nb-user');
      if (loggedIn) {
        if (guestEl) guestEl.style.display = 'none';
        if (userEl)  userEl.style.display  = 'flex';
      } else {
        if (guestEl) guestEl.style.display = 'flex';
        if (userEl)  userEl.style.display  = 'none';
      }
    }

    // Drawer CTA also hides Sign-In when already logged in
    const drSignin = document.getElementById('nb-drawer-signin');
    const drReg    = document.getElementById('nb-drawer-register');
    if (loggedIn && drSignin && drReg) {
      drSignin.style.display = 'none';
      drReg.textContent = 'Open app';
      drReg.href = '/app';
    }
  }
}
customElements.define('app-navbar', AppNavbar);

/* ─── Shared alerts popover (used by screener / arb / watchlist) ─────── */
window.openAlertsPopover = window.openAlertsPopover || function(ev) {
  if (typeof window._openAlertsModal === 'function') return window._openAlertsModal(ev);
  if (typeof window.toast === 'function') {
    window.toast({title: 'Alerts', sub: 'Coming soon — use /arb on a pair to set per-symbol alerts'});
  }
};
