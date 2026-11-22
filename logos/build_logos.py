"""
Generates the StrongHire logo set (bold pictogram style, based on the reference screenshot):
two people facing each other across a desk with a laptop in between.

Run:  python logos/build_logos.py
Outputs SVG files into logos/ (same folder as this script).
"""
from pathlib import Path

OUT = Path(__file__).parent

# ---------------------------------------------------------------------------
# Core pictogram (viewBox 0 0 240 150). `fg` = ink colour, `bg` = cut-out colour.
# ---------------------------------------------------------------------------

def person_left(fg: str) -> str:
    return f"""
    <circle cx="52" cy="32" r="18" fill="{fg}"/>
    <path fill="{fg}" d="
      M 22 106 L 22 80
      C 22 64 33 56 47 56 L 57 56
      C 69 56 75 62 79 71
      L 86 88 L 98 88
      C 103 88 105 91 105 95 L 105 106 Z"/>"""


def person_right(fg: str) -> str:
    # mirror of the left person around x = 120
    return f'<g transform="translate(240,0) scale(-1,1)">{person_left(fg)}</g>'


def desk(fg: str) -> str:
    return f"""
    <rect x="34" y="100" width="172" height="12" rx="4" fill="{fg}"/>
    <path d="M 60 112 L 60 134 Q 60 138 64 138 L 176 138 Q 180 138 180 134 L 180 112"
          fill="none" stroke="{fg}" stroke-width="9" stroke-linejoin="round"/>"""


def laptop(fg: str, bg: str, code_on_screen: bool = True) -> str:
    code = ""
    if code_on_screen:
        code = f"""
    <g fill="none" stroke="{bg}" stroke-width="4.2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M 111 72 L 104 79 L 111 86"/>
      <path d="M 129 72 L 136 79 L 129 86"/>
      <path d="M 123 70 L 117 88"/>
    </g>"""
    return f"""
    <path fill="{fg}" d="M 92 58 L 148 58 Q 152 58 151.4 62 L 146 100 L 94 100 L 88.6 62 Q 88 58 92 58 Z"/>{code}"""


def code_above(fg: str) -> str:
    return f"""
    <g fill="none" stroke="{fg}" stroke-width="6" stroke-linecap="round" stroke-linejoin="round">
      <path d="M 106 18 L 96 28 L 106 38"/>
      <path d="M 134 18 L 144 28 L 134 38"/>
      <path d="M 124 14 L 116 42"/>
    </g>"""


def check_above(fg: str) -> str:
    return f"""
    <g fill="none" stroke="{fg}" stroke-width="7" stroke-linecap="round" stroke-linejoin="round">
      <path d="M 106 28 L 116 38 L 136 16"/>
    </g>"""


_mask_counter = [0]


def pictogram(fg: str, bg: str, extra: str = "", code_on_screen: bool = True) -> str:
    """
    People are drawn behind the desk/laptop. A mask cuts a thin transparent gap
    around the desk and laptop so the shapes read separately (works on any background).
    """
    _mask_counter[0] += 1
    mid = f"cut{_mask_counter[0]}"
    halo = (
        f'<g fill="#000" stroke="#000" stroke-width="10" stroke-linejoin="round">'
        f'<rect x="34" y="100" width="172" height="12" rx="4"/>'
        f'<path d="M 92 58 L 148 58 Q 152 58 151.4 62 L 146 100 L 94 100 L 88.6 62 Q 88 58 92 58 Z"/>'
        f"</g>"
    )
    mask = (
        f'<defs><mask id="{mid}" maskUnits="userSpaceOnUse" x="-20" y="-20" width="280" height="200">'
        f'<rect x="-20" y="-20" width="280" height="200" fill="#fff"/>{halo}</mask></defs>'
    )
    people = f'<g mask="url(#{mid})">{person_left(fg)}{person_right(fg)}</g>'
    return mask + people + desk(fg) + laptop(fg, bg, code_on_screen) + extra


FONT = "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"


def svg(viewbox: str, body: str, bg_rect: str = "") -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox}">{bg_rect}{body}\n</svg>\n'


files = {}

# 1. Icon only, black (transparent background) - primary mark
files["stronghire_icon_black.svg"] = svg("0 0 240 150", pictogram("#000", "#fff"))

# 2. Icon only, white (for dark backgrounds)
files["stronghire_icon_white.svg"] = svg("0 0 240 150", pictogram("#fff", "#0a0a0a"))

# 3. Icon with </> floating above (code-pair variant)
files["stronghire_icon_code_above.svg"] = svg(
    "0 -10 240 160", pictogram("#000", "#fff", code_above("#000"), code_on_screen=False)
)

# 4. Icon with checkmark above ("Strong Hire" verdict variant)
files["stronghire_icon_check_above.svg"] = svg(
    "0 -10 240 160", pictogram("#000", "#fff", check_above("#000"))
)

# 5. App icon / favicon: white pictogram in black squircle
files["stronghire_app_icon.svg"] = svg(
    "0 0 512 512",
    f'<g transform="translate(46,105) scale(1.75)">{pictogram("#fff", "#000")}</g>',
    '<rect width="512" height="512" rx="112" fill="#000"/>',
)

