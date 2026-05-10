"""Branded HTML email templates.

Constraints (email-safe CSS):
- Inline styles only — class/id selectors get stripped by Gmail / Outlook
- Table-based layout for legacy clients (Outlook desktop in particular)
- No position/flex/grid/transforms — render is unreliable
- Width capped at 560px — standard for mobile + desktop alike
- Dark theme; some clients (Outlook web) force light mode and we accept
  that — readable in both

Each template returns (subject, text_body, html_body) for use with
mailer.send(to, subject, body, html=html).
"""
from __future__ import annotations

GREEN = "#1AFFAB"
GREEN_DARK = "#0f4d3a"
BG = "#0A0A0E"
SURFACE = "#13141A"
SURFACE_2 = "#1A1B22"
BORDER = "#26272E"
TEXT = "#E8E8EC"
TEXT_2 = "#9C9CA8"
TEXT_3 = "#5C5C6A"


def _envelope(content_html: str, preheader: str = "") -> str:
    """Wrap content with the outer table + preheader. Preheader is the
    hidden snippet that appears in inbox previews next to the subject."""
    preview = (
        f'<div style="display:none;font-size:1px;color:{BG};line-height:1px;'
        f'max-height:0;max-width:0;opacity:0;overflow:hidden">'
        f'{preheader}</div>' if preheader else ''
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<title>Avalant</title>
</head>
<body style="margin:0;padding:0;background:{BG};color:{TEXT};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased">
{preview}
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:{BG};padding:32px 16px">
  <tr>
    <td align="center">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="560" style="width:100%;max-width:560px;background:{SURFACE};border:1px solid {BORDER};border-radius:14px;overflow:hidden">
        <tr>
          <td style="padding:28px 32px 12px">
            <a href="https://avalant.xyz" style="text-decoration:none;color:{TEXT};font-weight:800;font-size:18px;letter-spacing:-0.02em">
              avalant<span style="color:{GREEN}">_</span>
            </a>
          </td>
        </tr>
        {content_html}
      </table>
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="560" style="width:100%;max-width:560px;margin-top:18px">
        <tr>
          <td align="center" style="padding:0 16px;color:{TEXT_3};font-size:11px;line-height:1.65">
            Avalant — crypto arbitrage &amp; portfolio analytics<br>
            <a href="https://avalant.xyz" style="color:{TEXT_3};text-decoration:underline">avalant.xyz</a>
            &nbsp;·&nbsp;
            <a href="https://avalant.xyz/terms" style="color:{TEXT_3};text-decoration:underline">Terms</a>
            &nbsp;·&nbsp;
            <a href="https://avalant.xyz/privacy" style="color:{TEXT_3};text-decoration:underline">Privacy</a>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>"""


def confirm_code(code: str) -> tuple[str, str, str]:
    """6-digit confirmation code used for 2FA setup / disable /
    recovery-code regeneration on Google-only accounts."""
    subject = "Your Avalant confirmation code"

    text = (
        f"Your Avalant confirmation code is: {code}\n\n"
        f"Enter this code on the page where you requested it. It's valid for\n"
        f"10 minutes and can only be used once.\n\n"
        f"If you didn't request this code, you can safely ignore this email —\n"
        f"no action was taken on your account.\n\n"
        f"— Avalant\n"
        f"https://avalant.xyz\n"
    )

    # Pretty 6-digit display: "123 456" with extra letter-spacing
    pretty = code[:3] + " " + code[3:] if len(code) == 6 else code

    content = f"""
        <tr>
          <td style="padding:8px 32px 0">
            <div style="display:inline-block;padding:5px 11px;border-radius:999px;background:rgba(26,255,171,0.08);border:1px solid rgba(26,255,171,0.28);color:{GREEN};font-size:11px;font-weight:600;letter-spacing:0.04em;text-transform:uppercase">Security</div>
          </td>
        </tr>
        <tr>
          <td style="padding:18px 32px 0">
            <h1 style="margin:0;font-size:24px;font-weight:600;color:{TEXT};letter-spacing:-0.01em;line-height:1.25">Your confirmation code</h1>
            <p style="margin:10px 0 0;font-size:14px;color:{TEXT_2};line-height:1.6">
              Enter this code on the Avalant page where you requested it.
            </p>
          </td>
        </tr>
        <tr>
          <td style="padding:24px 32px 6px">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:{SURFACE_2};border:1px solid {BORDER};border-radius:12px">
              <tr>
                <td align="center" style="padding:28px 16px">
                  <div style="font-family:ui-monospace,'JetBrains Mono','SF Mono',Menlo,Consolas,monospace;font-size:36px;font-weight:700;letter-spacing:0.18em;color:{GREEN};line-height:1">{pretty}</div>
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:18px 32px 0">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
              <tr>
                <td style="padding:14px 16px;border-radius:10px;background:rgba(229,192,123,0.06);border:1px solid rgba(229,192,123,0.22);font-size:12.5px;color:{TEXT_2};line-height:1.6">
                  <strong style="color:#E5C07B">Valid for 10 minutes &middot; single-use.</strong><br>
                  Don't share this code with anyone — Avalant staff will never ask for it.
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:18px 32px 28px">
            <p style="margin:0;font-size:12px;color:{TEXT_3};line-height:1.6">
              Didn't request this code? You can safely ignore this email — no action was taken on your account.
            </p>
          </td>
        </tr>
"""
    html = _envelope(content, preheader=f"Your Avalant confirmation code: {pretty}")
    return subject, text, html
