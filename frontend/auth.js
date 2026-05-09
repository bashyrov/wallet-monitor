/**
 * Shared auth utilities for all frontend pages.
 * Usage: <script src="/auth.js"></script>
 */

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
  }

  function clearSession() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
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
