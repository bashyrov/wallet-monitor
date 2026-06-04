/**
 * Shared auth utilities for all frontend pages.
 * Usage: <script src="/auth.js"></script>
 */

// Service Worker registration — caches HTML / JS / CSS / fonts so repeat
// visits are instant and offline-tolerant. Bypasses /api/* and /ws/*
// (real-time data); HTML uses network-first with cache fallback. See
// frontend/sw.js for the cache strategy. Wrapped in capability check
// + try/catch so SW unsupported / blocked never breaks the page.
(function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  if (location.protocol !== 'https:' && location.hostname !== 'localhost') return;
  try {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js').catch(() => {});
    });
  } catch (_) {}
})();

// Capture ?ref=XYZ on any landing page so the register form can prefill
// it even if the user navigates through home/pricing/login first.
(function captureReferral(){
  try {
    const params = new URLSearchParams(window.location.search);
    let code = params.get('ref') || params.get('referral') || params.get('r');
    if (!code) return;
    code = code.trim().toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 16);
    if (code) sessionStorage.setItem('avalant_pending_ref', code);
  } catch {}
})();

const Auth = (() => {
  const TOKEN_KEY = 'wm_token';
  const USER_KEY  = 'wm_user';

  function getToken() { return localStorage.getItem(TOKEN_KEY); }
  function getUser()  {
    try { return JSON.parse(localStorage.getItem(USER_KEY) || 'null'); }
    catch { return null; }
  }

  function setSession(token, user) {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(USER_KEY, JSON.stringify(user));
    // Remove the page's anonymous-state class without a reload — pages that
    // gate UI on `html.no-auth` (auth-lock overlays on /arb, etc.) can
    // re-paint as authed immediately. Login flows never set this class so
    // the remove is a no-op there.
    try { document.documentElement.classList.remove('no-auth'); } catch (_) {}
    // Signal listeners (notably <app-navbar>) so they re-render auth-aware
    // bits like the avatar initial. Without this, a navbar that rendered
    // BEFORE the async cookie-session probe finished would stay stuck on
    // the placeholder "U" until next full page load.
    try { window.dispatchEvent(new CustomEvent('avalant:auth-changed', { detail: { user } })); } catch (_) {}
  }

  function clearSession() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    try { window.dispatchEvent(new CustomEvent('avalant:auth-changed', { detail: { user: null } })); } catch (_) {}
  }

  function isLoggedIn() { return !!getToken(); }

  /** Redirect to login if not authenticated. */
  function requireAuth(redirectTo = '/login') {
    if (!isLoggedIn()) {
      window.location.replace(redirectTo + '?next=' + encodeURIComponent(window.location.pathname));
    }
  }

  /** Redirect to app if authenticated but not admin. */
  function requireAdmin(redirectTo = '/portfolio') {
    if (!isLoggedIn()) {
      window.location.replace('/login?next=' + encodeURIComponent(window.location.pathname));
      return;
    }
    const user = getUser();
    if (!user || !user.is_admin) {
      window.location.replace(redirectTo);
    }
  }

  /** Returns true only if logged in AND is_admin. */
  function isAdmin() {
    const user = getUser();
    return isLoggedIn() && !!user?.is_admin;
  }

  /** If already logged in, redirect away from login/register pages.
   *  Clears stale tokens instead of redirecting — avoids the redirect loop
   *  caused by valid Bearer token + expired HttpOnly session cookie. */
  function redirectIfAuthed(redirectTo = '/screener') {
    if (!isLoggedIn()) return;
    // Validate token before redirecting to avoid redirect loops on stale JWT.
    // Target should be a non-cookie-gated page (e.g. /screener) — gated pages
    // (/app, /portfolio, /profile) require the session cookie which may be
    // missing even when the localStorage JWT is fresh.
    fetch('/api/auth/me', { headers: { 'Authorization': 'Bearer ' + getToken() } })
      .then(r => {
        if (r.ok) {
          location.replace(redirectTo);
        } else {
          clearSession();
        }
      })
      .catch(() => {});
  }

  /** Logout: clear session + clear server cookie + redirect. */
  function logout(redirectTo = '/login') {
    clearSession();
    fetch('/api/auth/logout', { method: 'POST' }).finally(() => {
      window.location.replace(redirectTo);
    });
  }

  /** Base fetch wrapper that adds Authorization header. */
  async function apiFetch(path, opts = {}) {
    const token = getToken();
    const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
    if (token) headers['Authorization'] = 'Bearer ' + token;
    const resp = await fetch('/api' + path, { ...opts, headers });
    // 401 redirect logic — only matters for authenticated sessions whose
    // token died mid-flight. Anonymous visitors expect 401s on user-only
    // endpoints (watchlist, /auth/me, alerts, etc.) and should NOT get
    // bounced off a public page like /screener.
    if (resp.status === 401 && token) {
      clearSession();
      const p = window.location.pathname;
      if (p !== '/login' && p !== '/register') {
        window.location.replace('/login');
      }
      throw new Error('Session expired');
    }
    return resp;
  }

  return { getToken, getUser, setSession, clearSession, isLoggedIn, isAdmin, requireAuth, requireAdmin, redirectIfAuthed, logout, apiFetch };
})();

// One-shot: if localStorage is empty but the HttpOnly session cookie carries
// a live JWT (the user is logged in server-side but lost their localStorage —
// privacy extension, clear-browsing-data, navigated between www and apex,
// fresh browser profile etc.), recover the token so the page's IS_AUTHED
// check stops showing the anonymous lockout overlay.
//
// We always probe (no `wm_authed=1` gate any more) — gating on the cookie
// missed legacy sessions that predate the companion-cookie deploy. The
// extra request is cheap (HMAC + DB user lookup, ~5-10ms) and only fires
// when localStorage is empty; recurring visitors with a token short-circuit
// at the isLoggedIn check.
//
// On success we DON'T reload — Auth.setSession's classList.remove('no-auth')
// + downstream handlers re-running their fetch on next interval will repaint
// the page in place. Reload was a sledgehammer that ate any in-flight WS
// state.
(function recoverFromCookie() {
  if (Auth.isLoggedIn()) return;
  fetch('/api/auth/cookie-session', { credentials: 'include' })
    .then(r => r.ok ? r.json() : null)
    .then(j => {
      if (!j || !j.access_token) return;
      Auth.setSession(j.access_token, j.user);
    })
    .catch(() => {});
})();
