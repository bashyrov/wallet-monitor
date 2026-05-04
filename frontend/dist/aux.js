(function(){"use strict";const e="avalant-site-banner",r="avalant-banner-style";let o=null,p=null;function c(){if(document.getElementById(r))return;const i=`
      #${e} {
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
      #${e} .b-text {
        display: inline-block; max-width: 100%;
        text-overflow: ellipsis; overflow: hidden;
      }
      #${e}.marquee {
        justify-content: flex-start;
      }
      #${e}.marquee .b-track {
        display: inline-flex; gap: 64px; align-items: center;
        white-space: nowrap;
        animation: avalant-banner-scroll 28s linear infinite;
        will-change: transform;
      }
      #${e}.marquee .b-track .b-text {
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
        #${e} { font-size: 12.5px; height: 32px; padding: 0 12px; }
        body.has-site-banner { padding-top: 32px !important; }
        body.has-site-banner .wrap { min-height: calc(100vh - 32px) !important; }
      }
    `,t=document.createElement("style");t.id=r,t.textContent=i,(document.head||document.documentElement).appendChild(t)}function n(i){return(i==null?"":String(i)).replace(/[&<>"']/g,function(t){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[t]})}function a(){const i=document.getElementById(e);i&&i.remove(),document.body.classList.remove("has-site-banner"),o=null}function u(i){if(!i||!i.enabled||!i.text){a();return}if(o&&o.enabled===i.enabled&&o.text===i.text&&o.marquee===i.marquee)return;c();let t=document.getElementById(e);t||(t=document.createElement("div"),t.id=e,document.body.firstChild?document.body.insertBefore(t,document.body.firstChild):document.body.appendChild(t),document.body.classList.add("has-site-banner"));const l=n(i.text);i.marquee?(t.classList.add("marquee"),t.innerHTML='<div class="b-track"><span class="b-text">'+l+'</span><span class="b-text" aria-hidden="true">'+l+"</span></div>"):(t.classList.remove("marquee"),t.innerHTML='<span class="b-text">'+l+"</span>"),o=i}async function g(){try{const i=await fetch("/api/banner",{cache:"no-store"});if(!i.ok)return;const t=await i.json();u(t)}catch{}}function b(){g(),p=setInterval(g,6e4)}document.readyState==="loading"?document.addEventListener("DOMContentLoaded",b,{once:!0}):b(),window.AvalantBanner={reload:g}})(),(function(){function s(){if(document.getElementById("avalant-popup-style"))return;const t=`
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
    `,l=document.createElement("style");l.id="avalant-popup-style",l.textContent=t,(document.head||document.documentElement).appendChild(l)}function e(t){return(t??"").toString().replace(/[&<>"']/g,l=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[l])}function r(){try{return!!localStorage.getItem("wm_token")}catch{return!1}}let o=null,p=[];function c(t){if(o)return;s(),o=t;const l=t.button_url||"/pricing",v=t.button_text||"View pricing",d=document.createElement("div");d.className="avalant-popup-backdrop",d.innerHTML=`
      <div class="avalant-popup-card" role="dialog" aria-modal="true" aria-labelledby="avalant-popup-title-${t.id}">
        <button class="avalant-popup-close" aria-label="Close">
          <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M3 3l8 8M11 3l-8 8"/></svg>
        </button>
        <div class="avalant-popup-inner">
          <div class="avalant-popup-head">
            <span class="avalant-popup-id">#${t.id}</span>
            <span class="avalant-popup-tag">Announcement</span>
          </div>
          <h3 class="avalant-popup-title" id="avalant-popup-title-${t.id}">${e(t.title)}</h3>
          <p class="avalant-popup-body">${e(t.body)}</p>
          <div class="avalant-popup-foot">
            <button class="avalant-popup-skip" type="button">Maybe later</button>
            <a class="avalant-popup-cta" href="${e(l)}">
              ${e(v)}
              <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7h8M7.5 3.5L11 7l-3.5 3.5"/></svg>
            </a>
          </div>
        </div>
      </div>
    `,document.body.appendChild(d);const h=()=>{g(t.id).finally(()=>{if(d.remove(),o=null,p.length){const f=p.shift();c(f)}})};d.querySelector(".avalant-popup-close").addEventListener("click",h),d.querySelector(".avalant-popup-skip").addEventListener("click",h),d.addEventListener("click",f=>{f.target===d&&h()})}const n="avalant_popup_anon_dismissed";function a(){try{const t=localStorage.getItem(n),l=t?JSON.parse(t):[];return Array.isArray(l)?new Set(l.map(Number)):new Set}catch{return new Set}}function u(t){try{localStorage.setItem(n,JSON.stringify([...t]))}catch{}}async function g(t){if(r())try{await Auth.apiFetch(`/popups/${t}/dismiss`,{method:"POST"})}catch{}else{const l=a();l.add(Number(t)),u(l)}}async function b(){try{const t=r()?{headers:{Authorization:"Bearer "+localStorage.getItem("wm_token")}}:{},l=await fetch("/api/popups/pending",t);if(!l.ok)return;let d=(await l.json()).popups||[];if(!r()&&d.length){const h=a();d=d.filter(f=>!h.has(Number(f.id)))}if(!d.length)return;p=d.slice(1),c(d[0])}catch{}}function i(){document.readyState==="loading"?document.addEventListener("DOMContentLoaded",b,{once:!0}):b()}i(),window.AvalantPopup={reload:b}})(),(function(){function s(){try{return!!localStorage.getItem("wm_token")}catch{return!1}}if(!s())return;const e="expiry_banner_dismissed_until",r=1440*60*1e3;function o(){try{const n=parseInt(localStorage.getItem(e)||"0",10);return Number.isFinite(n)&&Date.now()<n}catch{return!1}}async function p(){if(o())return;let n=null;try{const g=await fetch("/api/auth/me",{headers:{Authorization:"Bearer "+localStorage.getItem("wm_token")}});if(!g.ok)return;n=await g.json()}catch{return}if(!n||!n.plan_expires_at)return;const a=new Date(n.plan_expires_at);if(Number.isNaN(a.getTime()))return;const u=Math.round((a.getTime()-Date.now())/864e5);u>7||c(n,u)}function c(n,a){if(document.getElementById("expiry-banner"))return;let u,g;a<=0?(u="#F87171",g=`Your <b>${n.plan_slug||n.plan||"plan"}</b> plan expired \u2014 portfolio scan downgraded. <a href="/pricing" style="color:#fff;text-decoration:underline">Renew</a>`):a<=3?(u="#E5C07B",g=`Your <b>${n.plan_slug||n.plan||"plan"}</b> plan ends in <b>${a} day${a===1?"":"s"}</b>. <a href="/pricing" style="color:#000;text-decoration:underline">Renew now</a>`):(u="#06B6D4",g=`Your <b>${n.plan_slug||n.plan||"plan"}</b> plan renews in <b>${a} days</b>. <a href="/pricing" style="color:#fff;text-decoration:underline">Manage</a>`);const b=document.createElement("div");b.id="expiry-banner",b.style.cssText=`
      position:sticky; top:0; z-index:200;
      background:${u}; color:${a<=3&&a>0?"#000":"#fff"};
      padding:9px 16px; text-align:center; font-size:13px; font-weight:500;
      font-family:'Inter',sans-serif;
      box-shadow:0 2px 12px rgba(0,0,0,0.12);
      display:flex; justify-content:center; align-items:center; gap:14px;
    `,b.innerHTML=`
      <span>${g}</span>
      <button id="expiry-banner-close" aria-label="Dismiss"
        style="background:transparent;border:0;color:inherit;font-size:18px;cursor:pointer;line-height:1;padding:0 6px;opacity:0.75">\xD7</button>
    `,document.body.insertBefore(b,document.body.firstChild),document.getElementById("expiry-banner-close").addEventListener("click",()=>{try{localStorage.setItem(e,String(Date.now()+r))}catch{}b.remove()})}document.readyState==="loading"?document.addEventListener("DOMContentLoaded",p,{once:!0}):p()})();const _ICONS={portfolio:'<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M2 11V7M5 11V4M8 11V8M11 11V2"/></svg>',screener:'<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M2 7h10M2 4h10M2 10h6"/></svg>',archive:'<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M1.5 4.5h11v7a1 1 0 0 1-1 1h-9a1 1 0 0 1-1-1v-7zM.5 1.5h13v3H.5zM5.5 7.5h3"/></svg>',pricing:'<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M1.5 1.5h5l6 6-5 5-6-6v-5z"/><circle cx="4.5" cy="4.5" r="1" fill="currentColor"/></svg>',login:'<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M9 5L13 9 9 13M13 9H4M4 1H2a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h2"/></svg>'},_ALL_LINKS=[{id:"app",href:"/app",label:"Portfolio",icon:_ICONS.portfolio,authOnly:!1},{id:"archive",href:"/archive",label:"Archive",icon:_ICONS.archive,authOnly:!1},{id:"screener",href:"/screener",label:"Screener",icon:_ICONS.screener,authOnly:!1},{id:"pricing",href:"/pricing",label:"Pricing",icon:_ICONS.pricing,authOnly:!1}],_NAV_SET={app:["app","archive","screener","pricing"],archive:["app","archive","screener","pricing"],profile:["app","archive","screener","pricing"],index:["app","archive","screener","pricing"],pricing:["app","archive","screener","pricing"],screener:["app","screener","pricing"],arb:["app","screener","pricing"],watchlist:["app","screener","pricing"],login:[],register:[],checkout:["app","pricing"]},_ACTIVE={app:"app",screener:"screener",archive:"archive",pricing:"pricing",watchlist:"watchlist",profile:null,index:null,login:null,register:null,checkout:null,arb:"screener"};function _navLink(s,e){const r="nav-lnk"+(s.id===e?" active":"");return`<a href="${s.href}" class="${r}" data-nb-id="${s.id}">${s.label}</a>`}function _drawerLink(s,e,r){const o=s.id===e?' class="active"':"",p=String(r+1).padStart(2,"0");return`<a href="${s.href}"${o} data-nb-drawer="${s.id}">${s.label}<span class="num">${p}</span></a>`}function _avatarBtn(){return'<a href="/profile" class="avatar-btn" id="nav-avatar" title="Profile">U</a>'}function _rightHtml(s){switch(s){case"app":return`<button class="btn btn-primary btn-sm" onclick="openAddWalletModal()">+ Add Wallet</button>${_avatarBtn()}`;case"archive":case"profile":case"checkout":return _avatarBtn();case"screener":case"arb":case"watchlist":return`
        <div id="_nb-guest" style="display:flex;align-items:center;gap:10px">
          <a href="/login" class="btn btn-ghost btn-sm">Sign in</a>
          <a href="/register" class="btn btn-primary btn-sm">Get started</a>
        </div>
        <div id="_nb-user" style="display:none;align-items:center;gap:8px">
          <a href="/watchlist" class="nav-lnk-icon${s==="watchlist"?" active":""}" title="Watchlist" aria-label="Watchlist">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M8 1.3l1.85 3.75 4.15.6-3 2.93.71 4.13L8 10.77 4.29 12.7 5 8.57l-3-2.92 4.15-.6z"/></svg>
          </a>
          <button class="nav-lnk-bell" onclick="openAlertsPopover(event)" title="Alerts" aria-label="Alerts">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2a5 5 0 0 1 5 5v3l1 2H2l1-2V7a5 5 0 0 1 5-5z"/><path d="M6.5 13.5a1.5 1.5 0 0 0 3 0"/></svg>
            <span class="nav-dot" id="nb-alerts-dot" style="display:none"></span>
          </button>
          ${_avatarBtn()}
        </div>`;case"index":return`
        <div id="_nb-guest" style="display:flex;align-items:center;gap:10px">
          <a href="/login" class="btn btn-ghost btn-sm">Sign in</a>
          <a href="/register" class="btn btn-primary btn-sm">Get started</a>
        </div>
        <div id="_nb-user" style="display:none;align-items:center;gap:10px">
          <a href="/app" class="btn btn-primary btn-sm">Open app</a>
          ${_avatarBtn()}
        </div>`;case"pricing":return`
        <div id="_nb-guest" style="display:flex;align-items:center;gap:10px">
          <a href="/login" class="btn btn-ghost btn-sm">Sign in</a>
          <a href="/register" class="btn btn-primary btn-sm">Get started</a>
        </div>
        <div id="_nb-user" style="display:none;align-items:center;gap:10px">${_avatarBtn()}</div>`;case"login":return`<a href="/register" class="btn btn-ghost btn-sm">Register</a>
              <a href="/app" class="btn btn-primary btn-sm">Open app</a>`;case"register":return`<a href="/login" class="btn btn-ghost btn-sm">Sign in</a>
              <a href="/app" class="btn btn-primary btn-sm">Open app</a>`;default:return""}}class AppNavbar extends HTMLElement{connectedCallback(){const e=this.getAttribute("page")||"index",r=_ACTIVE[e]??null,o=_NAV_SET[e]??[],p=_ALL_LINKS.filter(a=>o.includes(a.id)),c=p.map(a=>_navLink(a,r)).join(""),n=p.map((a,u)=>_drawerLink(a,r,u)).join("");if(this.innerHTML=`
      <a href="/" class="brand">avalant<span class="brand-cursor">_</span></a>
      <nav class="topbar-nav">${c}</nav>
      <div class="topbar-right">${_rightHtml(e)}</div>
      <button class="nav-burger" id="nb-burger" aria-label="Open menu">
        <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M3 6h14M3 10h14M3 14h14"/></svg>
      </button>
    `,!document.getElementById("nav-drawer-root")){const a=document.createElement("div");a.id="nav-drawer-root",a.className="nav-drawer",a.innerHTML=`
        <div class="drawer-top">
          <a href="/" class="brand">avalant<span class="brand-cursor">_</span></a>
          <button class="nav-burger" id="nb-close" aria-label="Close menu">
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M5 5l10 10M15 5L5 15"/></svg>
          </button>
        </div>
        <nav class="drawer-menu">${n}</nav>
        <div class="drawer-cta">
          <a href="/login" class="btn btn-outline btn-lg" id="nb-drawer-signin">Sign in</a>
          <a href="/register" class="btn btn-primary btn-lg" id="nb-drawer-register">Get started</a>
        </div>
      `,document.body.appendChild(a)}this._wireBurger(),this._wireScrollState(),this._initAuth(e)}_wireBurger(){const e=document.getElementById("nav-drawer-root"),r=this.querySelector("#nb-burger");if(!e||!r)return;const o=e.querySelector("#nb-close"),p=()=>{e.classList.add("open"),r.classList.add("open"),document.body.style.overflow="hidden"},c=()=>{e.classList.remove("open"),r.classList.remove("open"),document.body.style.overflow=""};r.addEventListener("click",p),o&&o.addEventListener("click",c),e.querySelectorAll("a[data-nb-drawer]").forEach(n=>n.addEventListener("click",c)),document.addEventListener("keydown",n=>{n.key==="Escape"&&c()})}_wireScrollState(){const e=this.closest(".topbar");if(!e)return;const r=()=>{window.scrollY>4?e.classList.add("scrolled"):e.classList.remove("scrolled")};r(),window.addEventListener("scroll",r,{passive:!0})}_initAuth(e){if(typeof Auth>"u"){document.addEventListener("DOMContentLoaded",()=>this._applyAuth(e));return}this._applyAuth(e)}_applyAuth(e){if(typeof Auth>"u")return;const r=Auth.isLoggedIn(),o=Auth.getUser();if(r&&o){const n=this.querySelector("#nav-avatar");n&&(n.textContent=(o.username||o.email||"U")[0].toUpperCase())}if(["index","pricing","screener","arb","watchlist"].includes(e)){const n=this.querySelector("#_nb-guest"),a=this.querySelector("#_nb-user");r?(n&&(n.style.display="none"),a&&(a.style.display="flex")):(n&&(n.style.display="flex"),a&&(a.style.display="none"))}const p=document.getElementById("nb-drawer-signin"),c=document.getElementById("nb-drawer-register");r&&p&&c&&(p.style.display="none",c.textContent="Open app",c.href="/app")}}customElements.define("app-navbar",AppNavbar),window.openAlertsPopover=window.openAlertsPopover||function(s){if(typeof window._openAlertsModal=="function")return window._openAlertsModal(s);typeof window.toast=="function"&&window.toast({title:"Alerts",sub:"Coming soon \u2014 use /arb on a pair to set per-symbol alerts"})};
