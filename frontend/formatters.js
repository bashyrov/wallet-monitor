/**
 * Shared formatters.
 *
 * Dedupes helpers that existed inline in screener.html and arb.html. Exposed
 * on `window.FMT` as a namespace so both files can opt-in per-helper without
 * breaking any of their existing locally-scoped `fmtPrice` / `fmtVol` /
 * `fmtApr` definitions (migration happens incrementally).
 *
 * Load AFTER auth.js but before the page-specific inline scripts:
 *   <script src="/formatters.js"></script>
 */
(function () {
  'use strict';

  const mono = (s) => `<span style="font-family:var(--mono)">${s}</span>`;

  const FMT = {
    /** $12,345 / $1.2345 / $0.000012 — USD price with auto-precision. */
    price(p) {
      if (p == null || p === 0) return '—';
      if (p >= 1000) return '$' + p.toLocaleString('en-US', { maximumFractionDigits: 2 });
      if (p >= 1) return '$' + p.toFixed(4);
      return '$' + p.toPrecision(4);
    },

    /** $1.23B / $12.4M / $150K — compact USD volume. */
    volume(v) {
      if (!v || v === 0) return '<span style="color:var(--text3)">—</span>';
      if (v >= 1e9) return '$' + (v / 1e9).toFixed(2) + 'B';
      if (v >= 1e6) return '$' + (v / 1e6).toFixed(2) + 'M';
      if (v >= 1e3) return '$' + (v / 1e3).toFixed(1) + 'K';
      return '$' + v.toFixed(0);
    },

    /** Annualised % with green/red rate class. */
    apr(apr) {
      if (apr == null) return '—';
      const sign = apr >= 0 ? '+' : '';
      const cls = apr > 0 ? 'rate-pos' : apr < 0 ? 'rate-neg' : 'rate-zero';
      return `<span class="td-apr ${cls}">${sign}${Math.abs(apr).toFixed(2)}%</span>`;
    },

    /** Raw rate as % + per-period suffix (1h / 8h). */
    rate(rate, interval_h) {
      const pct = (rate * 100).toFixed(4);
      const sign = rate >= 0 ? '+' : '';
      const cls = rate > 0 ? 'rate-pos' : rate < 0 ? 'rate-neg' : 'rate-zero';
      const lbl = interval_h === 1 ? '1h' : `${interval_h}h`;
      return `<span class="td-rate ${cls}">${sign}${pct}%<span style="font-weight:400;color:var(--text3);font-size:11px"> /${lbl}</span></span>`;
    },

    /** +1.2345% with fixed decimal precision — no colour class. */
    pct(v, decimals = 4) {
      if (v == null) return '—';
      const sign = v >= 0 ? '+' : '';
      return `${sign}${v.toFixed(decimals)}%`;
    },

    /** HH:MM:SS countdown to `ts` (unix seconds). */
    countdown(ts) {
      if (!ts) return '—';
      const diff = ts - Math.floor(Date.now() / 1000);
      if (diff <= 0) return '<span class="next-soon">now</span>';
      const h = Math.floor(diff / 3600),
            m = Math.floor((diff % 3600) / 60),
            s = diff % 60;
      const cls = diff < 600 ? 'next-soon' : '';
      if (h > 0) return `<span class="${cls}">${h}h ${m}m</span>`;
      if (m > 0) return `<span class="${cls}">${m}m ${s}s</span>`;
      return `<span class="next-soon">${s}s</span>`;
    },

    /** Signed-prefix helper for ad-hoc use: `sign(1.5) === '+'`. */
    sign(v) { return v >= 0 ? '+' : ''; },

    /** HTML-escape untrusted string (user tickers, exchange names, URLs). */
    esc(s) {
      return String(s ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    },

    /** "USDT" / "USD" suffix-stripper: "BTCUSDT" -> "BTC". */
    stripUsdt(sym) {
      if (!sym) return sym;
      if (sym.endsWith('USDT')) return sym.slice(0, -4);
      if (sym.endsWith('USD')) return sym.slice(0, -3);
      return sym;
    },
  };

  // Expose. Non-enumerable so it doesn't pollute Object.keys output.
  Object.defineProperty(window, 'FMT', { value: FMT, enumerable: false });
})();
