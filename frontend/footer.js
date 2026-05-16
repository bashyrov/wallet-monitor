/**
 * <app-footer> — shared site footer.
 *
 * Drop into any page: `<app-footer></app-footer>`. CSS lives inside this
 * file (injected once on first registration) so the host page only has
 * to import the script — no manual <link> for footer styles.
 *
 * Mirrors the footer that was previously inlined in /index.html. Single
 * source of truth — fix typos / add columns here and every page picks it
 * up next load. Removed columns vs the original markup: X/Twitter and
 * GitHub links (account placeholders, not yet live), API Docs link
 * (docs page doesn't exist).
 */
(() => {
  if (window.customElements?.get?.('app-footer')) return;

  const STYLE_ID = 'app-footer-style';
  const CSS = `
    app-footer { display:block; }
    .av-footer {
      background: var(--bg-soft, #0E0E11);
      border-top: 1px solid var(--border, #22222A);
      padding: clamp(56px, 7vw, 96px) 0 32px;
      margin-top: auto;
      font-family: var(--font, 'Inter', system-ui, sans-serif);
    }
    .av-footer-wrap { max-width: 1280px; margin: 0 auto; padding: 0 clamp(20px, 3.5vw, 56px); }
    .av-footer-cols {
      display: grid;
      grid-template-columns: 2fr repeat(3, 1fr);
      gap: clamp(24px, 3vw, 56px);
      margin-bottom: clamp(40px, 5vw, 72px);
    }
    @media (max-width: 900px) { .av-footer-cols { grid-template-columns: repeat(2, 1fr); } }
    @media (max-width: 520px) { .av-footer-cols { grid-template-columns: 1fr 1fr; } .av-footer-brand-col { grid-column: 1 / -1; } }

    .av-footer .brand {
      font-size: 22px; font-weight: 800; letter-spacing: -0.5px;
      color: var(--text, #E6E8E3); text-decoration: none; display: inline-block;
    }
    .av-footer .brand .cursor {
      color: var(--green, #1AFFAB);
      animation: av-footer-blink 1.1s step-start infinite;
    }
    @keyframes av-footer-blink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }

    .av-footer-brand-col p {
      margin-top: 16px; color: var(--text2, #9B9FAB); font-size: 14px;
      max-width: 36ch; line-height: 1.55;
    }
    .av-footer-col h5 {
      font-size: 11px; font-weight: 600;
      color: var(--text3, #55585F);
      letter-spacing: .12em; text-transform: uppercase;
      margin: 0 0 18px;
    }
    .av-footer-col ul {
      list-style: none; margin: 0; padding: 0;
      display: flex; flex-direction: column; gap: 12px;
    }
    .av-footer-col a {
      color: var(--text2, #9B9FAB);
      font-size: 14px; text-decoration: none;
      transition: color .15s ease;
    }
    .av-footer-col a:hover { color: var(--text, #E6E8E3); }
    .av-footer-bottom {
      padding-top: 24px; border-top: 1px solid var(--border, #22222A);
      display: flex; justify-content: space-between; align-items: center;
      font-size: 12px; color: var(--text3, #55585F);
      flex-wrap: wrap; gap: 12px;
    }
  `;

  function injectStyles() {
    if (document.getElementById(STYLE_ID)) return;
    const s = document.createElement('style');
    s.id = STYLE_ID;
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  class AppFooter extends HTMLElement {
    connectedCallback() {
      injectStyles();
      this.innerHTML = `
        <footer class="av-footer">
          <div class="av-footer-wrap">
            <div class="av-footer-cols">
              <div class="av-footer-col av-footer-brand-col">
                <a href="/" class="brand">avalant<span class="cursor">_</span></a>
                <p>Funding arbitrage, without the spreadsheet tax. 18 venues, sub-second data, one workbench.</p>
              </div>
              <div class="av-footer-col">
                <h5>Product</h5>
                <ul>
                  <li><a href="/screener">Screener</a></li>
                  <li><a href="/portfolio">Portfolio</a></li>
                  <li><a href="/arb">Arb terminal</a></li>
                  <li><a href="/pricing">Pricing</a></li>
                </ul>
              </div>
              <div class="av-footer-col">
                <h5>Resources</h5>
                <ul>
                  <li><a href="/changelog">Changelog</a></li>
                  <li><a href="/status">Status</a></li>
                </ul>
              </div>
              <div class="av-footer-col">
                <h5>Company</h5>
                <ul>
                  <li><a href="mailto:hi@avalant.xyz">Contact</a></li>
                  <li><a href="/privacy">Privacy</a></li>
                  <li><a href="/terms">Terms</a></li>
                  <li><a href="/security">Security</a></li>
                </ul>
              </div>
            </div>
            <div class="av-footer-bottom">
              <div>© 2026 Avalant. Not investment advice. Trading is risky.</div>
              <div><a href="https://t.me/avalant" target="_blank" rel="noopener" style="color:inherit;text-decoration:none">Telegram</a></div>
            </div>
          </div>
        </footer>
      `;
    }
  }

  window.customElements.define('app-footer', AppFooter);
})();
