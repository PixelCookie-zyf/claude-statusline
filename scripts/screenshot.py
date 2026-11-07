#!/usr/bin/env python3
"""Render README screenshots from the demo environment (needs Google Chrome; the GIF also needs Pillow).

    python3 scripts/screenshot.py docs/dark.png  --theme dark
    python3 scripts/screenshot.py docs/light.png --theme light
    python3 scripts/screenshot.py docs/working.gif --theme dark --gif

Each frame is real status line output (via scripts/demo.py) converted from ANSI to HTML, framed as a
terminal window under a placeholder prompt box, and rendered by headless Chrome.
"""
import html, os, re, subprocess, sys, tempfile, time, unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
COLS = 150
SGR = re.compile(r"\x1b\[([0-9;]*)m")
THEMES = {
    "dark": dict(win="#1c1c1e", bar="#232325", fg="#8c8a84", faint="#5c5b58", text="#e0ded8", shadow=".45"),
    "light": dict(win="#fbfaf7", bar="#efede8", fg="#6c6a64", faint="#b4b2ac", text="#262624", shadow=".18"),
}


LEFT = {"▏": 1, "▎": 2, "▍": 3, "▌": 4, "▋": 5, "▊": 6, "▉": 7, "█": 8}
LOWER = {"▁": 1, "▂": 2, "▃": 3, "▄": 4, "▅": 5, "▆": 6, "▇": 7}
LINE = "1.3px"
CORNER = {  # position of the quarter-box that draws each rounded corner, and which borders it uses
    "╭": "left:50%;top:50%;right:0;bottom:0;border-left:{w} solid {c};border-top:{w} solid {c};border-top-left-radius:7px",
    "╮": "left:0;top:50%;right:50%;bottom:0;border-right:{w} solid {c};border-top:{w} solid {c};border-top-right-radius:7px",
    "╰": "left:50%;top:0;right:0;bottom:50%;border-left:{w} solid {c};border-bottom:{w} solid {c};border-bottom-left-radius:7px",
    "╯": "left:0;top:0;right:50%;bottom:50%;border-right:{w} solid {c};border-bottom:{w} solid {c};border-bottom-right-radius:7px",
}


def rgb(c):
    return "rgb(%s,%s,%s)" % c if c else "currentColor"


def cell(ch, fg=None, bg=None):
    """Terminals draw block and box glyphs edge to edge; browsers don't, so paint those with CSS."""
    f, b = rgb(fg), (rgb(bg) if bg else "transparent")
    if ch in LEFT:
        x = LEFT[ch] * 12.5
        return '<span class="c" style="background:linear-gradient(to right,%s 0 %.1f%%,%s %.1f%% 100%%)"></span>' % (
            f, x, b, x)
    if ch in LOWER:
        y = LOWER[ch] * 12.5 * 0.82  # leave a little headroom like a font would
        return '<span class="c" style="background:linear-gradient(to top,%s 0 %.1f%%,%s %.1f%% 100%%)"></span>' % (
            f, y, b, y)
    if ch == "│":
        return '<span class="c" style="background:linear-gradient(%s,%s) center/%s 100%% no-repeat"></span>' % (f, f, LINE)
    if ch == "─":
        return '<span class="c" style="background:linear-gradient(%s,%s) center/100%% %s no-repeat"></span>' % (f, f, LINE)
    if ch in CORNER:
        return '<span class="c" style="position:relative"><i style="position:absolute;%s"></i></span>' % (
            CORNER[ch].format(w=LINE, c=f))
    if ord(ch) < 128:
        return html.escape(ch)
    w = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
    return '<span class="c" style="width:%dch">%s</span>' % (w, html.escape(ch))