# 6. Horizontal lockup, light (navbar)
files["stronghire_horizontal_light.svg"] = svg(
    "0 0 760 170",
    f"""
  <g transform="translate(10,10)">{pictogram("#000", "#fff")}</g>
  <text x="275" y="92" font-family="{FONT}" font-size="64" font-weight="800" letter-spacing="-1.5" fill="#000">Strong<tspan font-weight="400">Hire</tspan></text>
  <text x="279" y="130" font-family="{MONO}" font-size="17" letter-spacing="4" fill="#555">SWE MOCK INTERVIEWS</text>""",
)

# 7. Horizontal lockup, dark
files["stronghire_horizontal_dark.svg"] = svg(
    "0 0 760 170",
    f"""
  <g transform="translate(10,10)">{pictogram("#fff", "#0a0a0a")}</g>
  <text x="275" y="92" font-family="{FONT}" font-size="64" font-weight="800" letter-spacing="-1.5" fill="#fff">Strong<tspan font-weight="400">Hire</tspan></text>
  <text x="279" y="130" font-family="{MONO}" font-size="17" letter-spacing="4" fill="#a1a1aa">SWE MOCK INTERVIEWS</text>""",
    '<rect width="760" height="170" fill="#0a0a0a"/>',
)

# 8. Stacked lockup (icon above wordmark)
files["stronghire_stacked.svg"] = svg(
    "0 0 400 330",
    f"""
  <g transform="translate(80,20)">{pictogram("#000", "#fff")}</g>
  <text x="200" y="240" text-anchor="middle" font-family="{FONT}" font-size="50" font-weight="800" letter-spacing="-1" fill="#000">Strong<tspan font-weight="400">Hire</tspan></text>
  <text x="200" y="280" text-anchor="middle" font-family="{MONO}" font-size="14" letter-spacing="4" fill="#555">SWE MOCK INTERVIEWS</text>""",
)

for name, content in files.items():
    (OUT / name).write_text(content)

# ---------------------------------------------------------------------------
# YouTube profile pictures (800x800, YouTube crops to a circle -> keep content centred)
# ---------------------------------------------------------------------------
yt_files = {
    "youtube_profile_icon.svg": (
        '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="800" viewBox="0 0 800 800">'
        '<rect width="800" height="800" fill="#000"/>'
        f'<g transform="translate(112,220) scale(2.4)">{pictogram("#fff", "#000")}</g>'
        "</svg>\n"
    ),
    "youtube_profile_named.svg": (
        '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="800" viewBox="0 0 800 800">'
        '<rect width="800" height="800" fill="#000"/>'
        f'<g transform="translate(160,165) scale(2.0)">{pictogram("#fff", "#000")}</g>'
        f'<text x="400" y="560" text-anchor="middle" font-family="{FONT}" font-size="82" '
        f'font-weight="800" letter-spacing="-2" fill="#fff">Strong<tspan font-weight="400">Hire</tspan></text>'
        "</svg>\n"
    ),
}
# Monogram "SH": same typography as the wordmark (S bold like "Strong", H regular like "Hire")
yt_files["youtube_profile_monogram.svg"] = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="800" viewBox="0 0 800 800">'
    '<rect width="800" height="800" fill="#000"/>'
    f'<text x="400" y="400" text-anchor="middle" dominant-baseline="central" font-family="{FONT}" '
    f'font-size="400" font-weight="800" letter-spacing="-12" fill="#fff">S<tspan font-weight="400">H</tspan></text>'
    "</svg>\n"
)
for name, content in yt_files.items():
    (OUT / name).write_text(content)
files.update(yt_files)

# ---------------------------------------------------------------------------
# YouTube channel banner (2560x1440). Safe area visible on all devices is the
# centred 1546x423 box (x 507-2053, y 508-931): the lockup is kept inside it.
# ---------------------------------------------------------------------------
banner_lockup = f"""
  <g transform="translate(10,10)">{pictogram("#fff", "#0a0a0a")}</g>
  <text x="275" y="92" font-family="{FONT}" font-size="64" font-weight="800" letter-spacing="-1.5" fill="#fff">Strong<tspan font-weight="400">Hire</tspan></text>
  <text x="279" y="130" font-family="{MONO}" font-size="17" letter-spacing="4" fill="#a1a1aa">SWE MOCK INTERVIEWS</text>"""
banner = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="2560" height="1440" viewBox="0 0 2560 1440">'
    '<rect width="2560" height="1440" fill="#0a0a0a"/>'
    f'<g transform="translate(587,533) scale(2.2)">{banner_lockup}</g>'
    "</svg>\n"
)
(OUT / "youtube_banner.svg").write_text(banner)
files["youtube_banner.svg"] = banner

# Preview page
cards = "".join(
    f'<div class="card {"dark" if ("white" in n or "dark" in n) else ""}"><img src="{n}"><p>{n}</p></div>'
    for n in files
)
(OUT / "preview.html").write_text(f"""<!doctype html><html><head><meta charset="utf-8"><title>StrongHire logos</title>
<style>
body{{font-family:-apple-system,sans-serif;background:#f4f4f5;margin:0;padding:24px;display:grid;grid-template-columns:repeat(4,1fr);gap:16px}}
.card{{background:#fff;border:1px solid #e4e4e7;border-radius:12px;padding:20px;display:flex;flex-direction:column;align-items:center;justify-content:center}}
.card.dark{{background:#0a0a0a}} .card img{{width:100%;height:160px;object-fit:contain}}
.card p{{font-size:11px;color:#71717a;margin:10px 0 0}}
</style></head><body>{cards}</body></html>""")

print("Generated:", ", ".join(files))
