/**
 * Shared auth utilities for all frontend pages.
 * Usage: <script src="/auth.js"></script>
 */

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
  function requireAdmin(redirectTo = '/app') {
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
   *  Validates the token is still accepted by the backend first —
   *  prevents a redirect loop when the HttpOnly session cookie has
   *  expired but the localStorage token is still present.
   *  Hides <body> during the check to prevent form flash ("jump"). */
  function redirectIfAuthed(redirectTo = '/app') {
    if (!isLoggedIn()) return;
    // Hide body while checking — prevents the form from flashing before redirect
    document.documentElement.style.visibility = 'hidden';
    fetch('/api/auth/me', { headers: { 'Authorization': 'Bearer ' + getToken() } })
      .then(r => {
        if (r.ok) {
          window.location.replace(redirectTo);
        } else {
          clearSession();
          document.documentElement.style.visibility = '';
        }
      })
      .catch(() => {
        document.documentElement.style.visibility = '';
      });
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
    if (resp.status === 401) {
      clearSession();
      window.location.replace('/login');
      throw new Error('Session expired');
    }
    return resp;
  }

  return { getToken, getUser, setSession, clearSession, isLoggedIn, isAdmin, requireAuth, requireAdmin, redirectIfAuthed, logout, apiFetch };
})();
