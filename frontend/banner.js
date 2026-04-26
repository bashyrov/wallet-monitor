/**
 * Site-wide announcement banner — admin-controlled.
 *
 * Renders a fixed bar at the very top of the page (above the navbar) when
 * /api/banner returns enabled=true. Two display modes:
 *   · static  — single line of text, centered, truncated with ellipsis if
 *               it overflows. Best for short announcements.
 *   · marquee — endless horizontal scroll. Best for long messages or when
 *               you want continuous attention.
 *
 * Polls /api/banner every 60 s so admin changes propagate without a
 * page refresh — same cadence as the maintenance lockout's auto-reload.
 *
 * Lives in vanilla JS with zero deps so it loads on every page including
 * the maintenance lockout page (no `auth.js` requirement). Idempotent —
 * re-running the IIFE on dynamic-route changes is safe.
 */
(function () {
  'use strict';

  const POLL_MS = 60000;
  const ELEMENT_ID = 'avalant-site-banner';
  const STYLE_ID = 'avalant-banner-style';

  let _last = null;       // last JSON we rendered, to skip no-op DOM writes
  let _timer = null;

  function injectStyles() {
    if (document.getElementById(STYLE_ID)) return;
    const css = `
      #${ELEMENT_ID} {
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
      #${ELEMENT_ID} .b-text {
        display: inline-block; max-width: 100%;
        text-overflow: ellipsis; overflow: hidden;
      }
      #${ELEMENT_ID}.marquee {
        justify-content: flex-start;
      }
      #${ELEMENT_ID}.marquee .b-track {
        display: inline-flex; gap: 64px; align-items: center;
        white-space: nowrap;
        animation: avalant-banner-scroll 28s linear infinite;
        will-change: transform;
      }
      #${ELEMENT_ID}.marquee .b-track .b-text {
        display: inline-block; max-width: none;
      }
      @keyframes avalant-banner-scroll {
        from { transform: translateX(0); }
        to   { transform: translateX(-50%); }
      }
      body.has-site-banner { padding-top: 36px !important; }
      /* Maintenance + landing pages center their content with min-height:100vh
         — push that down by the banner height so the card doesn't crowd up. */
      body.has-site-banner .wrap { min-height: calc(100vh - 36px) !important; }
      @media (max-width: 560px) {
        #${ELEMENT_ID} { font-size: 12.5px; height: 32px; padding: 0 12px; }
        body.has-site-banner { padding-top: 32px !important; }
        body.has-site-banner .wrap { min-height: calc(100vh - 32px) !important; }
      }
    `;
    const tag = document.createElement('style');
    tag.id = STYLE_ID;
    tag.textContent = css;
    (document.head || document.documentElement).appendChild(tag);
  }

  function escapeHtml(s) {
    return (s == null ? '' : String(s)).replace(/[&<>"']/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
    });
  }

  function remove() {
    const el = document.getElementById(ELEMENT_ID);
    if (el) el.remove();
    document.body.classList.remove('has-site-banner');
    _last = null;
  }

  function render(state) {
    if (!state || !state.enabled || !state.text) {
      remove();
      return;
    }
    // Skip DOM writes when nothing changed — avoids restarting the marquee
    // animation on every poll.
    if (_last && _last.enabled === state.enabled
        && _last.text === state.text
        && _last.marquee === state.marquee) {
      return;
    }

    injectStyles();
    let el = document.getElementById(ELEMENT_ID);
    if (!el) {
      el = document.createElement('div');
      el.id = ELEMENT_ID;
      // Insert as the first child of body so the navbar (which usually
      // takes top:0) gets pushed below via the body padding rule.
      if (document.body.firstChild) {
        document.body.insertBefore(el, document.body.firstChild);
      } else {
        document.body.appendChild(el);
      }
      document.body.classList.add('has-site-banner');
    }

    const safe = escapeHtml(state.text);
    if (state.marquee) {
      el.classList.add('marquee');
      // Two copies of the text in the track so the scroll loops seamlessly
      // (animation translates -50% so the second copy fills the gap).
      el.innerHTML =
        '<div class="b-track">' +
        '<span class="b-text">' + safe + '</span>' +
        '<span class="b-text" aria-hidden="true">' + safe + '</span>' +
        '</div>';
    } else {
      el.classList.remove('marquee');
      el.innerHTML = '<span class="b-text">' + safe + '</span>';
    }
    _last = state;
  }

  async function poll() {
    try {
      const r = await fetch('/api/banner', { cache: 'no-store' });
      if (!r.ok) return;
      const j = await r.json();
      render(j);
    } catch (_e) {
      // Network blip — keep the current banner displayed; next tick retries.
    }
  }

  function start() {
    poll();
    _timer = setInterval(poll, POLL_MS);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }

  // Expose for manual refresh (e.g. admin clicks Save → ping the banner).
  window.AvalantBanner = { reload: poll };
})();
