"""
Alternative "SH" monogram profile pictures (800x800, circle-safe).
Run: python3 logos/build_monograms.py  -> writes logos/monograms/*.svg
"""
from pathlib import Path

OUT = Path(__file__).parent / "monograms"
OUT.mkdir(exist_ok=True)

SANS = "'Inter', -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Helvetica Neue', Arial, sans-serif"
MONO = "ui-monospace, 'SF Mono', SFMono-Regular, Menlo, Consolas, monospace"


def wrap(body: str, bg: str = "#000") -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="800" viewBox="0 0 800 800">'
        f'<rect width="800" height="800" fill="{bg}"/>{body}</svg>\n'
    )


designs = {}

# A. Code tag  <SH/>
designs["a_code_tag"] = wrap(f"""
<text x="400" y="400" text-anchor="middle" dominant-baseline="central" font-family="{SANS}" font-size="180" font-weight="900" letter-spacing="-6" fill="#fff"><tspan font-family="{MONO}" font-weight="300" fill="#71717a">&lt;</tspan>SH<tspan font-family="{MONO}" font-weight="300" fill="#71717a">/&gt;</tspan></text>""")

# B. Terminal prompt  SH_  (cursor block)
designs["b_terminal"] = wrap(f"""
<text x="160" y="400" dominant-baseline="central" font-family="{MONO}" font-size="300" font-weight="700" fill="#fff">SH</text>
<rect x="525" y="470" width="125" height="34" fill="#fff"/>""")

# C. Badge ring with circular text
designs["c_badge_ring"] = wrap(f"""
<defs><path id="ring" d="M 400 400 m -290 0 a 290 290 0 1 1 580 0 a 290 290 0 1 1 -580 0"/></defs>
<circle cx="400" cy="400" r="345" fill="none" stroke="#fff" stroke-width="6"/>
<circle cx="400" cy="400" r="235" fill="none" stroke="#3f3f46" stroke-width="3"/>
<text font-family="{MONO}" font-size="38" font-weight="600" fill="#fff">
  <textPath href="#ring" startOffset="0" textLength="1805" lengthAdjust="spacing">STRONG HIRE · SWE MOCK INTERVIEWS ·</textPath></text>
<text x="400" y="400" text-anchor="middle" dominant-baseline="central" font-family="{SANS}" font-size="250" font-weight="900" letter-spacing="-10" fill="#fff">SH</text>""")

# D. Split: S white-on-black | H black-on-white
designs["d_split"] = wrap(f"""
<rect x="400" width="400" height="800" fill="#fff"/>
<text x="400" y="400" text-anchor="end" dominant-baseline="central" dx="-14" font-family="{SANS}" font-size="380" font-weight="900" fill="#fff">S</text>
<text x="400" y="400" text-anchor="start" dominant-baseline="central" dx="14" font-family="{SANS}" font-size="380" font-weight="900" fill="#000">H</text>""")

# E. Verified: heavy SH + check badge
designs["e_verified"] = wrap(f"""
<text x="385" y="390" text-anchor="middle" dominant-baseline="central" font-family="{SANS}" font-size="340" font-weight="900" letter-spacing="-16" fill="#fff">SH</text>
<circle cx="600" cy="560" r="68" fill="#fff" stroke="#000" stroke-width="14"/>
<path d="M 566 562 L 590 586 L 636 536" fill="none" stroke="#000" stroke-width="18" stroke-linecap="round" stroke-linejoin="round"/>""")

# F. Solid S + outlined H
designs["f_solid_outline"] = wrap(f"""
<text x="400" y="400" text-anchor="middle" dominant-baseline="central" font-family="{SANS}" font-size="380" font-weight="900" letter-spacing="-6"><tspan fill="#fff">S</tspan><tspan fill="none" stroke="#fff" stroke-width="9">H</tspan></text>""")

# G. Light version: black SH on white, heavy and tight
designs["g_light_heavy"] = wrap(f"""
<text x="400" y="400" text-anchor="middle" dominant-baseline="central" font-family="{SANS}" font-size="360" font-weight="900" letter-spacing="-10" fill="#000">SH</text>
<rect x="250" y="590" width="300" height="16" rx="8" fill="#000"/>""", bg="#fff")

# H. Rounded tile: white squircle on black with black SH
designs["h_tile"] = wrap(f"""
<rect x="170" y="170" width="460" height="460" rx="110" fill="#fff"/>
<text x="400" y="400" text-anchor="middle" dominant-baseline="central" font-family="{SANS}" font-size="250" font-weight="900" letter-spacing="-12" fill="#000">SH</text>""")

for name, content in designs.items():
    (OUT / f"{name}.svg").write_text(content)

# Final YouTube profile pictures for the two chosen proposals
import shutil  # noqa: E402
FINAL = {"d_split": "youtube_profile_D_split", "e_verified": "youtube_profile_E_verified"}
for src, dst in FINAL.items():
    shutil.copy(OUT / f"{src}.svg", OUT.parent / f"{dst}.svg")
