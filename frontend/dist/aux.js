(function(){"use strict";const o="avalant-site-banner",p="avalant-banner-style";let n=null,s=null;function g(){if(document.getElementById(p))return;const r=`
      #${o} {
        position: fixed; top: 0; left: 0; right: 0; z-index: 1100;
        background: linear-gradient(90deg, #1AFFAB 0%, #00E89A 100%);
        color: #08090C;
        font-family: 'Inter', system-ui, -apple-system, sans-serif;
        font-weight: 700; font-size: 13.5px; letter-spacing: -0.005em;
        line-height: 1.35;
        height: 36px; box-sizing: border-box;
        padding: 0 16px;
        display: flex; align-items: center; justify-content: center;
        white-space: nowrap; overflow: hidden;
        box-shadow: 0 1px 0 rgba(0, 0, 0, 0.18), 0 4px 16px rgba(26, 255, 171, 0.15);
        animation: avalant-banner-slide-in 0.28s cubic-bezier(0.16, 1, 0.3, 1);
      }
      @keyframes avalant-banner-slide-in {
        from { transform: translateY(-100%); opacity: 0; }
        to   { transform: translateY(0);     opacity: 1; }
      }
      #${o} .b-text {
        display: inline-block; max-width: 100%;
        text-overflow: ellipsis; overflow: hidden;
      }
      #${o}.marquee {
        justify-content: flex-start;
      }
      #${o}.marquee .b-track {
        display: inline-flex; gap: 64px; align-items: center;
        white-space: nowrap;
        animation: avalant-banner-scroll 28s linear infinite;
        will-change: transform;
      }
      #${o}.marquee .b-track .b-text {
        display: inline-block; max-width: none;
      }
      @keyframes avalant-banner-scroll {
        from { transform: translateX(0); }
        to   { transform: translateX(-50%); }
      }
      body.has-site-banner { padding-top: 36px !important; }
      /* Maintenance + landing pages center their content with min-height:100vh
         \u2014 push that down by the banner height so the card doesn't crowd up. */
      body.has-site-banner .wrap { min-height: calc(100vh - 36px) !important; }
      @media (max-width: 560px) {
        #${o} { font-size: 12.5px; height: 32px; padding: 0 12px; }
        body.has-site-banner { padding-top: 32px !important; }
        body.has-site-banner .wrap { min-height: calc(100vh - 32px) !important; }
      }
    `,e=document.createElement("style");e.id=p,e.textContent=r,(document.head||document.documentElement).appendChild(e)}function l(r){return(r==null?"":String(r)).replace(/[&<>"']/g,function(e){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[e]})}function i(){const r=document.getElementById(o);r&&r.remove(),document.body.classList.remove("has-site-banner"),n=null}function h(r){if(!r||!r.enabled||!r.text){i();return}if(n&&n.enabled===r.enabled&&n.text===r.text&&n.marquee===r.marquee)return;g();let e=document.getElementById(o);e||(e=document.createElement("div"),e.id=o,document.body.firstChild?document.body.insertBefore(e,document.body.firstChild):document.body.appendChild(e),document.body.classList.add("has-site-banner"));const t=l(r.text);r.marquee?(e.classList.add("marquee"),e.innerHTML='<div class="b-track"><span class="b-text">'+t+'</span><span class="b-text" aria-hidden="true">'+t+"</span></div>"):(e.classList.remove("marquee"),e.innerHTML='<span class="b-text">'+t+"</span>"),n=r}async function d(){try{const r=await fetch("/api/banner",{cache:"no-store"});if(!r.ok)return;const e=await r.json();h(e)}catch{}}function a(){d(),s=setInterval(d,6e4)}document.readyState==="loading"?document.addEventListener("DOMContentLoaded",a,{once:!0}):a(),window.AvalantBanner={reload:d}})(),(function(){function u(){if(document.getElementById("avalant-popup-style"))return;const e=`
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

      /* \u2500\u2500 Light theme \u2500\u2500 */
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

      /* \u2500\u2500 Mobile \u2500\u2500 */
      @media (max-width:520px){
        .avalant-popup-card{border-radius:14px}
        .avalant-popup-inner{padding:22px 20px 20px}
        .avalant-popup-title{font-size:19px;margin-right:32px}
        .avalant-popup-body{font-size:13.5px}
        .avalant-popup-foot{flex-direction:column-reverse;align-items:stretch;gap:8px}
        .avalant-popup-cta{justify-content:center;padding:13px 18px}
        .avalant-popup-skip{padding:8px}
      }
    `,t=document.createElement("style");t.id="avalant-popup-style",t.textContent=e,(document.head||document.documentElement).appendChild(t)}function o(e){return(e??"").toString().replace(/[&<>"']/g,t=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[t])}function p(){try{return!!localStorage.getItem("wm_token")}catch{return!1}}let n=null,s=[];function g(e){if(n)return;u(),n=e;const t=e.button_url||"/pricing",b=e.button_text||"View pricing",c=document.createElement("div");c.className="avalant-popup-backdrop",c.innerHTML=`
      <div class="avalant-popup-card" role="dialog" aria-modal="true" aria-labelledby="avalant-popup-title-${e.id}">
        <button class="avalant-popup-close" aria-label="Close">
          <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M3 3l8 8M11 3l-8 8"/></svg>
        </button>
        <div class="avalant-popup-inner">
          <div class="avalant-popup-head">
            <span class="avalant-popup-id">#${e.id}</span>
            <span class="avalant-popup-tag">Announcement</span>
          </div>
          <h3 class="avalant-popup-title" id="avalant-popup-title-${e.id}">${o(e.title)}</h3>
          <p class="avalant-popup-body">${o(e.body)}</p>
          <div class="avalant-popup-foot">
            <button class="avalant-popup-skip" type="button">Maybe later</button>
            <a class="avalant-popup-cta" href="${o(t)}">
              ${o(b)}
              <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7h8M7.5 3.5L11 7l-3.5 3.5"/></svg>
            </a>
          </div>
        </div>
      </div>
    `,document.body.appendChild(c);const f=()=>{d(e.id).finally(()=>{if(c.remove(),n=null,s.length){const x=s.shift();g(x)}})};c.querySelector(".avalant-popup-close").addEventListener("click",f),c.querySelector(".avalant-popup-skip").addEventListener("click",f),c.addEventListener("click",x=>{x.target===c&&f()})}const l="avalant_popup_anon_dismissed";function i(){try{const e=localStorage.getItem(l),t=e?JSON.parse(e):[];return Array.isArray(t)?new Set(t.map(Number)):new Set}catch{return new Set}}function h(e){try{localStorage.setItem(l,JSON.stringify([...e]))}catch{}}async function d(e){if(p())try{await Auth.apiFetch(`/popups/${e}/dismiss`,{method:"POST"})}catch{}else{const t=i();t.add(Number(e)),h(t)}}async function a(){try{const e=p()?{headers:{Authorization:"Bearer "+localStorage.getItem("wm_token")}}:{},t=await fetch("/api/popups/pending",e);if(!t.ok)return;let c=(await t.json()).popups||[];if(!p()&&c.length){const f=i();c=c.filter(x=>!f.has(Number(x.id)))}if(!c.length)return;s=c.slice(1),g(c[0])}catch{}}function r(){document.readyState==="loading"?document.addEventListener("DOMContentLoaded",a,{once:!0}):a()}r(),window.AvalantPopup={reload:a}})(),(function(){function u(){try{return!!localStorage.getItem("wm_token")}catch{return!1}}if(!u())return;const o="expiry_banner_dismissed_until",p=1440*60*1e3;function n(){try{const l=parseInt(localStorage.getItem(o)||"0",10);return Number.isFinite(l)&&Date.now()<l}catch{return!1}}async function s(){if(n())return;let l=null;try{const d=await fetch("/api/auth/me",{headers:{Authorization:"Bearer "+localStorage.getItem("wm_token")}});if(!d.ok)return;l=await d.json()}catch{return}if(!l||!l.plan_expires_at)return;const i=new Date(l.plan_expires_at);if(Number.isNaN(i.getTime()))return;const h=Math.round((i.getTime()-Date.now())/864e5);h>7||g(l,h)}function g(l,i){if(document.getElementById("expiry-banner"))return;let h,d;i<=0?(h="#F87171",d=`Your <b>${l.plan_slug||l.plan||"plan"}</b> plan expired \u2014 portfolio scan downgraded. <a href="/pricing" style="color:#fff;text-decoration:underline">Renew</a>`):i<=3?(h="#E5C07B",d=`Your <b>${l.plan_slug||l.plan||"plan"}</b> plan ends in <b>${i} day${i===1?"":"s"}</b>. <a href="/pricing" style="color:#000;text-decoration:underline">Renew now</a>`):(h="#06B6D4",d=`Your <b>${l.plan_slug||l.plan||"plan"}</b> plan renews in <b>${i} days</b>. <a href="/pricing" style="color:#fff;text-decoration:underline">Manage</a>`);const a=document.createElement("div");a.id="expiry-banner",a.style.cssText=`
      position:sticky; top:0; z-index:200;
      background:${h}; color:${i<=3&&i>0?"#000":"#fff"};
      padding:9px 16px; text-align:center; font-size:13px; font-weight:500;
      font-family:'Inter',sans-serif;
      box-shadow:0 2px 12px rgba(0,0,0,0.12);
      display:flex; justify-content:center; align-items:center; gap:14px;
    `,a.innerHTML=`
      <span>${d}</span>
      <button id="expiry-banner-close" aria-label="Dismiss"
        style="background:transparent;border:0;color:inherit;font-size:18px;cursor:pointer;line-height:1;padding:0 6px;opacity:0.75">\xD7</button>
    `,document.body.insertBefore(a,document.body.firstChild),document.getElementById("expiry-banner-close").addEventListener("click",()=>{try{localStorage.setItem(o,String(Date.now()+p))}catch{}a.remove()})}document.readyState==="loading"?document.addEventListener("DOMContentLoaded",s,{once:!0}):s()})();const _ICONS={portfolio:'<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><rect x="1" y="6" width="3" height="5" rx="0.7" fill="currentColor" opacity=".5"/><rect x="4.5" y="3.5" width="3" height="7.5" rx="0.7" fill="currentColor" opacity=".75"/><rect x="8" y="1" width="3" height="10" rx="0.7" fill="currentColor"/></svg>',archive:'<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><rect x="1" y="4" width="10" height="7" rx="1" stroke="currentColor" stroke-width="1.35"/><path d="M1 4l1.5-2.5h7L11 4" stroke="currentColor" stroke-width="1.35" stroke-linejoin="round"/><path d="M4.5 6.5h3" stroke="currentColor" stroke-width="1.35" stroke-linecap="round"/></svg>',screener:'<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M1.5 6h9M1.5 3h9M1.5 9h5" stroke="currentColor" stroke-width="1.35" stroke-linecap="round"/></svg>',pricing:'<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M1.5 1.5h3.8l5 5-3.8 3.8-5-5V1.5z" stroke="currentColor" stroke-width="1.35" stroke-linejoin="round"/><circle cx="4" cy="4" r="0.9" fill="currentColor"/></svg>',watchlist:`<svg width="13" height="13" viewBox="0 0 14 14" fill="none"><defs><linearGradient id="wl-g-${Math.random().toString(36).slice(2,7)}" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="currentColor"/><stop offset="1" stop-color="currentColor" stop-opacity="0.55"/></linearGradient></defs><path d="M7 1.3l1.85 3.75 4.15.6-3 2.93.71 4.13L7 10.77 3.29 12.7 4 8.57l-3-2.92 4.15-.6z" fill="currentColor" stroke="currentColor" stroke-width="0.8" stroke-linejoin="round"/></svg>`,login:'<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M8 2H10a1 1 0 011 1v6a1 1 0 01-1 1H8M5 9l3-3-3-3M1 6h7" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"/></svg>'},_ALL_LINKS=[{id:"app",href:"/app",label:"Portfolio",icon:_ICONS.portfolio,authOnly:!1},{id:"archive",href:"/archive",label:"Archive",icon:_ICONS.archive,authOnly:!1},{id:"screener",href:"/screener",label:"Screener",icon:_ICONS.screener,authOnly:!1},{id:"watchlist",href:"/watchlist",label:"Watchlist",icon:_ICONS.watchlist,authOnly:!1},{id:"pricing",href:"/pricing",label:"Pricing",icon:_ICONS.pricing,authOnly:!1}],_NAV_SET={app:["app","archive","screener","pricing"],archive:["app","archive","screener","pricing"],profile:["app","archive","screener","pricing"],index:["app","archive","screener","pricing"],pricing:["app","archive","screener","pricing"],screener:["app","pricing"],arb:["app","pricing"],watchlist:["app","pricing"],login:[],register:[],checkout:["app","pricing"]},_ACTIVE={app:"app",screener:"screener",archive:"archive",pricing:"pricing",watchlist:"watchlist",profile:null,index:null,login:null,register:null,checkout:null,arb:"screener"};function _navLink(u,o){const p="nav-lnk"+(u.id===o?" active":"");return`<a href="${u.href}" class="${p}">${u.icon}${u.label}</a>`}function _avatarBtn(){return'<a href="/profile" class="avatar-btn" id="nav-avatar" title="Profile">U</a>'}function _rightHtml(u){switch(u){case"app":return`<button class="btn btn-primary btn-sm" onclick="openAddWalletModal()">+ Add Wallet</button>${_avatarBtn()}`;case"archive":case"profile":case"checkout":return _avatarBtn();case"screener":case"arb":case"watchlist":{const o=u==="watchlist"?" active":"";return`
        <div id="_nb-guest" style="display:flex;align-items:center;gap:8px">
          <a href="/login" class="nav-lnk">${_ICONS.login}Sign In</a>
          <a href="/register" class="btn btn-primary btn-sm">Get Started</a>
        </div>
        <div id="_nb-user" style="display:none;align-items:center;gap:8px">
          <a href="/watchlist" class="nav-lnk nav-lnk-icon${o}" title="Watchlist" aria-label="Watchlist">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor" stroke="currentColor" stroke-width="0.8" stroke-linejoin="round"><path d="M7 1.3l1.85 3.75 4.15.6-3 2.93.71 4.13L7 10.77 3.29 12.7 4 8.57l-3-2.92 4.15-.6z"/></svg>
          </a>
          <button class="nav-lnk nav-lnk-bell" onclick="openAlertsPopover(event)" title="Alerts" aria-label="Alerts">
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2a5 5 0 0 1 5 5v3l1 2H2l1-2V7a5 5 0 0 1 5-5z"/><path d="M6.5 13.5a1.5 1.5 0 0 0 3 0"/></svg>
            <span class="nav-dot" id="nb-alerts-dot" style="display:none"></span>
          </button>
          ${_avatarBtn()}
        </div>`}case"index":return`
        <div id="_nb-guest" style="display:flex;align-items:center;gap:8px">
          <a href="/login" class="nav-lnk">${_ICONS.login}Sign In</a>
          <a href="/register" class="btn btn-primary btn-sm">Get Started</a>
        </div>
        <div id="_nb-user" style="display:none;align-items:center;gap:8px">
          <a href="/app" class="btn btn-primary btn-sm">Open App</a>
          ${_avatarBtn()}
        </div>`;case"pricing":return`
        <div id="_nb-guest" style="display:flex;align-items:center;gap:8px">
          <a href="/login" class="btn btn-primary btn-sm" id="topbar-cta">Sign In</a>
        </div>
        <div id="_nb-user" style="display:none;align-items:center;gap:8px">
          ${_avatarBtn()}
        </div>`;case"login":return`<a href="/register" class="nav-lnk">${_ICONS.login}Register</a>
              <a href="/app" class="btn btn-primary btn-sm">Open App</a>`;case"register":return`<a href="/login" class="nav-lnk">${_ICONS.login}Sign in</a>
              <a href="/app" class="btn btn-primary btn-sm">Open App</a>`;default:return""}}class AppNavbar extends HTMLElement{connectedCallback(){const o=this.getAttribute("page")||"index",p=_ACTIVE[o]??null,n=_NAV_SET[o]??[],s=_ALL_LINKS.filter(i=>n.includes(i.id)),g=o==="pricing"?["app","archive"]:[],l=s.map(i=>{const h=g.includes(i.id)?' style="display:none"':"",d="nav-lnk"+(i.id===p?" active":"");return`<a href="${i.href}" class="${d}" data-nb-id="${i.id}"${h}>${i.icon}${i.label}</a>`}).join("");this.innerHTML=`
      <a href="/" class="brand">avalant<span class="brand-cursor">_</span></a>
      <nav class="topbar-nav">${l}</nav>
      <div class="topbar-right">${_rightHtml(o)}</div>
    `,this._initAuth(o)}_initAuth(o){if(typeof Auth>"u"){document.addEventListener("DOMContentLoaded",()=>this._applyAuth(o));return}this._applyAuth(o)}_applyAuth(o){if(typeof Auth>"u")return;const p=Auth.isLoggedIn(),n=Auth.getUser();if(p&&n){const s=this.querySelector("#nav-avatar");s&&(s.textContent=(n.username||n.email||"U")[0].toUpperCase())}if(["index","pricing","screener","arb","watchlist"].includes(o)){const s=this.querySelector("#_nb-guest"),g=this.querySelector("#_nb-user");p?(s&&(s.style.display="none"),g&&(g.style.display="flex")):(s&&(s.style.display="flex"),g&&(g.style.display="none"))}p&&this.querySelectorAll("[data-nb-id]").forEach(s=>{s.style.display==="none"&&(s.style.display="")})}}customElements.define("app-navbar",AppNavbar),(function(){if(window.openAlertsPopover)return;const u={binance:"Binance",bybit:"Bybit",okx:"OKX",gate:"Gate",kucoin:"KuCoin",mexc:"MEXC",bitget:"Bitget",hyperliquid:"Hyperliquid",aster:"Aster",ethereal:"Ethereal",whitebit:"WhiteBIT",bingx:"BingX",lighter:"Lighter",paradex:"Paradex"},o=`
.nb-alerts-pop{position:fixed;background:var(--surface,#131217);border:1px solid var(--border,#22222A);border-radius:12px;box-shadow:0 18px 48px rgba(0,0,0,.5);min-width:320px;max-width:380px;max-height:70vh;display:flex;flex-direction:column;z-index:500;overflow:hidden;font-family:Inter,sans-serif;opacity:0;transform:translateY(-4px);transition:opacity .16s,transform .16s;}
.nb-alerts-pop.open{opacity:1;transform:translateY(0);}
.nb-alerts-hdr{display:flex;align-items:center;gap:8px;padding:12px 14px;border-bottom:1px solid var(--border,#22222A);}
.nb-alerts-hdr-title{font-size:13px;font-weight:700;flex:1;letter-spacing:-0.01em;color:var(--text,#E6E8E3);}
.nb-alerts-hdr-count{padding:2px 7px;border-radius:999px;background:var(--surface3,#202028);color:var(--text3,#676B7E);font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:700;}
.nb-alerts-body{flex:1;overflow-y:auto;padding:6px;}
.nb-alert-row{display:flex;align-items:center;gap:8px;padding:9px 10px;border-radius:8px;cursor:pointer;transition:background .12s;text-decoration:none;color:inherit;}
.nb-alert-row:hover{background:var(--surface2,#17171C);}
.nb-alert-sym{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:12.5px;min-width:50px;color:var(--text,#E6E8E3);}
.nb-alert-pair{font-size:11px;color:var(--text3,#676B7E);flex:1;letter-spacing:0.01em;}
.nb-alert-thr{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--yellow,#E5C07B);font-weight:600;background:rgba(229,192,123,0.08);padding:2px 7px;border-radius:6px;}
.nb-alert-toggle{width:30px;height:16px;border-radius:8px;background:var(--surface3,#202028);position:relative;flex-shrink:0;transition:background .15s;cursor:pointer;}
.nb-alert-toggle::after{content:'';position:absolute;top:2px;left:2px;width:12px;height:12px;border-radius:50%;background:var(--text3,#676B7E);transition:transform .16s,background .15s;}
.nb-alert-toggle.on{background:rgba(26,255,171,0.2);}
.nb-alert-toggle.on::after{transform:translateX(14px);background:var(--green,#1AFFAB);}
.nb-alert-del{display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;border:none;background:transparent;color:var(--text3,#676B7E);border-radius:5px;cursor:pointer;flex-shrink:0;transition:color .12s,background .12s;opacity:0;font-family:inherit;}
.nb-alert-row:hover .nb-alert-del{opacity:1;}
.nb-alert-del:hover{color:var(--red,#F87171);background:rgba(248,113,113,0.08);}
.nb-alerts-empty{padding:28px 16px;text-align:center;color:var(--text3,#676B7E);font-size:12.5px;}
.nb-alerts-empty-icon{margin:0 auto 10px;width:38px;height:38px;display:flex;align-items:center;justify-content:center;border-radius:10px;background:var(--surface2,#17171C);color:var(--text3,#676B7E);}
.nb-alerts-empty .nb-hint{color:var(--text2,#9B9FAB);font-size:11.5px;margin-top:4px;}
`,p=document.createElement("style");p.id="nb-alerts-pop-css",p.textContent=o,document.head.appendChild(p);let n=null;async function s(a){if(a&&a.stopPropagation(),n){l();return}const r=a?.currentTarget||document.querySelector(".nav-lnk-bell"),e=r?r.getBoundingClientRect():{right:window.innerWidth-20,bottom:56};n=document.createElement("div"),n.className="nb-alerts-pop",n.innerHTML=`
      <div class="nb-alerts-hdr">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2a5 5 0 0 1 5 5v3l1 2H2l1-2V7a5 5 0 0 1 5-5z"/><path d="M6.5 13.5a1.5 1.5 0 0 0 3 0"/></svg>
        <span class="nb-alerts-hdr-title">Alerts</span>
        <span class="nb-alerts-hdr-count" id="nb-alerts-count">\u2014</span>
      </div>
      <div class="nb-alerts-body" id="nb-alerts-body"><div class="nb-alerts-empty">Loading\u2026</div></div>
    `,document.body.appendChild(n);const t=n.offsetWidth,b=Math.max(12,Math.min(e.right-t,window.innerWidth-t-12)),c=e.bottom+6;n.style.left=b+"px",n.style.top=c+"px",requestAnimationFrame(()=>n.classList.add("open")),setTimeout(()=>document.addEventListener("click",g,{once:!1}),0);try{const f=await Auth.apiFetch("/alerts"),x=f.ok?await f.json():[];i(x)}catch{i([])}}function g(a){n&&!n.contains(a.target)&&!a.target.closest(".nav-lnk-bell")&&l()}function l(){if(!n)return;document.removeEventListener("click",g),n.classList.remove("open");const a=n;n=null,setTimeout(()=>a.remove(),160)}function i(a){if(!n)return;const r=n.querySelector("#nb-alerts-body"),e=n.querySelector("#nb-alerts-count");if(e.textContent=a.length,!a.length){r.innerHTML=`
        <div class="nb-alerts-empty">
          <div class="nb-alerts-empty-icon">
            <svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2a5 5 0 0 1 5 5v3l1 2H2l1-2V7a5 5 0 0 1 5-5z"/><path d="M6.5 13.5a1.5 1.5 0 0 0 3 0"/></svg>
          </div>
          No alerts yet
          <div class="nb-hint">Open a pair on the Screener and tap the alert button to add one.</div>
        </div>`;return}r.innerHTML=a.map(t=>{const b=t.direction==="above"?"\u2265":t.direction==="below"?"\u2264":"\xB1";return`
      <a class="nb-alert-row" href="/arb?symbol=${t.symbol}&long=${t.long_exchange}&short=${t.short_exchange}" target="_blank" data-alert-id="${t.id}">
        <span class="nb-alert-sym">${t.symbol}</span>
        <span class="nb-alert-pair">${u[t.long_exchange]||t.long_exchange} \u2192 ${u[t.short_exchange]||t.short_exchange}</span>
        <span class="nb-alert-thr">${b}${t.threshold.toFixed(3)}%</span>
        <span class="nb-alert-toggle ${t.enabled?"on":""}" title="Enable/disable" onclick="event.preventDefault();event.stopPropagation();_nbToggleAlert(${t.id},this)"></span>
        <button class="nb-alert-del" title="Delete alert" onclick="event.preventDefault();event.stopPropagation();_nbDeleteAlert(${t.id},this)">
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M3 5h10M6 5V3.5a1 1 0 011-1h2a1 1 0 011 1V5m-4 0v8a1 1 0 001 1h2a1 1 0 001-1V5"/></svg>
        </button>
      </a>`}).join("")}async function h(a,r){try{(await Auth.apiFetch(`/alerts/${a}/toggle`,{method:"PATCH"})).ok&&r.classList.toggle("on")}catch{}}async function d(a,r){if(!(window.Confirm&&!await window.Confirm.ask({title:"Delete alert?",message:"This alert will stop triggering Telegram notifications.",okText:"Delete",danger:!0})))try{if(!(await Auth.apiFetch(`/alerts/${a}`,{method:"DELETE"})).ok)throw new Error;const t=r.closest(".nb-alert-row");t&&t.remove();const b=n?.querySelector("#nb-alerts-count");b&&(b.textContent=Math.max(0,parseInt(b.textContent||"0")-1)),window.refreshAlertsDot?.();const c=n?.querySelector("#nb-alerts-body");c&&!c.querySelector(".nb-alert-row")&&i([])}catch{window.toast&&toast("Failed to delete","error")}}window.openAlertsPopover=s,window._nbToggleAlert=h,window._nbDeleteAlert=d,window.refreshAlertsDot=async function(){try{const a=await Auth.apiFetch("/alerts");if(!a.ok)return;const r=await a.json(),e=document.getElementById("nb-alerts-dot");if(!e)return;const t=r.filter(b=>b.enabled).length;e.style.display=t>0?"inline-block":"none"}catch{}},document.addEventListener("DOMContentLoaded",()=>{const a=window.location.pathname;a==="/login"||a==="/register"||a==="/"||setTimeout(()=>window.refreshAlertsDot?.(),800)})})();
