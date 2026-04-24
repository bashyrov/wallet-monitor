/**
 * Single source of truth for exchange labels + brand colours + the
 * venue-chip helper. Loaded before any other app script on every page
 * that renders exchange names, so screener.html / arb.html / navbar.js
 * never drift.
 *
 * Use:
 *   window.EX.labels.binance   → 'Binance'
 *   window.EX.colors.binance   → '#F0B90B'
 *   window.EX.chip('binance')  → '<span class="ex-chip">…</span>'
 *   window.EX.dot('binance')   → '<span class="ex-dot" data-ex="binance"></span>'
 */
(function () {
  const labels = {
    binance: 'Binance', bybit: 'Bybit', okx: 'OKX', gate: 'Gate',
    kucoin: 'KuCoin', mexc: 'MEXC', bitget: 'Bitget',
    hyperliquid: 'Hyperliquid', aster: 'Aster', ethereal: 'Ethereal',
    whitebit: 'WhiteBIT', bingx: 'BingX', backpack: 'Backpack',
    lighter: 'Lighter', paradex: 'Paradex',
    htx: 'HTX', extended: 'Extended', ourbit: 'Ourbit',
  };
  // Palette: arb.html values (user's choice) — muted, distinct, no CEX-brand clash
  const colors = {
    binance: '#F0B90B', bybit: '#F0842D', okx: '#C8C8C8', gate: '#17C684',
    kucoin: '#09BA86', mexc: '#17D854', bitget: '#00D2C8',
    hyperliquid: '#64B4FF', aster: '#8A63D2', ethereal: '#C864C8',
    whitebit: '#2DCCCD', bingx: '#1DB8F2', backpack: '#4ADE80',
    lighter: '#A78BFA', paradex: '#FF6A6A',
    htx: '#2E7DF6', extended: '#E879F9', ourbit: '#FFB84D',
  };

  function dot(ex) {
    const key = (ex || '').toLowerCase();
    return `<span class="ex-dot" data-ex="${key}"></span>`;
  }
  function chip(ex, opts) {
    const key = (ex || '').toLowerCase();
    const label = labels[key] || ex || '—';
    const extra = (opts && opts.suffix) ? ' ' + opts.suffix : '';
    return `<span class="ex-chip">${dot(key)}<span class="ex-name">${label}${extra}</span></span>`;
  }

  // Inject shared CSS once — keeps every page's .ex-dot visuals identical.
  if (!document.getElementById('ex-shared-css')) {
    const rules = [
      '.ex-dot{width:7px;height:7px;border-radius:50%;display:inline-block;flex-shrink:0;vertical-align:baseline;box-shadow:0 0 0 1px rgba(0,0,0,.25) inset}',
      '.ex-chip{display:inline-flex;align-items:center;gap:6px;font-weight:600;font-size:12px;color:var(--text,#E6E8E3);}',
      '.ex-chip .ex-name{letter-spacing:.01em}',
    ];
    Object.entries(colors).forEach(([k, v]) => {
      rules.push(`.ex-dot[data-ex="${k}"]{background:${v}}`);
    });
    const el = document.createElement('style');
    el.id = 'ex-shared-css';
    el.textContent = rules.join('\n');
    document.head.appendChild(el);
  }

  window.EX = { labels, colors, dot, chip };
})();
