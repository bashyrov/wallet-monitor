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
        background:
          radial-gradient(ellipse at center, rgba(26,255,171,0.08), transparent 60%),
          rgba(8,8,11,0.72);
        backdrop-filter:blur(8px) saturate(140%);
        -webkit-backdrop-filter:blur(8px) saturate(140%);
        display:flex;align-items:center;justify-content:center;
        padding:18px;
        animation:avalant-pop-fade .22s ease-out;
      }
      @keyframes avalant-pop-fade{from{opacity:0}to{opacity:1}}
      .avalant-popup-card{
        position:relative;
        background:linear-gradient(180deg, #16161B 0%, #121116 100%);
        border:1px solid #2A2A33;
        border-radius:18px;
        padding:0;
        max-width:460px;
        width:100%;
        box-shadow:
          0 30px 80px rgba(0,0,0,0.55),
          0 0 0 1px rgba(255,255,255,0.02) inset,
          0 0 60px rgba(26,255,171,0.08);
        font-family:'Inter',system-ui,sans-serif;
        animation:avalant-pop-rise .26s cubic-bezier(0.16,1,0.3,1);
        overflow:hidden;
      }
      .avalant-popup-card::before{
        content:'';position:absolute;top:0;left:0;right:0;height:2px;
        background:linear-gradient(90deg, transparent, #1AFFAB 50%, transparent);
        opacity:.85;
      }
      .avalant-popup-card::after{
        content:'';position:absolute;top:-40%;right:-30%;width:280px;height:280px;
        background:radial-gradient(circle, rgba(26,255,171,0.12), transparent 60%);
        pointer-events:none;
      }
      @keyframes avalant-pop-rise{
        from{opacity:0;transform:translateY(16px) scale(.98)}
        to{opacity:1;transform:translateY(0) scale(1)}
      }
      .avalant-popup-inner{position:relative;padding:26px 28px 24px;z-index:1}
      .avalant-popup-head{
        display:flex;align-items:center;gap:10px;
        margin-bottom:16px;
      }
      .avalant-popup-id{
        display:inline-flex;align-items:center;gap:6px;
        padding:4px 9px;border-radius:999px;
        background:rgba(26,255,171,0.10);
        border:1px solid rgba(26,255,171,0.25);
        color:#1AFFAB;
        font-family:'JetBrains Mono','Menlo',monospace;
        font-size:11px;font-weight:600;letter-spacing:.04em;
      }
      .avalant-popup-id::before{
        content:'';width:6px;height:6px;border-radius:50%;
        background:#1AFFAB;box-shadow:0 0 6px rgba(26,255,171,.7);
        animation:avalant-pop-pulse 2.2s ease-in-out infinite;
      }
      @keyframes avalant-pop-pulse{
        0%,100%{opacity:.55}
        50%{opacity:1}
      }
      .avalant-popup-tag{
        font-size:10px;font-weight:700;letter-spacing:.12em;
        text-transform:uppercase;color:#676B7E;
      }
      .avalant-popup-close{
        position:absolute;top:14px;right:14px;
        background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.06);
        color:#9B9FAB;cursor:pointer;
        width:30px;height:30px;display:flex;align-items:center;justify-content:center;
        border-radius:8px;
        transition:background .18s, color .18s, border-color .18s, transform .12s;
        z-index:2;
      }
      .avalant-popup-close svg{width:14px;height:14px}
      .avalant-popup-close:hover{
        background:rgba(255,255,255,0.08);
        color:#E6E8E3;
        border-color:rgba(255,255,255,0.10);
      }
      .avalant-popup-close:active{transform:scale(.94)}
      .avalant-popup-title{
        font-size:22px;font-weight:700;letter-spacing:-0.015em;line-height:1.25;
        margin:0 36px 8px 0;color:#E6E8E3;
      }
      .avalant-popup-body{
        font-size:14px;line-height:1.6;color:#9B9FAB;
        margin:0 0 22px;white-space:pre-wrap;
      }
      .avalant-popup-foot{
        display:flex;align-items:center;justify-content:space-between;gap:14px;
        padding-top:18px;border-top:1px solid rgba(255,255,255,0.05);
      }
      .avalant-popup-cta{
        display:inline-flex;align-items:center;gap:8px;
        background:#1AFFAB;color:#08090C;
        padding:11px 18px;border-radius:9px;
        font-weight:700;font-size:13.5px;letter-spacing:-.005em;
        text-decoration:none;font-family:inherit;border:0;cursor:pointer;
        transition:background .18s, box-shadow .18s, transform .08s;
      }
      .avalant-popup-cta:hover{
        background:#00E89A;
        box-shadow:0 0 0 1px rgba(26,255,171,.35), 0 8px 22px rgba(26,255,171,.22);
      }
      .avalant-popup-cta:active{transform:translateY(1px)}
      .avalant-popup-cta svg{width:14px;height:14px}
      .avalant-popup-skip{
        background:transparent;border:0;cursor:pointer;
        color:#676B7E;font:inherit;font-size:12.5px;
        padding:6px 8px;border-radius:6px;
        transition:color .18s, background .18s;
      }
      .avalant-popup-skip:hover{color:#9B9FAB;background:rgba(255,255,255,0.03)}

      /* ── Light theme ── */
      body.light .avalant-popup-backdrop{
        background:radial-gradient(ellipse at center, rgba(0,107,60,0.08), transparent 60%), rgba(0,0,0,0.45);
      }
      body.light .avalant-popup-card{
        background:linear-gradient(180deg, #FFFFFF 0%, #F8F8F8 100%);
        border-color:#D6D6D6;
        box-shadow:0 30px 80px rgba(0,0,0,.18), 0 0 60px rgba(0,107,60,.08);
      }
      body.light .avalant-popup-card::before{background:linear-gradient(90deg, transparent, #006B3C 50%, transparent)}
      body.light .avalant-popup-card::after{background:radial-gradient(circle, rgba(0,107,60,.10), transparent 60%)}
      body.light .avalant-popup-id{background:rgba(0,107,60,.08);border-color:rgba(0,107,60,.30);color:#006B3C}
      body.light .avalant-popup-id::before{background:#006B3C;box-shadow:0 0 6px rgba(0,107,60,.6)}
      body.light .avalant-popup-tag{color:#595959}
      body.light .avalant-popup-close{background:rgba(0,0,0,.04);border-color:rgba(0,0,0,.08);color:#595959}
      body.light .avalant-popup-close:hover{background:rgba(0,0,0,.07);color:#000;border-color:rgba(0,0,0,.12)}
      body.light .avalant-popup-title{color:#000}
      body.light .avalant-popup-body{color:#1A1A1A}
      body.light .avalant-popup-foot{border-top-color:rgba(0,0,0,.07)}
      body.light .avalant-popup-cta{background:#006B3C;color:#FFFFFF}
      body.light .avalant-popup-cta:hover{background:#005A33;box-shadow:0 0 0 1px rgba(0,107,60,.35), 0 8px 22px rgba(0,107,60,.22)}
      body.light .avalant-popup-skip{color:#595959}
      body.light .avalant-popup-skip:hover{color:#000;background:rgba(0,0,0,.04)}

      /* ── Mobile ── */
      @media (max-width:520px){
        .avalant-popup-card{border-radius:14px}
        .avalant-popup-inner{padding:22px 20px 20px}
        .avalant-popup-title{font-size:19px;margin-right:32px}
        .avalant-popup-body{font-size:13.5px}
        .avalant-popup-foot{flex-direction:column-reverse;align-items:stretch;gap:8px}
        .avalant-popup-cta{justify-content:center;padding:13px 18px}
        .avalant-popup-skip{padding:8px}
      }
    `;
    const tag = document.createElement('style');
    tag.id = 'avalant-popup-style';
    tag.textContent = css;
    (document.head || document.documentElement).appendChild(tag);
  }

  function _esc(s){
    return (s ?? '').toString().replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
  }

  function _isAuthed(){
    try { return !!localStorage.getItem('wm_token'); } catch { return false; }
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
        <button class="avalant-popup-close" aria-label="Close">
          <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M3 3l8 8M11 3l-8 8"/></svg>
        </button>
        <div class="avalant-popup-inner">
          <div class="avalant-popup-head">
            <span class="avalant-popup-id">#${popup.id}</span>
            <span class="avalant-popup-tag">Announcement</span>
          </div>
          <h3 class="avalant-popup-title" id="avalant-popup-title-${popup.id}">${_esc(popup.title)}</h3>
          <p class="avalant-popup-body">${_esc(popup.body)}</p>
          <div class="avalant-popup-foot">
            <button class="avalant-popup-skip" type="button">Maybe later</button>
            <a class="avalant-popup-cta" href="${_esc(url)}">
              ${_esc(text)}
              <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7h8M7.5 3.5L11 7l-3.5 3.5"/></svg>
            </a>
          </div>
        </div>
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
    backdrop.querySelector('.avalant-popup-skip').addEventListener('click', close);
    backdrop.addEventListener('click', e => { if (e.target === backdrop) close(); });
  }

  async function _dismiss(id){
    if (!_isAuthed()) return;
    try {
      await Auth.apiFetch(`/popups/${id}/dismiss`, { method: 'POST' });
    } catch {}
  }

  async function load(){
    if (!_isAuthed()) return;
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
