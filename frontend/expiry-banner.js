/**
 * Plan-expiry banner — pings /api/auth/me on every page load that
 * carries an authenticated session, and pops a sticky banner across
 * the top when the user's plan_expires_at is within 7 / 3 / 1 days.
 *
 * Behaviour:
 *   · 7 days out      → blue tone, "expires in N days"
 *   · 3 days or fewer → yellow tone, "expires in N days · renew"
 *   · 0 days (now)    → red tone, "expired — renew to keep portfolio"
 *
 * Dismissed banners hide for 24h via localStorage so the user isn't
 * pestered every page nav while they decide.
 *
 * Free-tier users (no expiry) and lifetime / unlimited plans skip the
 * banner entirely.
 */
(function(){
  function _isAuthed(){
    try { return !!localStorage.getItem('wm_token'); } catch { return false; }
  }
  if (!_isAuthed()) return;

  const KEY = 'expiry_banner_dismissed_until';
  const SUPPRESS_MS = 24 * 60 * 60 * 1000;

  function _suppressed(){
    try {
      const t = parseInt(localStorage.getItem(KEY) || '0', 10);
      return Number.isFinite(t) && Date.now() < t;
    } catch { return false; }
  }

  async function _check(){
    if (_suppressed()) return;
    let me = null;
    try {
      const r = await fetch('/api/auth/me', {
        headers: { Authorization: 'Bearer ' + localStorage.getItem('wm_token') },
      });
      if (!r.ok) return;
      me = await r.json();
    } catch { return; }
    if (!me || !me.plan_expires_at) return;
    const exp = new Date(me.plan_expires_at);
    if (Number.isNaN(exp.getTime())) return;
    const days = Math.round((exp.getTime() - Date.now()) / 86400000);
    if (days > 7) return;
    _show(me, days);
  }

  function _show(me, days){
    if (document.getElementById('expiry-banner')) return;
    let tone, msg;
    if (days <= 0) {
      tone = '#F87171';
      msg = `Your <b>${me.plan_slug || me.plan || 'plan'}</b> plan expired — portfolio scan downgraded. <a href="/pricing" style="color:#fff;text-decoration:underline">Renew</a>`;
    } else if (days <= 3) {
      tone = '#E5C07B';
      msg = `Your <b>${me.plan_slug || me.plan || 'plan'}</b> plan ends in <b>${days} day${days === 1 ? '' : 's'}</b>. <a href="/pricing" style="color:#000;text-decoration:underline">Renew now</a>`;
    } else {
      tone = '#06B6D4';
      msg = `Your <b>${me.plan_slug || me.plan || 'plan'}</b> plan renews in <b>${days} days</b>. <a href="/pricing" style="color:#fff;text-decoration:underline">Manage</a>`;
    }
    const bar = document.createElement('div');
    bar.id = 'expiry-banner';
    bar.style.cssText = `
      position:sticky; top:0; z-index:200;
      background:${tone}; color:${days <= 3 && days > 0 ? '#000' : '#fff'};
      padding:9px 16px; text-align:center; font-size:13px; font-weight:500;
      font-family:'Inter',sans-serif;
      box-shadow:0 2px 12px rgba(0,0,0,0.12);
      display:flex; justify-content:center; align-items:center; gap:14px;
    `;
    bar.innerHTML = `
      <span>${msg}</span>
      <button id="expiry-banner-close" aria-label="Dismiss"
        style="background:transparent;border:0;color:inherit;font-size:18px;cursor:pointer;line-height:1;padding:0 6px;opacity:0.75">×</button>
    `;
    document.body.insertBefore(bar, document.body.firstChild);
    document.getElementById('expiry-banner-close').addEventListener('click', () => {
      try { localStorage.setItem(KEY, String(Date.now() + SUPPRESS_MS)); } catch {}
      bar.remove();
    });
  }

  if (document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', _check, { once: true });
  } else {
    _check();
  }
})();
