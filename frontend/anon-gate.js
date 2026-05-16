/**
 * Anonymous screener gate — 2-minute look-around for not-logged-in
 * users, then a hard popup that demands sign-up. Surviving page reload
 * is the whole point: the timer is anchored to a localStorage timestamp,
 * not a per-session counter.
 *
 * Behaviour:
 *   · On load, if not logged in:
 *       - localStorage `anon_first_seen_at` is set on first visit ever.
 *       - Every 2s, check elapsed time. After 120s elapsed, show the hard
 *         lock — no close button, page interaction blocked behind it.
 *       - "Sign in" / "Create account" links lead to /login or /register.
 *   · On reload after the 2 min has passed: lock shows immediately on
 *     first paint. localStorage survives reloads.
 *   · Cleared session → no lock for the next 2 min (this is the
 *     documented behaviour the user picked: localStorage is the truth,
 *     wiping it grants 2 more minutes).
 *
 * The page only includes this script on /screener (and similar
 * public-but-gated pages). Logged-in users bypass entirely — `Auth`
 * presence is enough since /screener already requires auth in the
 * normal Avalant flow but the public-preview deployment leaves it
 * accessible for SEO + sign-up funnel.
 */
(function(){
  // Path guard. anon-gate.js is bundled into core.min.js (loaded on every
  // page) but the gate must ONLY appear on /screener. Без этого юзер видит
  // лок и на /index, /landing, /pricing, /portfolio — bug from the bundle
  // refactor where the script became globally loaded.
  // Match /screener, /screener?..., /screener/anything. Anything else
  // short-circuits before localStorage touched.
  try {
    const path = (location.pathname || '').replace(/\/+$/, '') || '/';
    if (path !== '/screener' && !path.startsWith('/screener/')) return;
  } catch (_) { return; }

  const KEY = 'anon_first_seen_at';
  const LIMIT_MS = 120 * 1000;     // 2 minutes
  const POLL_MS = 2000;
  let _shown = false;

  function _now(){ return Date.now(); }

  function _isAuthed(){
    // Check localStorage directly — `Auth` is a top-level const in
    // auth.js, NOT a window property, so `window.Auth` reads as
    // undefined and the gate previously fired for logged-in users.
    try {
      return !!localStorage.getItem('wm_token');
    } catch { return false; }
  }

  function _firstSeen(){
    let v = null;
    try { v = parseInt(localStorage.getItem(KEY) || '', 10); } catch {}
    if (!Number.isFinite(v) || v <= 0){
      v = _now();
      try { localStorage.setItem(KEY, String(v)); } catch {}
    }
    return v;
  }

  function _injectStyles(){
    if (document.getElementById('anon-gate-style')) return;
    const css = `
      .anon-gate-backdrop{
        position:fixed;inset:0;z-index:1000;
        background:rgba(0,0,0,0.78);backdrop-filter:blur(6px);
        display:flex;align-items:center;justify-content:center;
        animation:anon-gate-fade .22s ease-out;
      }
      @keyframes anon-gate-fade{from{opacity:0}to{opacity:1}}
      .anon-gate-card{
        background:var(--surface,#131217);
        border:1px solid var(--border,#22222A);
        border-radius:18px;
        padding:36px 36px 30px;
        max-width:460px;width:calc(100% - 32px);
        box-shadow:0 22px 70px rgba(0,0,0,0.55);
        font-family:'Inter',sans-serif;
        text-align:center;
        animation:anon-gate-rise .26s cubic-bezier(0.16,1,0.3,1);
      }
      @keyframes anon-gate-rise{from{opacity:0;transform:scale(0.96)}to{opacity:1;transform:scale(1)}}
      .anon-gate-icon{
        width:54px;height:54px;
        border-radius:50%;
        background:rgba(26,255,171,0.12);
        display:inline-flex;align-items:center;justify-content:center;
        margin:0 auto 16px;
        color:var(--green,#1AFFAB);
      }
      .anon-gate-title{
        font-size:22px;font-weight:700;letter-spacing:-0.01em;
        margin:0 0 8px;color:var(--text,#E6E8E3);
      }
      .anon-gate-body{
        font-size:14px;line-height:1.55;color:var(--text2,#9B9FAB);
        margin:0 0 22px;
      }
      .anon-gate-cta-row{display:flex;gap:10px;flex-direction:column}
      .anon-gate-btn{
        display:block;text-align:center;text-decoration:none;
        padding:13px 20px;border-radius:10px;
        font-weight:700;font-size:14px;font-family:inherit;
        border:0;cursor:pointer;transition:.18s;
      }
      .anon-gate-btn-primary{background:var(--green,#1AFFAB);color:#000}
      .anon-gate-btn-primary:hover{filter:brightness(1.06)}
      .anon-gate-btn-secondary{
        background:transparent;border:1px solid var(--border2,#3A3A50);
        color:var(--text,#E6E8E3);
      }
      .anon-gate-btn-secondary:hover{background:var(--surface3,#202028)}
      .anon-gate-foot{
        font-size:12px;color:var(--text3,#676B7E);margin-top:18px;
      }
      body.light .anon-gate-card{background:#FFFFFF;border-color:#BABABA}
      body.light .anon-gate-title{color:#000}
      body.light .anon-gate-body{color:#1A1A1A}
      body.light .anon-gate-btn-primary{background:#006B3C;color:#FFF}
      body.light .anon-gate-btn-secondary{color:#000;border-color:#8C8C8C}
      body.light .anon-gate-icon{background:rgba(0,107,60,0.08);color:#006B3C}
      .anon-gate-locked{overflow:hidden}
    `;
    const tag = document.createElement('style');
    tag.id = 'anon-gate-style';
    tag.textContent = css;
    (document.head || document.documentElement).appendChild(tag);
  }

  function _show(){
    if (_shown) return;
    _shown = true;
    _injectStyles();
    document.body.classList.add('anon-gate-locked');
    const next = encodeURIComponent(location.pathname + location.search);
    const card = document.createElement('div');
    card.className = 'anon-gate-backdrop';
    card.id = 'anon-gate';
    card.innerHTML = `
      <div class="anon-gate-card" role="dialog" aria-modal="true" aria-labelledby="anon-gate-title">
        <div class="anon-gate-icon">
          <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="4" y="11" width="16" height="9" rx="2"/>
            <path d="M8 11V7a4 4 0 0 1 8 0v4"/>
          </svg>
        </div>
        <h3 class="anon-gate-title" id="anon-gate-title">Sign in to keep scanning</h3>
        <p class="anon-gate-body">You've had a 2-minute look around. Create a free account to keep using the live screener — no card required, 5 portfolio wallets and the full live data are on the house.</p>
        <div class="anon-gate-cta-row">
          <a class="anon-gate-btn anon-gate-btn-primary" href="/register?next=${next}">Create free account</a>
          <a class="anon-gate-btn anon-gate-btn-secondary" href="/login?next=${next}">Sign in</a>
        </div>
        <div class="anon-gate-foot">Scanner data, portfolio, alerts — all included. Upgrade only if you need 30 portfolio wallets, no trade delay, or 3 keys per exchange.</div>
      </div>
    `;
    // Block clicks behind the card. The lock has no close button on
    // purpose — the only way out is signing in or refusing to use the
    // page.
    document.body.appendChild(card);
  }

  function _tick(){
    if (_isAuthed()){
      // Authenticated session — clear timer + remove any lock that
      // might be lingering (shouldn't happen but defensive).
      try { localStorage.removeItem(KEY); } catch {}
      const el = document.getElementById('anon-gate');
      if (el){
        el.remove();
        document.body.classList.remove('anon-gate-locked');
        _shown = false;
      }
      return;
    }
    const since = _firstSeen();
    if (_now() - since >= LIMIT_MS) _show();
  }

  function start(){
    if (_isAuthed()) return;
    // Anchor first-seen NOW so the timer starts ticking even if the
    // user had cleared localStorage. Subsequent loads keep using the
    // same value (because _firstSeen reads existing first).
    _firstSeen();
    _tick();
    setInterval(_tick, POLL_MS);
  }

  if (document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();
