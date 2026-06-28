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
  { id: 'app',       href: '/portfolio',       label: 'Portfolio', icon: _ICONS.portfolio, authOnly: false },
  { id: 'screener',  href: '/screener',  label: 'Screener',  icon: _ICONS.screener,  authOnly: false },
  { id: 'pricing',   href: '/pricing',   label: 'Pricing',   icon: _ICONS.pricing,   authOnly: false },
];

const _NAV_SET = {
  app:       ['app', 'screener', 'pricing'],
  archive:   ['app', 'screener', 'pricing'],
  profile:   ['app', 'screener', 'pricing'],
  index:     ['app', 'screener', 'pricing'],
  pricing:   ['app', 'screener', 'pricing'],
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
  // Default to a neutral silhouette icon. Once Auth state is known
  // (sync read from localStorage, or after the cookie-session probe
  // dispatches avalant:auth-changed), _applyAuth() replaces this with
  // the user's initial. The icon prevents a misleading "U" placeholder
  // when localStorage is briefly empty after a hard refresh.
  return `<button type="button" class="avatar-btn" id="nav-avatar" title="Account menu" aria-label="Account menu" aria-haspopup="menu" onclick="openAvatarMenu(event)"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4.4 3.6-8 8-8s8 3.6 8 8"/></svg></button>`;
}

