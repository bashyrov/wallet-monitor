// Avalant shared confirm dialog — replaces native confirm()/alert().
// Usage:
//   const ok = await Confirm.ask({ title, message, okText, cancelText, danger })
//   await Confirm.tell({ title, message, okText })    // alert-like (no cancel)
//
// Dependencies: none. Injects its own CSS once.

(function(){
  if (window.Confirm) return;

  const CSS = `
.av-cf-backdrop{position:fixed;inset:0;background:rgba(0,0,0,0.58);backdrop-filter:blur(5px);-webkit-backdrop-filter:blur(5px);display:flex;align-items:center;justify-content:center;z-index:9500;opacity:0;pointer-events:none;transition:opacity .15s;}
.av-cf-backdrop.open{opacity:1;pointer-events:auto;}
.av-cf-box{width:min(420px,calc(100vw - 28px));background:var(--surface,#131217);border:1px solid var(--border,#22222A);border-radius:13px;box-shadow:0 24px 68px rgba(0,0,0,0.6);transform:translateY(8px) scale(0.98);transition:transform .16s cubic-bezier(.16,1,.3,1);overflow:hidden;font-family:Inter,system-ui,sans-serif;}
.av-cf-backdrop.open .av-cf-box{transform:translateY(0) scale(1);}
.av-cf-hdr{padding:16px 18px 6px;display:flex;flex-direction:column;gap:10px;}
.av-cf-ic{width:40px;height:40px;border-radius:10px;display:inline-flex;align-items:center;justify-content:center;background:rgba(229,192,123,0.1);color:var(--yellow,#E5C07B);border:1px solid rgba(229,192,123,0.3);}
.av-cf-hdr.danger .av-cf-ic{background:rgba(248,113,113,0.1);color:var(--red,#F87171);border-color:rgba(248,113,113,0.3);}
.av-cf-hdr.info .av-cf-ic{background:rgba(26,255,171,0.1);color:var(--green,#1AFFAB);border-color:rgba(26,255,171,0.3);}
.av-cf-title{font-size:15px;font-weight:700;letter-spacing:-0.01em;margin:0;color:var(--text,#E6E8E3);}
.av-cf-msg{padding:0 18px 14px;font-size:13px;color:var(--text2,#9B9FAB);line-height:1.55;}
.av-cf-msg b{color:var(--text,#E6E8E3);font-weight:600;}
.av-cf-msg code{font-family:'JetBrains Mono',monospace;font-size:12px;background:var(--surface2,#17171C);padding:1px 5px;border-radius:4px;color:var(--text,#E6E8E3);}
.av-cf-actions{display:flex;justify-content:flex-end;gap:8px;padding:12px 18px 18px;background:var(--surface2,#17171C);border-top:1px solid var(--border,#22222A);}
.av-cf-btn{padding:9px 16px;border-radius:7px;font-family:inherit;font-weight:700;font-size:12.5px;cursor:pointer;border:1px solid transparent;letter-spacing:-0.01em;min-width:88px;transition:box-shadow .15s,background .15s,color .15s;}
.av-cf-btn-ghost{background:transparent;border-color:var(--border,#22222A);color:var(--text2,#9B9FAB);}
.av-cf-btn-ghost:hover{color:var(--text,#E6E8E3);border-color:var(--border2,#3A3A50);}
.av-cf-btn-primary{background:var(--green,#1AFFAB);color:#09090B;}
.av-cf-btn-primary:hover{box-shadow:0 0 16px rgba(26,255,171,0.3);}
.av-cf-btn-danger{background:var(--red,#F87171);color:#140000;}
.av-cf-btn-danger:hover{box-shadow:0 0 16px rgba(248,113,113,0.35);}
`;
  const s = document.createElement('style'); s.id='av-cf-style'; s.textContent=CSS; document.head.appendChild(s);

  const ICONS = {
    default: '<svg width="20" height="20" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2l6 11H2L8 2z"/><path d="M8 7v3"/><circle cx="8" cy="12.2" r="0.6" fill="currentColor"/></svg>',
    danger:  '<svg width="20" height="20" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M3 5h10M6 5V3.5a1 1 0 011-1h2a1 1 0 011 1V5m-4 0v8a1 1 0 001 1h2a1 1 0 001-1V5"/></svg>',
    info:    '<svg width="20" height="20" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="8" cy="8" r="6"/><path d="M8 5v.5M8 7.5v3.5"/></svg>',
  };

  let root = null, resolver = null;

  function ensureRoot(){
    if (root) return root;
    root = document.createElement('div');
    root.className = 'av-cf-backdrop';
    root.innerHTML = `
      <div class="av-cf-box" role="dialog" aria-modal="true">
        <div class="av-cf-hdr" data-av-cf-hdr>
          <span class="av-cf-ic" data-av-cf-ic>${ICONS.default}</span>
          <h3 class="av-cf-title" data-av-cf-title>Confirm</h3>
        </div>
        <div class="av-cf-msg" data-av-cf-msg></div>
        <div class="av-cf-actions">
          <button type="button" class="av-cf-btn av-cf-btn-ghost" data-av-cf-cancel>Cancel</button>
          <button type="button" class="av-cf-btn av-cf-btn-primary" data-av-cf-ok>OK</button>
        </div>
      </div>`;
    document.body.appendChild(root);
    root.addEventListener('click', (e) => { if (e.target === root) close(false); });
    root.querySelector('[data-av-cf-cancel]').addEventListener('click', () => close(false));
    root.querySelector('[data-av-cf-ok]').addEventListener('click', () => close(true));
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && root.classList.contains('open')) close(false); });
    return root;
  }

  function close(result){
    if (!root) return;
    root.classList.remove('open');
    const r = resolver; resolver = null;
    if (r) r(result);
  }

  function ask(opts = {}){
    ensureRoot();
    const title = opts.title || 'Confirm';
    const message = opts.message || '';
    const okText = opts.okText || 'OK';
    const cancelText = opts.cancelText;  // if empty string, hide cancel (alert-like)
    const kind = opts.danger ? 'danger' : (opts.info ? 'info' : 'default');
    const hdr = root.querySelector('[data-av-cf-hdr]');
    hdr.classList.remove('danger','info'); if (kind !== 'default') hdr.classList.add(kind);
    // Icon is OFF by default — the title + danger button colour carry the
    // intent on their own and the trash/warn icon read as visual noise on
    // close-position confirmations. Set opts.icon = true to opt back in.
    const ic = root.querySelector('[data-av-cf-ic]');
    if (opts.icon === true) { ic.style.display = ''; ic.innerHTML = ICONS[kind] || ICONS.default; }
    else { ic.style.display = 'none'; }
    root.querySelector('[data-av-cf-title]').textContent = title;
    root.querySelector('[data-av-cf-msg]').innerHTML = message;
    const okBtn = root.querySelector('[data-av-cf-ok]');
    okBtn.textContent = okText;
    okBtn.className = 'av-cf-btn ' + (opts.danger ? 'av-cf-btn-danger' : 'av-cf-btn-primary');
    const cancelBtn = root.querySelector('[data-av-cf-cancel]');
    if (cancelText === '' || cancelText === null) { cancelBtn.style.display = 'none'; }
    else { cancelBtn.style.display = ''; cancelBtn.textContent = cancelText || 'Cancel'; }
    root.classList.add('open');
    setTimeout(() => okBtn.focus(), 30);
    return new Promise(resolve => { resolver = resolve; });
  }

  function tell(opts = {}){ return ask({ ...opts, cancelText: '' }); }

  window.Confirm = { ask, tell };
})();
