// Shared toast notifications — Avalant
// Usage:
//   toast('Message')                         → info
//   toast('Done', 'success')                 → success
//   toast('Failed', 'error')                 → error
//   toast('Title', 'success', 'subtitle')    → with subtitle
//   toast({title, sub, type, duration})      → object form

(function(){
  if (window.toast && window.toast._avalant) return;

  // Inject CSS once
  const CSS = `
.av-toast-host{position:fixed;top:20px;right:20px;display:flex;flex-direction:column;gap:10px;z-index:10000;pointer-events:none;max-width:380px;}
.av-toast{background:var(--surface,#131217);border:1px solid var(--border,#22222A);border-radius:10px;padding:12px 14px;font-size:12px;color:var(--text,#E6E8E3);box-shadow:0 12px 32px rgba(0,0,0,.5),0 0 0 1px rgba(255,255,255,.03);display:flex;align-items:center;gap:10px;opacity:0;pointer-events:auto;transition:opacity .25s,transform .28s cubic-bezier(.16,1,.3,1);max-width:380px;transform:translateX(20px);font-family:Inter,sans-serif;line-height:1.35;}
.av-toast.show{opacity:1;transform:translateX(0);}
.av-toast.hide{opacity:0;transform:translateX(20px);}
.av-toast-icon-wrap{width:32px;height:32px;border-radius:8px;display:flex;align-items:center;justify-content:center;flex-shrink:0;background:rgba(155,159,171,.12);color:var(--text2,#9B9FAB);}
.av-toast-body{flex:1;display:flex;flex-direction:column;gap:3px;min-width:0;}
.av-toast-title{font-weight:700;font-size:12.5px;color:var(--text,#E6E8E3);letter-spacing:-.01em;}
.av-toast-sub{font-size:10.5px;color:var(--text3,#676B7E);line-height:1.4;}
.av-toast-sub .mono{font-family:'JetBrains Mono',monospace;color:var(--text2,#9B9FAB);font-weight:600;}
.av-toast-close{background:none;border:none;color:var(--text3,#676B7E);cursor:pointer;padding:2px 4px;border-radius:4px;font-size:16px;line-height:1;margin-left:4px;transition:color .15s,background .15s;}
.av-toast-close:hover{color:var(--text,#E6E8E3);background:rgba(255,255,255,.06);}
.av-toast.success{border-color:var(--green,#1AFFAB);}
.av-toast.success .av-toast-icon-wrap{background:rgba(26,255,171,.12);color:var(--green,#1AFFAB);animation:avToastRing 1.1s ease-out;}
.av-toast.error{border-color:var(--red,#F87171);}
.av-toast.error .av-toast-icon-wrap{background:rgba(248,113,113,.12);color:var(--red,#F87171);}
.av-toast.warn{border-color:var(--yellow,#E5C07B);}
.av-toast.warn .av-toast-icon-wrap{background:rgba(229,192,123,.12);color:var(--yellow,#E5C07B);}
body.light .av-toast{background:#FFFFFF;box-shadow:0 10px 28px rgba(0,0,0,.14);}
body.light .av-toast.success .av-toast-icon-wrap{background:#F0F7F3;}
body.light .av-toast.error .av-toast-icon-wrap{background:#FBECEC;}
body.light .av-toast.warn .av-toast-icon-wrap{background:#FBF5E7;}
@keyframes avToastRing{0%{box-shadow:0 0 0 0 rgba(26,255,171,.4)}100%{box-shadow:0 0 0 10px rgba(26,255,171,0)}}
`;
  const style = document.createElement('style');
  style.id = 'av-toast-style';
  style.textContent = CSS;
  document.head.appendChild(style);

  function ensureHost(){
    let h = document.getElementById('av-toast-host');
    if (!h){
      h = document.createElement('div');
      h.id = 'av-toast-host';
      h.className = 'av-toast-host';
      document.body.appendChild(h);
    }
    return h;
  }

  const ICONS = {
    success: '<svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8.5l3.5 3.5L13 5"/></svg>',
    error:   '<svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="8" cy="8" r="6.5"/><path d="M8 5v4M8 11v.5"/></svg>',
    warn:    '<svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2l6.5 11.5h-13L8 2z"/><path d="M8 7v3M8 11.5v.5"/></svg>',
    info:    '<svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="8" cy="8" r="6.5"/><path d="M8 7v4M8 5v.5"/></svg>',
  };

  function show(a, b, c){
    let opts;
    if (typeof a === 'object' && a !== null){
      opts = a;
    } else {
      opts = { title: a, type: b || 'info', sub: c || '' };
    }
    const type = opts.type || 'info';
    const title = opts.title || '';
    const sub = opts.sub || '';
    const duration = opts.duration != null ? opts.duration : (type === 'success' ? 4000 : 3200);

    const host = ensureHost();
    const el = document.createElement('div');
    el.className = 'av-toast ' + type;
    el.innerHTML = `
      <div class="av-toast-icon-wrap">${ICONS[type] || ICONS.info}</div>
      <div class="av-toast-body">
        <div class="av-toast-title">${escape(title)}</div>
        ${sub ? `<div class="av-toast-sub">${sub}</div>` : ''}
      </div>
      <button class="av-toast-close" aria-label="Close">×</button>
    `;
    host.appendChild(el);
    requestAnimationFrame(() => el.classList.add('show'));

    const dismiss = () => {
      el.classList.add('hide');
      el.classList.remove('show');
      setTimeout(() => el.remove(), 300);
    };
    el.querySelector('.av-toast-close').addEventListener('click', dismiss);
    if (duration > 0) setTimeout(dismiss, duration);
    return dismiss;
  }

  function escape(s){
    return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  show._avalant = true;
  window.toast = show;
})();