function _rightHtml(page) {
  switch (page) {
    case 'app':
      return _avatarBtn();
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
          <a href="/portfolio" class="btn btn-primary btn-sm">Open app</a>
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
              <a href="/portfolio" class="btn btn-primary btn-sm">Open app</a>`;
    case 'register':
      return `<a href="/login" class="btn btn-ghost btn-sm">Sign in</a>
              <a href="/portfolio" class="btn btn-primary btn-sm">Open app</a>`;

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
      <button class="nav-burger" id="nb-burger" aria-label="Open menu" aria-expanded="false">
        <svg class="bg-icon bg-open" width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M3 6h14M3 10h14M3 14h14"/></svg>
        <svg class="bg-icon bg-close" width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M5 5l10 10M15 5L5 15"/></svg>
      </button>
    `;

    // Drawer (separate root so it sits on top of everything).
    // On every page render we REPLACE existing markup so per-page nav-set
    // changes (e.g. screener vs portfolio) propagate without a hard refresh.
    let drawer = document.getElementById('nav-drawer-root');
    if (!drawer) {
      drawer = document.createElement('div');
      drawer.id = 'nav-drawer-root';
      drawer.className = 'nav-drawer';
      document.body.appendChild(drawer);
    }
    drawer.innerHTML = `
      <div class="drawer-top">
        <a href="/" class="brand">avalant<span class="brand-cursor">_</span></a>
        <button class="nav-burger" id="nb-close" aria-label="Close menu">
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M5 5l10 10M15 5L5 15"/></svg>
        </button>
      </div>
      <nav class="drawer-menu">${drawerHtml}</nav>
      <!-- Account block — shown only when logged in. Replaces the guest CTAs
           at the drawer bottom so mobile users get Profile + Sign-out the
           same way desktop users get them via the avatar dropdown. -->
      <div class="drawer-account" id="nb-drawer-account" style="display:none">
        <a href="/profile" class="drawer-acct-item" data-nb-drawer="profile">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="5" r="2.6"/><path d="M3 14c0-2.8 2.2-5 5-5s5 2.2 5 5"/></svg>
          <span>Profile</span>
        </a>
        <button type="button" class="drawer-acct-item danger" id="nb-drawer-logout">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M10 5L13 8 10 11M13 8H5M7 2H3a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h4"/></svg>
          <span>Sign out</span>
        </button>
      </div>
      <div class="drawer-cta" id="nb-drawer-cta">
        <a href="/login" class="btn btn-outline btn-lg" id="nb-drawer-signin">Sign in</a>
        <a href="/register" class="btn btn-primary btn-lg" id="nb-drawer-register">Get started</a>
      </div>
    `;

    this._wireBurger();
    this._wireScrollState();
    this._initAuth(page);
  }

  _wireBurger() {
    const drawer = document.getElementById('nav-drawer-root');
    const burger = this.querySelector('#nb-burger');
    if (!drawer || !burger) return;
    const open = () => {
      drawer.classList.add('open');
      burger.classList.add('open');
      burger.setAttribute('aria-expanded', 'true');
      // No body-overflow lock — drawer drops below the topbar, page can stay scrollable
    };
    const shut = () => {
      drawer.classList.remove('open');
      burger.classList.remove('open');
      burger.setAttribute('aria-expanded', 'false');
    };
    const toggle = () => burger.classList.contains('open') ? shut() : open();
    burger.addEventListener('click', toggle);
    drawer.querySelectorAll('a[data-nb-drawer], .drawer-acct-item').forEach(el => el.addEventListener('click', shut));
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
    // Re-apply on every auth state change (Auth.setSession dispatches the
    // event after cookie-session probe lands, OAuth bridge completes, etc.).
    // Without this the avatar stays stuck on the placeholder "U" when the
    // navbar was rendered before localStorage was populated.
    window.addEventListener('avalant:auth-changed', () => this._applyAuth(page));
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

    // Hamburger visibility — narrow by page+auth:
    //   guest  → only on index + pricing (they get Sign in / Get started CTAs
    //            on desktop; mobile drawer lets them see those CTAs too)
    //   authed → only on index (every other auth-only page navigates via
    //            the mobile bottom-nav; logout is reachable via /profile)
    const burger = this.querySelector('#nb-burger');
    if (burger) {
      const allowedGuest = (page === 'index' || page === 'pricing');
      const allowedAuthed = (page === 'index');
      const show = loggedIn ? allowedAuthed : allowedGuest;
      burger.style.display = show ? '' : 'none';
      // Mirror state into a class on the host element so CSS can centre
      // the brand on mobile pages where the topbar is brand-only.
      this.classList.toggle('no-burger', !show);
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

    // Drawer: when logged in, swap the guest CTAs for the Account block
    // (Profile + Sign out). When guest, show Sign in + Get started.
    const drCta = document.getElementById('nb-drawer-cta');
    const drAcct = document.getElementById('nb-drawer-account');
    if (loggedIn) {
      if (drCta) drCta.style.display = 'none';
      if (drAcct) drAcct.style.display = 'flex';
      const logoutBtn = document.getElementById('nb-drawer-logout');
      if (logoutBtn && !logoutBtn._wired) {
        logoutBtn._wired = true;
        logoutBtn.addEventListener('click', () => {
          if (Auth && Auth.logout) Auth.logout('/login');
          else window.location.href = '/login';
        });
      }
    } else {
      if (drCta) drCta.style.display = 'flex';
      if (drAcct) drAcct.style.display = 'none';
    }
  }
}
customElements.define('app-navbar', AppNavbar);

/* ─── Avatar dropdown menu (used on every page with a navbar) ──────────
   Shows Profile + Sign out so the user picks intent instead of always
   being routed to /profile on click. Anchored under the avatar button,
   closes on outside click + Escape. */
window.openAvatarMenu = window.openAvatarMenu || function(ev) {
  ev && ev.stopPropagation();
  // Toggle off if already open
  const existing = document.getElementById('nb-avatar-menu');
  if (existing) { existing.remove(); return; }

  const anchor = (ev && ev.currentTarget) || document.getElementById('nav-avatar');
  if (!anchor) return;
  const r = anchor.getBoundingClientRect();

  // Pull user info from Auth (cached on window after login). Falls back
  // to "Account" / no plan badge for legacy sessions without cached user.
  const u = (typeof Auth !== 'undefined' && Auth.getUser) ? (Auth.getUser() || {}) : {};
  const name = u.username || u.email || 'Account';
  const initial = (name[0] || 'A').toUpperCase();
  const planRaw = (u.plan || '').toString().toLowerCase();
  const planLabel = planRaw ? planRaw.charAt(0).toUpperCase() + planRaw.slice(1) : '';
  const isAdmin = !!u.is_admin;

  const menu = document.createElement('div');
  menu.id = 'nb-avatar-menu';
  menu.className = 'nb-avatar-menu';
  menu.setAttribute('role', 'menu');
  menu.innerHTML = `
    <div class="nb-avm-head">
      <div class="nb-avm-avatar">${initial}</div>
      <div class="nb-avm-meta">
        <div class="nb-avm-name" title="${name}">${name}</div>
        ${planLabel ? `<div class="nb-avm-plan ${'plan-'+planRaw}">${planLabel}${isAdmin ? ' · Admin' : ''}</div>` : (isAdmin ? '<div class="nb-avm-plan plan-admin">Admin</div>' : '')}
      </div>
    </div>
    <div class="nb-avm-sep"></div>
    <a href="/portfolio" class="nb-avm-item" role="menuitem">
      <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="12" height="9" rx="2"/><path d="M2 7h12M6 4V2.5a1 1 0 011-1h2a1 1 0 011 1V4"/></svg>
      <span>Portfolio</span>
    </a>
    <a href="/profile" class="nb-avm-item" role="menuitem">
      <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="5" r="2.6"/><path d="M3 14c0-2.8 2.2-5 5-5s5 2.2 5 5"/></svg>
      <span>Profile</span>
    </a>
    <a href="/profile#sec-security" class="nb-avm-item" role="menuitem">
      <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="7" width="10" height="6" rx="1.5"/><path d="M5 7V5a3 3 0 016 0v2"/></svg>
      <span>Security &amp; 2FA</span>
    </a>
    <a href="/avashare" class="nb-avm-item" role="menuitem">
      <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="5" cy="5" r="2"/><circle cx="11" cy="11" r="2"/><path d="M6.5 6.5l3 3"/></svg>
      <span>Avashare</span>
    </a>
    ${isAdmin ? `<a href="/admin" class="nb-avm-item" role="menuitem">
      <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.5l5.5 2v4c0 3.4-2.3 6.4-5.5 7.5C4.8 13.9 2.5 10.9 2.5 7.5v-4L8 1.5z"/></svg>
      <span>Admin</span>
    </a>` : ''}
    <div class="nb-avm-sep"></div>
    <button type="button" class="nb-avm-item danger" role="menuitem" id="nb-avm-logout">
      <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M10 5L13 8 10 11M13 8H5M7 2H3a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h4"/></svg>
      <span>Sign out</span>
    </button>
  `;
  Object.assign(menu.style, {
    position: 'fixed',
    top: (r.bottom + 8) + 'px',
    right: Math.max(8, window.innerWidth - r.right) + 'px',
    zIndex: '400',
  });
  document.body.appendChild(menu);
  // Animate in on next frame
  requestAnimationFrame(() => menu.classList.add('open'));

  const close = () => {
    menu.classList.remove('open');
    setTimeout(() => menu.remove(), 120);
    document.removeEventListener('click', onOutside, true);
    document.removeEventListener('keydown', onEsc, true);
  };
  const onOutside = (e) => {
    if (menu.contains(e.target)) return;
    if (anchor.contains(e.target)) return;
    close();
  };
  const onEsc = (e) => { if (e.key === 'Escape') close(); };
  setTimeout(() => {
    document.addEventListener('click', onOutside, true);
    document.addEventListener('keydown', onEsc, true);
  }, 0);

  const logoutBtn = menu.querySelector('#nb-avm-logout');
  if (logoutBtn) {
    logoutBtn.addEventListener('click', (e) => {
      e.preventDefault();
      close();
      if (typeof Auth !== 'undefined' && Auth.logout) Auth.logout('/login');
      else window.location.href = '/login';
    });
  }
};

/* ─── Shared alerts popover (used by screener / arb / watchlist) ─────── */
window.openAlertsPopover = window.openAlertsPopover || function(ev) {
  // Pair-page custom modal wins (richer UI lives on /arb)
  if (typeof window._openAlertsModal === 'function') return window._openAlertsModal(ev);
  ev && ev.stopPropagation();

  // Toggle off if already open
  let pop = document.getElementById('nb-alerts-pop');
  if (pop && pop.classList.contains('open')) { pop.remove(); return; }
  if (pop) pop.remove();

  const anchor = (ev && ev.currentTarget) || document.querySelector('.nav-lnk-bell');
  if (!anchor) return;
  const r = anchor.getBoundingClientRect();

  pop = document.createElement('div');
  pop.id = 'nb-alerts-pop';
  pop.className = 'nb-alerts-pop open';
  pop.innerHTML = `
    <div class="nbap-head">
      <span class="nbap-title">Alerts</span>
      <a href="/arb" class="nbap-add" title="Create alert (open arb page)">+ New</a>
    </div>
    <div class="nbap-body" id="nbap-body">
      <div class="nbap-empty"><span class="nbap-spinner"></span></div>
    </div>
  `;
  Object.assign(pop.style, {
    position: 'fixed',
    top: (r.bottom + 8) + 'px',
    right: Math.max(8, window.innerWidth - r.right - 4) + 'px',
    zIndex: '300',
  });
  document.body.appendChild(pop);

  // Click outside to close
  const close = () => { pop.remove(); document.removeEventListener('click', onOutside, true); };
  const onOutside = (e) => {
    if (pop.contains(e.target)) return;
    if (anchor.contains(e.target)) return;
    close();
  };
  setTimeout(() => document.addEventListener('click', onOutside, true), 0);

  // Load alerts
  const body = pop.querySelector('#nbap-body');
  const dirLabel = { any: 'Any side', above: '≥ +', below: '≤ −' };
  const modeLbl = { futures: 'L/S', spot: 'Spot', dex: 'DEX' };

  const render = (alerts) => {
    if (!Array.isArray(alerts) || !alerts.length) {
      body.innerHTML = `
        <div class="nbap-empty">
          <div class="nbap-empty-ic">
            <svg width="22" height="22" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 2a5 5 0 0 1 5 5v3l1 2H2l1-2V7a5 5 0 0 1 5-5z"/><path d="M6.5 13.5a1.5 1.5 0 0 0 3 0"/></svg>
          </div>
          <div class="nbap-empty-title">No alerts yet</div>
          <div class="nbap-empty-sub">Open any pair on /arb and set a spread threshold to get a Telegram ping.</div>
        </div>`;
      return;
    }
    body.innerHTML = alerts.map(a => {
      const isAny = a.long_exchange === '*' && a.short_exchange === '*';
      const pair = isAny ? 'Any pair' : `${a.long_exchange} → ${a.short_exchange}`;
      const tIcon = (a.trigger_mode || 'speed') === 'protected' ? '🛡' : '⚡';
      const m = modeLbl[a.mode] || 'L/S';
      const link = `/arb?symbol=${encodeURIComponent(a.symbol)}&type=${a.mode === 'futures' ? 'long-short' : a.mode || 'long-short'}` + (isAny ? '' : `&long=${encodeURIComponent(a.long_exchange)}&short=${encodeURIComponent(a.short_exchange)}`);
      return `
        <div class="nbap-row" data-id="${a.id}">
          <a href="${link}" class="nbap-row-main">
            <div class="nbap-row-top">
              <span class="nbap-sym">${a.symbol}</span>
              <span class="nbap-mode">${m}</span>
              <span class="nbap-trig">${tIcon}</span>
            </div>
            <div class="nbap-row-sub">
              <span class="nbap-thr">${dirLabel[a.direction] || ''}${a.threshold}%</span>
              <span class="nbap-pair">${pair}</span>
            </div>
          </a>
          <button class="nbap-toggle ${a.enabled ? 'on' : ''}" data-id="${a.id}" data-on="${a.enabled ? '1' : '0'}" title="${a.enabled ? 'Disable' : 'Enable'}">${a.enabled ? 'ON' : 'OFF'}</button>
          <button class="nbap-del" data-id="${a.id}" title="Delete">×</button>
        </div>`;
    }).join('');
  };

  const reload = async () => {
    try {
      const res = await Auth.apiFetch('/alerts');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const alerts = await res.json();
      render(alerts);
    } catch (err) {
      body.innerHTML = `<div class="nbap-empty"><div class="nbap-empty-title">Failed to load</div><div class="nbap-empty-sub">${err.message || 'Network error'}</div></div>`;
    }
  };

  pop.addEventListener('click', async (e) => {
    const tog = e.target.closest('.nbap-toggle');
    const del = e.target.closest('.nbap-del');
    if (tog) {
      e.preventDefault(); e.stopPropagation();
      const id = tog.dataset.id;
      tog.disabled = true;
      try {
        const r = await Auth.apiFetch(`/alerts/${id}/toggle`, { method: 'PATCH' });
        if (r.ok) await reload();
      } finally { tog.disabled = false; }
    } else if (del) {
      e.preventDefault(); e.stopPropagation();
      const id = del.dataset.id;
      del.disabled = true;
      try {
        const r = await Auth.apiFetch(`/alerts/${id}`, { method: 'DELETE' });
        if (r.ok) await reload();
      } finally { del.disabled = false; }
    }
  });

  reload();
};
