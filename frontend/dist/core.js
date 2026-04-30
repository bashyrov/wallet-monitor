const Auth=(()=>{const g="wm_token",p="wm_user";function e(){return localStorage.getItem(g)}function t(){try{return JSON.parse(localStorage.getItem(p)||"null")}catch{return null}}function f(a,d){localStorage.setItem(g,a),localStorage.setItem(p,JSON.stringify(d))}function r(){localStorage.removeItem(g),localStorage.removeItem(p)}function o(){return!!e()}function h(a="/login"){o()||window.location.replace(a+"?next="+encodeURIComponent(window.location.pathname))}function m(a="/app"){if(!o()){window.location.replace("/login?next="+encodeURIComponent(window.location.pathname));return}const d=t();(!d||!d.is_admin)&&window.location.replace(a)}function i(){const a=t();return o()&&!!a?.is_admin}function s(a="/app"){o()&&fetch("/api/auth/me",{headers:{Authorization:"Bearer "+e()}}).then(d=>{d.ok||r()}).catch(()=>{})}function x(a="/login"){r(),fetch("/api/auth/logout",{method:"POST"}).finally(()=>{window.location.replace(a)})}async function l(a,d={}){const b=e(),n={"Content-Type":"application/json",...d.headers||{}};b&&(n.Authorization="Bearer "+b);const c=await fetch("/api"+a,{...d,headers:n});if(c.status===401&&b){r();const u=window.location.pathname;throw u!=="/login"&&u!=="/register"&&window.location.replace("/login"),new Error("Session expired")}return c}return{getToken:e,getUser:t,setSession:f,clearSession:r,isLoggedIn:o,isAdmin:i,requireAuth:h,requireAdmin:m,redirectIfAuthed:s,logout:x,apiFetch:l}})();(function(){if(window.toast&&window.toast._avalant)return;const g=`
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
`,p=document.createElement("style");p.id="av-toast-style",p.textContent=g,document.head.appendChild(p);function e(){let o=document.getElementById("av-toast-host");return o||(o=document.createElement("div"),o.id="av-toast-host",o.className="av-toast-host",document.body.appendChild(o)),o}const t={success:'<svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8.5l3.5 3.5L13 5"/></svg>',error:'<svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="8" cy="8" r="6.5"/><path d="M8 5v4M8 11v.5"/></svg>',warn:'<svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2l6.5 11.5h-13L8 2z"/><path d="M8 7v3M8 11.5v.5"/></svg>',info:'<svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="8" cy="8" r="6.5"/><path d="M8 7v4M8 5v.5"/></svg>'};function f(o,h,m){let i;typeof o=="object"&&o!==null?i=o:i={title:o,type:h||"info",sub:m||""};const s=i.type||"info",x=i.title||"",l=i.sub||"",a=i.duration!=null?i.duration:s==="success"?4e3:3200,d=e(),b=document.createElement("div");b.className="av-toast "+s,b.innerHTML=`
      <div class="av-toast-icon-wrap">${t[s]||t.info}</div>
      <div class="av-toast-body">
        <div class="av-toast-title">${r(x)}</div>
        ${l?`<div class="av-toast-sub">${l}</div>`:""}
      </div>
      <button class="av-toast-close" aria-label="Close">\xD7</button>
    `,d.appendChild(b),requestAnimationFrame(()=>b.classList.add("show"));const n=()=>{b.classList.add("hide"),b.classList.remove("show"),setTimeout(()=>b.remove(),300)};return b.querySelector(".av-toast-close").addEventListener("click",n),a>0&&setTimeout(n,a),n}function r(o){return String(o).replace(/[&<>"']/g,h=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[h])}f._avalant=!0,window.toast=f})(),(function(){if(window.Confirm)return;const g=`
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
`,p=document.createElement("style");p.id="av-cf-style",p.textContent=g,document.head.appendChild(p);const e={default:'<svg width="20" height="20" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2l6 11H2L8 2z"/><path d="M8 7v3"/><circle cx="8" cy="12.2" r="0.6" fill="currentColor"/></svg>',danger:'<svg width="20" height="20" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M3 5h10M6 5V3.5a1 1 0 011-1h2a1 1 0 011 1V5m-4 0v8a1 1 0 001 1h2a1 1 0 001-1V5"/></svg>',info:'<svg width="20" height="20" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="8" cy="8" r="6"/><path d="M8 5v.5M8 7.5v3.5"/></svg>'};let t=null,f=null;function r(){return t||(t=document.createElement("div"),t.className="av-cf-backdrop",t.innerHTML=`
      <div class="av-cf-box" role="dialog" aria-modal="true">
        <div class="av-cf-hdr" data-av-cf-hdr>
          <span class="av-cf-ic" data-av-cf-ic>${e.default}</span>
          <h3 class="av-cf-title" data-av-cf-title>Confirm</h3>
        </div>
        <div class="av-cf-msg" data-av-cf-msg></div>
        <div class="av-cf-actions">
          <button type="button" class="av-cf-btn av-cf-btn-ghost" data-av-cf-cancel>Cancel</button>
          <button type="button" class="av-cf-btn av-cf-btn-primary" data-av-cf-ok>OK</button>
        </div>
      </div>`,document.body.appendChild(t),t.addEventListener("click",i=>{i.target===t&&o(!1)}),t.querySelector("[data-av-cf-cancel]").addEventListener("click",()=>o(!1)),t.querySelector("[data-av-cf-ok]").addEventListener("click",()=>o(!0)),document.addEventListener("keydown",i=>{i.key==="Escape"&&t.classList.contains("open")&&o(!1)}),t)}function o(i){if(!t)return;t.classList.remove("open");const s=f;f=null,s&&s(i)}function h(i={}){r();const s=i.title||"Confirm",x=i.message||"",l=i.okText||"OK",a=i.cancelText,d=i.danger?"danger":i.info?"info":"default",b=t.querySelector("[data-av-cf-hdr]");b.classList.remove("danger","info"),d!=="default"&&b.classList.add(d),t.querySelector("[data-av-cf-ic]").innerHTML=e[d]||e.default,t.querySelector("[data-av-cf-title]").textContent=s,t.querySelector("[data-av-cf-msg]").innerHTML=x;const n=t.querySelector("[data-av-cf-ok]");n.textContent=l,n.className="av-cf-btn "+(i.danger?"av-cf-btn-danger":"av-cf-btn-primary");const c=t.querySelector("[data-av-cf-cancel]");return a===""||a===null?c.style.display="none":(c.style.display="",c.textContent=a||"Cancel"),t.classList.add("open"),setTimeout(()=>n.focus(),30),new Promise(u=>{f=u})}function m(i={}){return h({...i,cancelText:""})}window.Confirm={ask:h,tell:m}})(),(function(){if(window.toggleTheme&&window.toggleTheme._avalant)return;const g=`
/* \u2500\u2500 Light theme: pure B&W with slightly darker green/red for readable text \u2500\u2500 */
body.light{
  --bg:        #FFFFFF;
  --surface:   #FFFFFF;
  --surface2:  #F4F4F4;
  --surface3:  #E8E8E8;
  --border:    #BABABA;
  --border2:   #8C8C8C;
  --text:      #000000;
  --text2:     #1A1A1A;
  --text3:     #595959;
  --green:     #006B3C;   /* darker, readable on white */
  --red:       #8B0000;
  --yellow:    #6B5011;
  --blue:      #1E478F;
  --teal:      #006970;
  --purple:    #5A2E9E;
}
body.light{color-scheme:light;}

/* Shared component overrides (navbar, toast) */
body.light .topbar{background:rgba(255,255,255,0.95)!important;border-bottom-color:#E2E2E2;}
body.light app-navbar::after{background:linear-gradient(90deg,transparent,rgba(0,0,0,0.08),transparent);}
body.light .brand{color:#000;}
body.light .brand-cursor{color:#000;}
body.light .nav-lnk{color:#2A2A2A;}
body.light .nav-lnk:hover{color:#000;background:#EDEDED;border-color:#BABABA;}
body.light .nav-lnk.active{color:#000;background:#EDEDED;border-color:#000;}
body.light .avatar-btn{background:linear-gradient(135deg,#006B3C,#1E478F);color:#FFF;}

/* Positive/negative color helpers (for text on white bg, darker green for contrast) */
body.light .pos,body.light .pos-text,body.light .net-pos,body.light .rate-pos{color:#006B3C!important;}
body.light .neg,body.light .neg-text,body.light .net-neg,body.light .rate-neg{color:#8B0000!important;}

/* Theme toggle button (added by this script) */
.theme-toggle-btn{background:none;border:none;cursor:pointer;padding:5px 9px;border-radius:6px;color:var(--text3);display:inline-flex;align-items:center;justify-content:center;transition:color .15s,background .15s;font-family:inherit;}
.theme-toggle-btn:hover{color:var(--text);background:rgba(255,255,255,.05);}
body.light .theme-toggle-btn:hover{background:#EDEDED;color:#000;}
.theme-toggle-btn svg{width:15px;height:15px;display:block;}
`,p=document.createElement("style");p.id="av-theme-style",p.textContent=g,(document.head||document.documentElement).appendChild(p);try{localStorage.getItem("theme")==="light"&&(document.body?document.body.classList.add("light"):document.documentElement.classList.add("light-preload"))}catch{}document.body||document.addEventListener("DOMContentLoaded",()=>{document.documentElement.classList.contains("light-preload")&&(document.body.classList.add("light"),document.documentElement.classList.remove("light-preload"))});const e='<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="8" cy="8" r="3.2"/><path d="M8 1v1.5M8 13.5V15M1 8h1.5M13.5 8H15M3 3l1 1M12 12l1 1M13 3l-1 1M4 12l-1 1"/></svg>',t='<svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 1a7 7 0 1 0 0 14A7 7 0 0 0 8 1zm0 2v10a5 5 0 1 1 0-10z"/></svg>';function f(){const o=document.body.classList.contains("light");document.querySelectorAll(".theme-toggle-btn").forEach(h=>{h.innerHTML=o?e:t,h.title=o?"Switch to dark":"Switch to light"})}function r(){const o=document.body.classList.toggle("light");try{localStorage.setItem("theme",o?"light":"dark")}catch{}f(),window.dispatchEvent(new CustomEvent("themechange",{detail:{light:o}}))}r._avalant=!0,window.toggleTheme=r})(),(function(){const g={binance:"Binance",bybit:"Bybit",okx:"OKX",gate:"Gate",kucoin:"KuCoin",mexc:"MEXC",bitget:"Bitget",hyperliquid:"Hyperliquid",aster:"Aster",ethereal:"Ethereal",whitebit:"WhiteBIT",bingx:"BingX",backpack:"Backpack",lighter:"Lighter",paradex:"Paradex",htx:"HTX",extended:"Extended",kraken:"Kraken"},p={binance:"#F0B90B",bybit:"#F0842D",okx:"#C8C8C8",gate:"#17C684",kucoin:"#09BA86",mexc:"#17D854",bitget:"#00D2C8",hyperliquid:"#64B4FF",aster:"#8A63D2",ethereal:"#C864C8",whitebit:"#2DCCCD",bingx:"#1DB8F2",backpack:"#4ADE80",lighter:"#A78BFA",paradex:"#FF6A6A",htx:"#2E7DF6",extended:"#E879F9",kraken:"#7C5CFF",ethereum:"#627EEA",bsc:"#F3BA2F",polygon:"#8247E5",arbitrum:"#28A0F0",optimism:"#FF0420",base:"#0052FF",avalanche:"#E84142",tron:"#C2A633",solana:"#9945FF",zksync:"#1C9BEF",linea:"#7B61FF",scroll:"#FFEEDA",mantle:"#27E5C7",blast:"#FCFC03",fantom:"#13B5EC"};Object.assign(g,{ethereum:"Ethereum",bsc:"BSC",polygon:"Polygon",arbitrum:"Arbitrum",optimism:"Optimism",base:"Base",avalanche:"Avalanche",tron:"Tron",solana:"Solana",zksync:"zkSync",linea:"Linea",scroll:"Scroll",mantle:"Mantle",blast:"Blast",fantom:"Fantom"});function t(s){return`<span class="ex-dot" data-ex="${(s||"").toLowerCase()}"></span>`}function f(s,x){const l=(s||"").toLowerCase(),a=g[l]||s||"\u2014",d=x&&x.suffix?" "+x.suffix:"";return`<span class="ex-chip">${t(l)}<span class="ex-name">${a}${d}</span></span>`}if(!document.getElementById("ex-shared-css")){const s=[".ex-dot{width:7px;height:7px;border-radius:50%;display:inline-block;flex-shrink:0;vertical-align:baseline;box-shadow:0 0 0 1px rgba(0,0,0,.25) inset}",".ex-chip{display:inline-flex;align-items:center;gap:6px;font-weight:600;font-size:12px;color:var(--text,#E6E8E3);}",".ex-chip .ex-name{letter-spacing:.01em}"];Object.entries(p).forEach(([l,a])=>{s.push(`.ex-dot[data-ex="${l}"]{background:${a}}`)});const x=document.createElement("style");x.id="ex-shared-css",x.textContent=s.join(`
`),document.head.appendChild(x)}const r={screener_cex:[],screener_perp_dex:[],screener_spot:[],portfolio_cex:[],portfolio_perp_dex:[],portfolio_chains:[],screener_all:[]},o={screener_cex:0,screener_perp_dex:0,screener_spot:0,portfolio_cex:0,portfolio_perp_dex:0,portfolio_chains:0};let h;const m=new Promise(s=>{h=s});async function i(){try{const s=await fetch("/api/meta/venues",{credentials:"omit"});if(!s.ok)throw new Error("http "+s.status);const x=await s.json(),l=x.screener||{},a=x.portfolio||{};r.screener_cex=(l.cex||[]).map(n=>n.id),r.screener_perp_dex=(l.perp_dex||[]).map(n=>n.id),r.screener_spot=(l.spot||[]).map(n=>n.id),r.portfolio_cex=(a.cex||[]).map(n=>n.id),r.portfolio_perp_dex=(a.perp_dex||[]).map(n=>n.id),r.portfolio_chains=(a.chains||[]).map(n=>n.id),r.screener_all=r.screener_cex.concat(r.screener_perp_dex),Object.assign(o,x.counts||{}),[l.cex,l.perp_dex,l.spot,a.cex,a.perp_dex,a.chains].forEach(n=>{(n||[]).forEach(({id:c,label:u})=>{c&&u&&!g[c]&&(g[c]=u)})}),document.querySelectorAll("[data-meta]").forEach(n=>{const c=n.getAttribute("data-meta");c&&o[c]!=null&&(n.textContent=String(o[c]))});const d={};[l.cex,l.perp_dex,l.spot,a.cex,a.perp_dex,a.chains].forEach(n=>{(n||[]).forEach(({id:c,label:u})=>{c&&(d[c]=u||g[c]||c)})});const b={screener_cex:r.screener_cex,screener_perp_dex:r.screener_perp_dex,screener_spot:r.screener_spot,portfolio_cex:r.portfolio_cex,portfolio_perp_dex:r.portfolio_perp_dex,portfolio_chains:r.portfolio_chains};document.querySelectorAll("[data-venues]").forEach(n=>{const c=n.getAttribute("data-venues"),u=b[c];Array.isArray(u)&&u.length&&(n.textContent=u.map(v=>d[v]||g[v]||v).join(", "))}),document.querySelectorAll("[data-meta-sum]").forEach(n=>{const u=(n.getAttribute("data-meta-sum")||"").split("+").map(v=>v.trim()).reduce((v,k)=>v+(o[k]||0),0);u>0&&(n.textContent=String(u))}),document.querySelectorAll("[data-venues-grid]").forEach(n=>{const c=n.getAttribute("data-venues-grid")||"",u=[];if(c.split("+").forEach(y=>{const w=b[y.trim()];Array.isArray(w)&&u.push(...w)}),!u.length)return;const v=n.getAttribute("data-chip-class")||"source-chip",k=n.getAttribute("data-chip-dot-class")||"source-chip-dot";n.innerHTML=u.map(y=>{const w=d[y]||g[y]||y,E=p[y]||"var(--text3)";return`<div class="${v}"><span class="${k}" style="background:${E}"></span>${w}</div>`}).join("")})}catch(s){console.warn("EX.loadVenues failed:",s)}finally{h()}}i(),window.EX={labels:g,colors:p,dot:t,chip:f,lists:r,counts:o,ready:m,loadVenues:i}})(),(function(){"use strict";const g=e=>`<span style="font-family:var(--mono)">${e}</span>`;Object.defineProperty(window,"FMT",{value:{price(e){return e==null||e===0?"\u2014":e>=1e3?"$"+e.toLocaleString("en-US",{maximumFractionDigits:2}):e>=1?"$"+e.toFixed(4):"$"+e.toPrecision(4)},volume(e){return!e||e===0?'<span style="color:var(--text3)">\u2014</span>':e>=1e9?"$"+(e/1e9).toFixed(2)+"B":e>=1e6?"$"+(e/1e6).toFixed(2)+"M":e>=1e3?"$"+(e/1e3).toFixed(1)+"K":"$"+e.toFixed(0)},apr(e){if(e==null)return"\u2014";const t=e>=0?"+":"";return`<span class="td-apr ${e>0?"rate-pos":e<0?"rate-neg":"rate-zero"}">${t}${Math.abs(e).toFixed(2)}%</span>`},rate(e,t){const f=(e*100).toFixed(4),r=e>=0?"+":"",o=e>0?"rate-pos":e<0?"rate-neg":"rate-zero",h=t===1?"1h":`${t}h`;return`<span class="td-rate ${o}">${r}${f}%<span style="font-weight:400;color:var(--text3);font-size:11px"> /${h}</span></span>`},pct(e,t=4){return e==null?"\u2014":`${e>=0?"+":""}${e.toFixed(t)}%`},countdown(e){if(!e)return"\u2014";const t=e-Math.floor(Date.now()/1e3);if(t<=0)return'<span class="next-soon">now</span>';const f=Math.floor(t/3600),r=Math.floor(t%3600/60),o=t%60,h=t<600?"next-soon":"";return f>0?`<span class="${h}">${f}h ${r}m</span>`:r>0?`<span class="${h}">${r}m ${o}s</span>`:`<span class="next-soon">${o}s</span>`},sign(e){return e>=0?"+":""},esc(e){return String(e??"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;")},stripUsdt(e){return e&&(e.endsWith("USDT")?e.slice(0,-4):e.endsWith("USD")?e.slice(0,-3):e)}},enumerable:!1})})();
