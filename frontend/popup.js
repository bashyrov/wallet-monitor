/**
 * Promotional popup loader — polls /api/popups/pending on page load (and
 * once every 5 minutes if multiple popups are queued), shows the first
 * eligible one, and POSTs /api/popups/{id}/dismiss when the user closes
 * it. Backend handles all targeting + frequency logic; this script only
 * renders + dismisses.
 *
 * Anonymous users are skipped — they have a separate gate (see
 * /screener.html) and the /api/popups/pending endpoint is auth-only
 * anyway.
 *
 * The only design assumption: one popup at a time. If multiple popups
 * are eligible, we render the first then dismiss-and-refetch on close
 * so they queue.
 */
(function(){
  function injectStyles(){
    if (document.getElementById('avalant-popup-style')) return;
    const css = `
      .avalant-popup-backdrop{
        position:fixed;inset:0;z-index:600;
        background:rgba(0,0,0,0.55);backdrop-filter:blur(4px);
        display:flex;align-items:center;justify-content:center;
        animation:avalant-pop-fade .18s ease-out;
      }
      @keyframes avalant-pop-fade{from{opacity:0}to{opacity:1}}
      .avalant-popup-card{
        position:relative;
        background:var(--surface,#131217);
        border:1px solid var(--border,#22222A);
        border-radius:16px;
        padding:30px 30px 26px;
        max-width:440px;
        width:calc(100% - 32px);
        box-shadow:0 18px 60px rgba(0,0,0,0.45);
        font-family:'Inter',sans-serif;
        animation:avalant-pop-rise .22s cubic-bezier(0.16,1,0.3,1);
      }
      @keyframes avalant-pop-rise{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}
      .avalant-popup-close{
        position:absolute;top:14px;right:14px;
        background:transparent;border:0;color:var(--text3,#676B7E);
        cursor:pointer;font-size:18px;width:28px;height:28px;
        display:flex;align-items:center;justify-content:center;
        border-radius:6px;transition:.18s;
      }
      .avalant-popup-close:hover{background:var(--surface3,#202028);color:var(--text,#E6E8E3)}
      .avalant-popup-title{
        font-size:22px;font-weight:700;letter-spacing:-0.01em;
        margin:0 32px 10px 0;color:var(--text,#E6E8E3);
      }
      .avalant-popup-body{
        font-size:14px;line-height:1.55;color:var(--text2,#9B9FAB);
        margin:0 0 22px;white-space:pre-wrap;
      }
      .avalant-popup-cta{
        display:inline-block;
        background:var(--green,#1AFFAB);color:#000;
        padding:12px 22px;border-radius:9px;
        font-weight:700;font-size:14px;text-decoration:none;
        font-family:inherit;border:0;cursor:pointer;
        transition:.18s;
      }
      .avalant-popup-cta:hover{filter:brightness(1.06)}
      body.light .avalant-popup-card{background:#FFFFFF;border-color:#BABABA}
      body.light .avalant-popup-title{color:#000}
      body.light .avalant-popup-body{color:#1A1A1A}
      body.light .avalant-popup-cta{background:#006B3C;color:#FFF}
    `;
    const tag = document.createElement('style');
    tag.id = 'avalant-popup-style';
    tag.textContent = css;
    (document.head || document.documentElement).appendChild(tag);
  }

  function _esc(s){
    return (s ?? '').toString().replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
  }

  let _shown = null;
  let _queue = [];

  function show(popup){
    if (_shown) return;
    injectStyles();
    _shown = popup;
    const url = popup.button_url || '/pricing';
    const text = popup.button_text || 'View pricing';
    const backdrop = document.createElement('div');
    backdrop.className = 'avalant-popup-backdrop';
    backdrop.innerHTML = `
      <div class="avalant-popup-card" role="dialog" aria-modal="true" aria-labelledby="avalant-popup-title-${popup.id}">
        <button class="avalant-popup-close" aria-label="Close">×</button>
        <h3 class="avalant-popup-title" id="avalant-popup-title-${popup.id}">${_esc(popup.title)}</h3>
        <p class="avalant-popup-body">${_esc(popup.body)}</p>
        <a class="avalant-popup-cta" href="${_esc(url)}">${_esc(text)}</a>
      </div>
    `;
    document.body.appendChild(backdrop);
    const close = () => {
      _dismiss(popup.id).finally(() => {
        backdrop.remove();
        _shown = null;
        if (_queue.length){ const next = _queue.shift(); show(next); }
      });
    };
    backdrop.querySelector('.avalant-popup-close').addEventListener('click', close);
    backdrop.addEventListener('click', e => { if (e.target === backdrop) close(); });
  }

  async function _dismiss(id){
    if (!window.Auth || !Auth.isLoggedIn()) return;
    try {
      await Auth.apiFetch(`/popups/${id}/dismiss`, { method: 'POST' });
    } catch {}
  }

  async function load(){
    if (!window.Auth || !Auth.isLoggedIn()) return;
    try {
      const r = await Auth.apiFetch('/popups/pending');
      if (!r.ok) return;
      const j = await r.json();
      const popups = j.popups || [];
      if (!popups.length) return;
      _queue = popups.slice(1);
      show(popups[0]);
    } catch {}
  }

  function start(){
    if (document.readyState === 'loading'){
      document.addEventListener('DOMContentLoaded', load, { once: true });
    } else {
      load();
    }
  }

  start();
  window.AvalantPopup = { reload: load };
})();