def ansi_to_html(text):
    out, fg, bg, bold = [], None, None, False
    for line in text.rstrip("\n").split("\n"):
        pos, parts = 0, []
        for m in list(SGR.finditer(line)) + [None]:
            chunk = line[pos:m.start()] if m else line[pos:]
            if chunk:
                style = ""
                if fg:
                    style += "color:rgb(%s,%s,%s);" % fg
                if bg:
                    style += "background:rgb(%s,%s,%s);" % bg
                if bold:
                    style += "font-weight:700;"
                body = "".join(cell(ch, fg, bg) for ch in chunk)
                parts.append('<span style="%s">%s</span>' % (style, body) if style else body)
            if not m:
                break
            pos = m.end()
            codes = [int(x) for x in m.group(1).split(";") if x] or [0]
            i = 0
            while i < len(codes):
                cde = codes[i]
                if cde == 0:
                    fg, bg, bold = None, None, False
                elif cde == 1:
                    bold = True
                elif cde in (38, 48) and i + 4 < len(codes) and codes[i + 1] == 2:
                    rgb = tuple(codes[i + 2:i + 5])
                    if cde == 38:
                        fg = rgb
                    else:
                        bg = rgb
                    i += 4
                i += 1
        out.append("".join(parts))
    return "\n".join(out)


def page(body, theme):
    t = THEMES[theme]
    rule = "─" * (COLS - 2)
    prompt = ('<span style="color:%s">%s</span>\n<span style="color:%s">❯ </span><span style="color:%s">'
              'add tests for the verifier cookie expiring</span>\n<span style="color:%s">%s</span>\n') % (
        t["faint"], rule, t["fg"], t["text"], t["faint"], rule)
    return """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;background:transparent}
.win{display:inline-block;margin:28px;border-radius:12px;background:%s;overflow:hidden;
     box-shadow:0 20px 50px rgba(0,0,0,%s),0 0 0 1px rgba(127,127,127,.18)}
.bar{height:34px;display:flex;align-items:center;gap:8px;padding:0 14px;background:%s}
.dot{width:12px;height:12px;border-radius:50%%}
pre{margin:0;padding:16px 20px 20px;font:13px/1.42 Menlo,ClaudeMark,monospace;color:%s;width:%dch}
.c{display:inline-block;text-align:center;width:1ch;height:1.42em;vertical-align:top}
</style></head><body><div class="win"><div class="bar">
<span class="dot" style="background:#ff5f57"></span><span class="dot" style="background:#febc2e"></span>
<span class="dot" style="background:#28c840"></span></div><pre>%s%s</pre></div></body></html>""" % (
        t["win"], t["shadow"], t["bar"], t["fg"], COLS, prompt, body)


def shoot(html_text, out_png, rows):
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        f.write(html_text)
    w = int(COLS * 7.83 + 40 + 56 + 4)
    h = int(rows * 13 * 1.42 + 34 + 36 + 56 + 2)
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--default-background-color=00000000", "--force-device-scale-factor=2",
                    "--window-size=%d,%d" % (w, h), "--screenshot=" + out_png, "file://" + f.name],
                   capture_output=True, check=True)
    os.unlink(f.name)


def main():
    a = sys.argv[1:]
    out = os.path.abspath(a[0])
    theme = a[a.index("--theme") + 1] if "--theme" in a else "dark"
    root = tempfile.mkdtemp(prefix="sl-shot-")
    demo.build(root, theme)
    if "--gif" not in a:
        text = demo.render(root, busy=False, columns=COLS)
        shoot(page(ansi_to_html(text), theme), out, rows=3 + text.count("\n"))
        print(out)
        return
    from PIL import Image
    frames, texts = [], []
    for _ in range(18):  # real renders, ~110 ms apart, while "Claude is working"
        texts.append(demo.render(root, busy=True, columns=COLS))
        time.sleep(0.11)
    for i, text in enumerate(texts):
        png = os.path.join(root, "f%02d.png" % i)
        shoot(page(ansi_to_html(text), theme), png, rows=3 + text.count("\n"))
        frames.append(Image.open(png).convert("RGBA"))
    bg = Image.new("RGBA", frames[0].size, (255, 255, 255, 0))
    pal = [Image.alpha_composite(bg, f).convert("P", palette=Image.ADAPTIVE, colors=255) for f in frames]
    pal[0].save(out, save_all=True, append_images=pal[1:], duration=110, loop=0, disposal=2, optimize=True)
    print(out)


if __name__ == "__main__":
    main()
