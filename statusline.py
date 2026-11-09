#!/usr/bin/env python3
"""claude-statusline: a four-line, theme-aware, lightly animated status line for Claude Code.

  line 1   logo · model · effort meter · dir · git · COST + per-turn sparkline · TOKENS · LINES · TIME
  line 2-4 three rounded "batteries":
           context window (stacked by sys / tools / mcp / skills / mem / chat, cache countdown on the frame)
           5h session limit and 7d weekly limit (used % and time to reset)

Reads Claude Code's statusline JSON on stdin and prints ANSI text. Python 3 stdlib only.
Per-session state (sparkline history, transcript scan offsets) lives in ~/.claude/statusline/cache/.
Tweak the constants in the config block below; changes apply on the next refresh.
"""
import json, math, os, subprocess, sys, time, unicodedata

HOME = os.path.expanduser("~")
CACHE_DIR = os.path.join(HOME, ".claude", "statusline", "cache")

# ─────────────────────────── config ───────────────────────────
SPARK_LEN = 8            # how many recent turns the cost sparkline shows
BUSY_WINDOW = 3.0        # transcript touched within N s => "working" (spinner + bright shimmer)
IDLE_SHIMMER = True      # faint drifting shimmer while idle
GIT_TTL = 5.0            # seconds to cache git status
SAFETY = 2               # columns kept free at the right edge

PALETTES = {
    "dark": dict(
        fg=(224, 222, 216), dim=(140, 138, 132), faint=(92, 91, 88),
        track=(46, 46, 49), line=(74, 74, 72), frame=(104, 103, 99), shine=(255, 255, 255), accent=(217, 119, 87),
        good=(118, 190, 140), warn=(224, 174, 92), bad=(228, 106, 98),
        seg=dict(sys=(150, 138, 214), tools=(96, 156, 222), mcp=(84, 186, 176),
                 skills=(214, 172, 92), mem=(208, 132, 178), chat=(217, 119, 87)),
        # effort low -> max: cool to hot, with max in Claude Code's "ultra" purple
        effort=[(84, 186, 176), (118, 190, 140), (224, 174, 92), (226, 116, 84), (178, 132, 250)],
    ),
    "light": dict(
        fg=(38, 38, 36), dim=(108, 106, 100), faint=(168, 166, 160),
        track=(226, 224, 218), line=(196, 194, 188), frame=(176, 174, 168), shine=(140, 138, 132), accent=(196, 94, 62),
        good=(42, 138, 78), warn=(184, 126, 26), bad=(196, 62, 54),
        seg=dict(sys=(116, 98, 196), tools=(50, 112, 196), mcp=(28, 138, 128),
                 skills=(176, 126, 28), mem=(176, 82, 140), chat=(196, 94, 62)),
        effort=[(28, 138, 128), (42, 138, 78), (184, 126, 26), (200, 84, 50), (128, 64, 230)],
    ),
}
SEG_ORDER = ["sys", "tools", "mcp", "skills", "mem", "chat"]
EFFORT_LEVELS = {"low": 1, "medium": 2, "high": 3, "xhigh": 4, "max": 5}
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
EIGHTHS = " ▏▎▍▌▋▊▉█"
# Claude logo. None = auto: the Claude mark if the bundled ClaudeMark font is installed, else "✻".
# Set "✻" to force the asterisk, or "" to hide the logo.
CLAUDE_ICON = None
GAP_AFTER_ICON = "  "     # spacing between the logo and the model name
FRAME_COLORS = True       # coloured battery frames: ctx orange, 5h blue, 7d purple; False = neutral grey
FRAME_TINT = 0.75         # 0..1, how strongly the frame takes on its colour
COST_CHEAP = 0.10         # per-turn cost at or below this is green ...
COST_PRICEY = 1.00        # ... rising through amber to red at this and above
TRACK = "─"
BARS = "▁▂▃▄▅▆▇█"

# ─────────────────────────── ansi helpers ───────────────────────────
RESET = "\x1b[0m"


def fgc(c):
    return "\x1b[38;2;%d;%d;%dm" % c


def bgc(c):
    return "\x1b[48;2;%d;%d;%dm" % c


def mix(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def width_of(s):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


class Seg:
    """A run of styled text with known visible width."""
    __slots__ = ("ansi", "w")

    def __init__(self, ansi="", w=0):
        self.ansi, self.w = ansi, w

    def __add__(self, o):
        return Seg(self.ansi + o.ansi, self.w + o.w)


def T(s, color=None, bold=False):
    pre = ("\x1b[1m" if bold else "") + (fgc(color) if color else "")
    return Seg(pre + s + RESET if pre else s, width_of(s))


def join(segs, sep):
    out = Seg()
    for i, s in enumerate(segs):
        if i:
            out = out + sep
        out = out + s
    return out


# ─────────────────────────── formatting ───────────────────────────
def ftok(n):
    if n is None:
        return "—"
    if n >= 1_000_000:
        v = n / 1_000_000
        return ("%.1fM" % v).replace(".0M", "M")
    if n >= 1000:
        v = n / 1000
        return "%.1fk" % v if v < 10 else "%dk" % round(v)
    return str(int(n))


def fdur(sec):
    sec = max(0, int(sec))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return "%dd%dh" % (d, h)
    if h:
        return "%dh%02dm" % (h, m)
    if m:
        return "%dm" % m
    return "%ds" % sec


def fcost(v):
    return "$%.2f" % v if v < 100 else "$%d" % round(v)


def heat(P, pct):
    """good -> warn -> bad as usage climbs."""
    if pct < 50:
        return P["good"]
    if pct < 80:
        return mix(P["good"], P["warn"], (pct - 50) / 30)
    return mix(P["warn"], P["bad"], (pct - 80) / 20)


# ─────────────────────────── environment ───────────────────────────
def detect_theme(cache):
    """Claude Code theme from settings.json (current) or ~/.claude.json (older); unset/auto -> macOS appearance."""
    name = None
    for path in (os.path.join(HOME, ".claude", "settings.json"), os.path.join(HOME, ".claude.json")):
        try:
            mt = os.path.getmtime(path)
        except OSError:
            continue
        key = "theme:" + path
        th = cache.get(key)
        if not th or th.get("mt") != mt:
            val = None
            try:
                with open(path) as f:
                    val = json.load(f).get("theme")
            except Exception:
                pass
            th = cache[key] = {"mt": mt, "name": val}
        if th["name"]:
            name = th["name"]
            break
    cache.pop("theme", None)
    if not name or name == "auto":
        now = time.time()
        sysd = cache.get("sys_dark")
        if not sysd or now - sysd["t"] > 10:
            dark = False
            try:
                r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                                   capture_output=True, text=True, timeout=0.3)
                dark = "Dark" in r.stdout
            except Exception:
                pass
            sysd = cache["sys_dark"] = {"t": now, "dark": dark}
        return "dark" if sysd["dark"] else "light"
    if name.startswith("custom:"):  # ~/.claude/themes/<slug>.json declares its base theme
        try:
            with open(os.path.join(HOME, ".claude", "themes", name[7:] + ".json")) as f:
                name = str(json.load(f).get("base") or name)
        except Exception:
            pass
    return "light" if "light" in name else "dark"


def term_cols(cache):
    """Live terminal width: ask the tty Claude Code runs on (COLUMNS is frozen at launch)."""
    pid = os.environ.get("CLAUDE_PID") or str(os.getppid())
    tt = cache.get("tty")
    if not tt or tt.get("pid") != pid:
        name = ""
        try:
            r = subprocess.run(["ps", "-o", "tty=", "-p", pid], capture_output=True, text=True, timeout=0.3)
            name = r.stdout.strip()
        except Exception:
            pass
        tt = cache["tty"] = {"pid": pid, "name": name if name and name not in ("??", "?") else ""}
    if tt["name"]:
        try:
            import fcntl, struct, termios
            fd = os.open("/dev/" + tt["name"], os.O_RDONLY | os.O_NOCTTY)
            try:
                cols = struct.unpack("hhhh", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))[1]
            finally:
                os.close(fd)
            if cols > 0:
                return cols
        except Exception:
            pass
    return int(os.environ.get("COLUMNS") or 0) or 120


def git_info(cwd, cache, now):
    g = cache.get("git")
    if g and g.get("cwd") == cwd and now - g.get("t", 0) < GIT_TTL:
        return g
    g = {"cwd": cwd, "t": now, "branch": None, "dirty": False, "ahead": 0, "behind": 0}
    try:
        r = subprocess.run(["git", "-C", cwd, "status", "--porcelain=v2", "--branch"],
                           capture_output=True, text=True, timeout=0.5)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if line.startswith("# branch.head "):
                    g["branch"] = line[14:]
                elif line.startswith("# branch.ab "):
                    a, b = line[12:].split()
                    g["ahead"], g["behind"] = int(a), -int(b)
                elif line and not line.startswith("#"):
                    g["dirty"] = True
    except Exception:
        pass
    cache["git"] = g
    return g


# ─────────────────────────── context breakdown ───────────────────────────
def est_tokens(s):
    """Rough tokenizer-free estimate: ~3.7 chars/token for ASCII, ~0.9 token per non-ASCII char."""
    if not s:
        return 0
    non = sum(1 for ch in s if ord(ch) > 127)
    return (len(s) - non) / 3.7 + non * 0.9


ATTACH_BUCKET = {
    "skill_listing": "skills",
    "mcp_instructions_delta": "mcp",
    "deferred_tools_delta": "tools",
    "agent_listing_delta": "tools",
}


def scan_transcript(path, cache):
    """Incrementally scan the transcript for the request shape (system + tools) and listing attachments."""
    st = cache.get("ctx")
    if not st or st.get("path") != path:
        st = {"path": path, "off": 0, "sys": 0, "tools": 0, "mcp": 0,
              "att": {"skills": 0, "mcp": 0, "tools": 0}}
    try:
        size = os.path.getsize(path)
    except OSError:
        return st
    if size < st["off"]:
        st.update(off=0, att={"skills": 0, "mcp": 0, "tools": 0})
    if size == st["off"]:
        return st
    with open(path, "rb") as f:
        f.seek(st["off"])
        data = f.read()
    nl = data.rfind(b"\n")
    if nl < 0:
        return st
    st["off"] += nl + 1
    for line in data[: nl + 1].split(b"\n"):
        is_shape = b'"type":"api-request-shape"' in line
        is_att = b'"type":"attachment"' in line
        is_compact = b'"compact_boundary"' in line
        is_cost = b'"type":"cost-state"' in line
        if not (is_shape or is_att or is_compact or is_cost):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") == "cost-state":
            # cumulative per-model usage for the whole session (subagents included)
            tot = 0
            for mu in (obj.get("modelUsage") or {}).values():
                tot += sum(int(mu.get(k) or 0) for k in
                           ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"))
            st["tokens"] = tot
        elif obj.get("subtype") == "compact_boundary":
            st["att"] = {"skills": 0, "mcp": 0, "tools": 0}
        elif obj.get("type") == "api-request-shape":
            shape = obj.get("shape") or {}
            sys_t = 0
            for blk in shape.get("system") or []:
                sys_t += est_tokens(blk.get("text", "") if isinstance(blk, dict) else str(blk))
            tools_t = mcp_t = 0
            for tool in shape.get("tools") or []:
                n = est_tokens(json.dumps(tool, ensure_ascii=False))
                if str(tool.get("name", "")).startswith("mcp__"):
                    mcp_t += n
                else:
                    tools_t += n
            st.update(sys=sys_t, tools=tools_t, mcp=mcp_t)
        elif obj.get("type") == "attachment":
            att = obj.get("attachment") or {}
            b = ATTACH_BUCKET.get(att.get("type"))
            if b:
                st["att"][b] += est_tokens(json.dumps(att, ensure_ascii=False))
    cache["ctx"] = st
    return st


def memory_tokens(cwd, transcript):
    files = [os.path.join(HOME, ".claude", "CLAUDE.md")]
    d = os.path.abspath(cwd)
    while True:
        files += [os.path.join(d, "CLAUDE.md"), os.path.join(d, "CLAUDE.local.md"),
                  os.path.join(d, ".claude", "CLAUDE.md")]
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    total = 0.0
    for p in dict.fromkeys(files):
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                total += est_tokens(f.read())
        except OSError:
            pass
    if transcript:  # auto-memory index: first 200 lines are loaded
        mem = os.path.join(os.path.dirname(transcript), "memory", "MEMORY.md")
        try:
            with open(mem, encoding="utf-8", errors="ignore") as f:
                total += est_tokens("".join(f.readlines()[:200]))
        except OSError:
            pass
    return total


def breakdown(used, st, mem):
    fixed = {
        "sys": st["sys"],
        "tools": st["tools"] + st["att"]["tools"],
        "mcp": st["mcp"] + st["att"]["mcp"],
        "skills": st["att"]["skills"],
        "mem": mem,
    }
    s = sum(fixed.values())
    if used and s > used * 0.98:  # estimates overshoot: scale them into the real total
        k = used * 0.98 / s
        fixed = {a: v * k for a, v in fixed.items()}
        s = used * 0.98
    fixed["chat"] = max(0, (used or 0) - s)
    return fixed


# ─────────────────────────── widgets ───────────────────────────
def shimmer_amount(i, width, now, busy):
    if not busy and not IDLE_SHIMMER:
        return 0.0
    speed, band, strength = (14.0, 3.0, 0.45) if busy else (3.0, 4.0, 0.12)
    span = width + band * 4
    pos = (now * speed) % span - band * 2
    d = abs(i - pos)
    return 0.0 if d >= band else strength * (0.5 + 0.5 * math.cos(math.pi * d / band))


def stacked_bar(P, parts, total, width, now, busy):
    """parts: [(color, tokens)]; renders with 1/8-cell precision and blends colors at boundaries."""
    units = width * 8
    cells = []
    for color, v in parts:
        if v <= 0 or not total:
            continue
        cells += [color] * max(1, round(v / total * units))
    cells = cells[:units] + [None] * max(0, units - len(cells))
    out = Seg()
    for i in range(width):
        sub = cells[i * 8:(i + 1) * 8]
        left = sub[0]
        n = 1
        while n < 8 and sub[n] == left:
            n += 1
        rest = [c for c in sub[n:]]
        right = max(set(rest), key=rest.count) if rest else left
        k = shimmer_amount(i, width, now, busy)
        if left is None:  # empty: let the terminal background show through
            out = out + Seg(" ", 1)
            continue
        lc = mix(left, P["shine"], k)
        if n == 8 or right is None and n >= 7:
            out = out + Seg(fgc(lc) + "█" + RESET, 1)
        elif right is None:
            out = out + Seg(fgc(lc) + EIGHTHS[n] + RESET, 1)
        else:
            out = out + Seg(fgc(lc) + bgc(mix(right, P["shine"], k)) + EIGHTHS[n] + RESET, 1)
    return out


def mini_bar(P, pct, width, color):
    filled = max(0.0, min(1.0, pct / 100)) * width * 8
    out = Seg()
    for i in range(width):
        e = int(max(0, min(8, filled - i * 8)))
        if e == 8:
            out = out + Seg(fgc(color) + "█" + RESET, 1)
        elif e == 0:
            out = out + Seg(" ", 1)
        else:
            out = out + Seg(fgc(color) + EIGHTHS[e] + RESET, 1)
    return out


def pad(seg, w):
    return seg + Seg(" " * max(0, w - seg.w), max(0, w - seg.w))


def battery(P, inner, inner_w, tip_color, x0, span, now, busy, tl=None, tr=None, bl=None, hue=None):
    """Rounded-box battery, labels embedded in the frame. Returns (top, mid, bot), each inner_w + 4 wide."""
    tinted = FRAME_COLORS and hue is not None
    base = mix(P["frame"], hue, FRAME_TINT) if tinted else P["frame"]
    glow = P["fg"] if tinted else P["accent"]  # shimmer toward higher contrast on a coloured frame

    def fc(x):
        return mix(base, glow, shimmer_amount(x, span, now, busy) * (1.2 if tinted else 1.6))

    def edge(lc, rc, left_lab, right_lab):
        out = T(lc, fc(x0)) + T("─", fc(x0 + 1))
        x = x0 + 2
        room = inner_w - 2
        labs = []
        for lab in (left_lab, right_lab):
            if lab is not None and lab.w + 2 <= room:
                labs.append(T(" ") + lab + T(" "))
                room -= lab.w + 2
            else:
                labs.append(None)
        if labs[0]:
            out = out + labs[0]
            x += labs[0].w
        fill = room
        for k in range(fill):
            out = out + T("─", fc(x + k))
        x += fill
        if labs[1]:
            out = out + labs[1]
            x += labs[1].w
        return out + T("─", fc(x)) + T(rc, fc(x + 1))

    top = edge("╭", "╮", tl, tr) + T("  ")
    bot = edge("╰", "╯", bl, None) + T("  ")
    mid = T("│", fc(x0)) + pad(inner, inner_w) + T("│", fc(x0 + inner_w + 1)) + T("▪", tip_color) + T(" ")
    return top, mid, bot


def split_bar(P, add, rem, width=4):
    """Tiny added/removed ratio bar."""
    tot = add + rem
    if not tot:
        return T("▬" * width, P["line"])
    g = round(add / tot * width)
    g = max(1 if add else 0, min(width - (1 if rem else 0), g))
    return T("▬" * g, P["good"]) + T("▬" * (width - g), P["bad"])


def cost_color(P, v):
    """Per-turn cost -> good / warn / bad on a log scale between COST_CHEAP and COST_PRICEY."""
    if v <= COST_CHEAP:
        return P["good"]
    t = math.log(v / COST_CHEAP) / math.log(COST_PRICEY / COST_CHEAP)
    return mix(P["good"], P["warn"], t * 2) if t < 0.5 else mix(P["warn"], P["bad"], t * 2 - 1)


def sparkline(P, vals, grow_t):
    """Height = cost relative to the priciest recent turn; colour = what the turn actually cost."""
    out = T("▁" * (SPARK_LEN - len(vals)), P["line"])  # placeholder baseline until history fills
    if not vals:
        return out
    mx = max(vals) or 1
    n = len(vals)
    for i, v in enumerate(vals):
        h = v / mx
        if i == n - 1 and grow_t < 1:  # newest bar grows in
            h *= 1 - (1 - grow_t) ** 3
        idx = max(0, min(7, int(round(h * 7))))
        col = mix(P["faint"], cost_color(P, v), 0.55 + 0.45 * (i + 1) / n)  # older turns fade a little
        out = out + T(BARS[idx], col)
    return out


def _auto_icon():
    for d in (os.path.join(HOME, "Library", "Fonts"), "/Library/Fonts",
              os.path.join(HOME, ".local", "share", "fonts"), os.path.join(HOME, ".fonts")):
        if os.path.exists(os.path.join(d, "ClaudeMark.ttf")):
            return "\U0010F914"
    return "\u273b"


if CLAUDE_ICON is None:
    CLAUDE_ICON = _auto_icon()


def claude_mark(P, now, busy):
    """Claude logo (ClaudeMark font glyph, or the ✻ fallback); breathes while working."""
    if not CLAUDE_ICON:
        return Seg()
    if not busy:
        return T(CLAUDE_ICON, P["accent"]) + T(GAP_AFTER_ICON)
    if CLAUDE_ICON == "✻":
        frames = "✢✶✻✶"
        ch = frames[int(now * 4) % len(frames)]
    else:
        ch = CLAUDE_ICON
    glow = 0.5 + 0.5 * math.sin(now * 4.0)
    return T(ch, mix(P["accent"], P["shine"], 0.3 * glow), bold=True) + T(GAP_AFTER_ICON)


def effort_meter(P, level):
    lv = EFFORT_LEVELS.get(level, 0)
    out = Seg()
    for i in range(5):
        out = out + T("▂▃▄▅▆"[i], P["effort"][lv - 1] if i < lv else P["faint"])
    return out


# ─────────────────────────── layout ───────────────────────────
def fit(left, right, avail, gap=3):
    """left/right: [(prio, Seg)]. Drop lowest-priority items until the line fits, then right-align `right`."""
    left, right = list(left), list(right)
    sep = T(" " * gap)

    def total():
        lw = sum(s.w for _, s in left) + gap * max(0, len(left) - 1)
        rw = sum(s.w for _, s in right) + gap * max(0, len(right) - 1)
        return lw + rw + (gap if left and right else 0), lw, rw

    while total()[0] > avail and len(left) + len(right) > 1:
        pool = [(p, 0, i) for i, (p, _) in enumerate(left)] + [(p, 1, i) for i, (p, _) in enumerate(right)]
        _, side, idx = min(pool)
        (left if side == 0 else right).pop(idx)
    _, lw, rw = total()
    out = join([s for _, s in left], sep)
    if right:
        out = out + T(" " * max(gap if left else 0, avail - lw - rw)) + join([s for _, s in right], sep)
    return out


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
        try:  # keep the latest input around for debugging
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(os.path.join(CACHE_DIR, "last-input.json"), "w") as f:
                f.write(raw)
        except OSError:
            pass
    except Exception:
        data = {}
    now = time.time()
    sid = data.get("session_id") or "default"
    os.makedirs(CACHE_DIR, exist_ok=True)
    cpath = os.path.join(CACHE_DIR, sid + ".json")
    try:
        with open(cpath) as f:
            cache = json.load(f)
    except Exception:
        cache = {}

    P = PALETTES[detect_theme(cache)]
    cols = term_cols(cache)
    avail = max(40, cols - SAFETY)

    cwd = (data.get("workspace") or {}).get("current_dir") or data.get("cwd") or os.getcwd()
    transcript = data.get("transcript_path") or ""
    try:
        busy = transcript and now - os.path.getmtime(transcript) < BUSY_WINDOW
    except OSError:
        busy = False

    # ── line 1 ──
    model = (data.get("model") or {}).get("display_name") or "Claude"
    effort = (data.get("effort") or {}).get("level") or os.environ.get("CLAUDE_EFFORT") or ""
    m = T(model, P["fg"], bold=True)
    if effort:
        m = m + T("  EFFORT ", P["faint"]) + effort_meter(P, effort)
    lv = EFFORT_LEVELS.get(effort, 0)
    eff_label = T(effort, P["effort"][lv - 1] if lv else P["dim"]) if effort else None
    if data.get("fast_mode"):
        eff_label = (eff_label + T(" fast", P["accent"])) if eff_label else T("fast", P["accent"])

    home_rel = cwd.replace(HOME, "~", 1) if cwd.startswith(HOME) else cwd
    parts = home_rel.split("/")
    short = "/".join(parts[-2:]) if len(parts) > 3 else home_rel
    loc = T(short, P["dim"])
    g = git_info(cwd, cache, now)
    gseg = None
    if g.get("branch"):
        gseg = T(g["branch"], P["fg"])
        if g["dirty"]:
            gseg = gseg + T("*", P["warn"])
        if g["ahead"]:
            gseg = gseg + T(" ↑%d" % g["ahead"], P["dim"])
        if g["behind"]:
            gseg = gseg + T(" ↓%d" % g["behind"], P["dim"])

    cost = data.get("cost") or {}
    tc = float(cost.get("total_cost_usd") or 0)
    hist = cache.get("cost_hist") or []
    last = cache.get("last_cost")
    if last is None:
        cache["last_cost"] = tc
    elif tc > last + 0.0005:
        hist = (hist + [tc - last])[-SPARK_LEN:]
        cache.update(last_cost=tc, cost_hist=hist, grow_at=now)
    elif tc < last:  # cost reset (new session state)
        cache.update(last_cost=tc, cost_hist=[])
        hist = []
    grow_t = min(1.0, (now - cache.get("grow_at", 0)) / 1.2)
    cseg = T("COST ", P["faint"]) + T(fcost(tc), P["fg"])
    spark = sparkline(P, hist, grow_t)

    add, rem = int(cost.get("total_lines_added") or 0), int(cost.get("total_lines_removed") or 0)
    lseg = T("LINES ", P["faint"]) + T("+%d" % add, P["good"]) + T(" ") + split_bar(P, add, rem) + T(" ") + T("−%d" % rem, P["bad"])
    dseg = T("TIME ", P["faint"]) + T(fdur((cost.get("total_duration_ms") or 0) / 1000), P["dim"])

    left1 = [(100, claude_mark(P, now, busy) + m + (T(" ") + eff_label if eff_label else Seg())), (70, loc)]
    if gseg:
        left1.append((75, gseg))
    left1.append((88, cseg + (T(" ") + spark + T(" /turn", P["faint"]) if spark.w else Seg())))
    tok_total = (scan_transcript(transcript, cache) if transcript else {}).get("tokens")
    if tok_total:
        left1.append((80, T("TOKENS ", P["faint"]) + T(ftok(tok_total), P["fg"])))
    left1.append((60, lseg))
    left1.append((45, dseg))

    rl = data.get("rate_limits") or {}

    # ── battery rows ──
    cw = data.get("context_window") or {}
    size = cw.get("context_window_size") or 200_000
    cu = cw.get("current_usage") or {}
    used = None
    if cu:
        used = (cu.get("input_tokens") or 0) + (cu.get("cache_creation_input_tokens") or 0) \
            + (cu.get("cache_read_input_tokens") or 0)
    pct = cw.get("used_percentage")
    if pct is None and used is not None:
        pct = used / size * 100
    pct = float(pct or 0)

    st = scan_transcript(transcript, cache) if transcript else \
        {"sys": 0, "tools": 0, "mcp": 0, "att": {"skills": 0, "mcp": 0, "tools": 0}}
    bd = breakdown(used, st, memory_tokens(cwd, transcript))
    pcol = heat(P, pct)

    # ctx text column: cache countdown / percent + tokens / legend
    cache_seg = Seg()
    pc = data.get("prompt_cache") or {}
    if pc.get("expires_at"):
        left_s = pc["expires_at"] - now
        if pc.get("warm") and left_s > 0:
            cache_seg = T("cache ", P["faint"]) + T(fdur(left_s), P["warn"] if left_s < 300 else P["dim"])
        else:
            cache_seg = T("cache cold", P["faint"])
    # ctx text column, same rhythm as the limit batteries: name / percent / tokens
    ctx_text = [T("context", P["faint"]),
                T("%d%%" % round(pct), pcol, bold=True) + T(" used", P["faint"]),
                T(ftok(used), P["fg"]) + T(" tokens", P["faint"])]
    stat_w = max(t.w for t in ctx_text)
    leg = lambda items: join([T(k, P["seg"][k]) + T(" " + ftok(v), P["dim"]) for k, v in items], T("  "))
    legends = [leg([(k, bd[k]) for k in SEG_ORDER]),
               leg([(k, bd[k]) for k in SEG_ORDER if bd[k] >= 1]), Seg()]

    # small batteries for the 5h / 7d limits
    smalls = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        r = rl.get(key)
        seen = cache.setdefault("seen_limits", {})
        if r:
            seen[key] = True
        elif seen.get(key):
            # Claude Code omits a limit once its window has lapsed and no new one has started yet:
            # keep the battery on screen, empty, instead of letting it vanish.
            name = "session limit" if key == "five_hour" else "weekly limit"
            smalls.append(dict(label=label, pct=0, col=P["good"],
                               text=[T(name, P["faint"]), T("0%", P["good"], bold=True) + T(" used", P["faint"]),
                                     T("starts on next msg", P["faint"])]))
            continue
        else:
            continue
        rp = float(r.get("used_percentage") or 0)
        rc = heat(P, rp)
        reset = Seg()
        if r.get("resets_at"):
            left_r = r["resets_at"] - now
            reset = T("resets in ", P["faint"]) + T(fdur(left_r), P["dim"]) if left_r > 0 else T("resetting", P["faint"])
        name = "session limit" if key == "five_hour" else "weekly limit"
        smalls.append(dict(label=label, pct=rp, col=rc,
                           text=[T(name, P["faint"]), T("%d%%" % round(rp), rc, bold=True) + T(" used", P["faint"]),
                                 reset]))
    SMALL_IN, GAP, PREFIX, BAR_MAX = 8, 3, 2, 72
    title = T(ftok(size), P["dim"])  # window size, parallel to the 5h / 7d time windows

    def small_w(sm):
        return SMALL_IN + 4 + max(t.w for t in sm["text"])

    def plan():
        ns = len(smalls)
        order = [(ns, 0), (ns, 1), (min(ns, 1), 0), (min(ns, 1), 1), (min(ns, 1), 2), (0, 0), (0, 1), (0, 2)]
        for n, li in dict.fromkeys(order):
            lg = legends[li]
            need = max(16, lg.w + 4 if lg.w else 0)
            if True:
                rest = PREFIX + 4 + stat_w + sum(GAP + small_w(sm) for sm in smalls[:n])
                bw = min(BAR_MAX, avail - rest)
                if bw >= need:
                    return bw, lg, smalls[:n]
        return max(8, avail - PREFIX - 4 - stat_w), Seg(), []
    bar_w, legend, shown = plan()

    spin = T(SPIN[int(now * 10) % len(SPIN)], P["accent"]) if busy else T("·", P["faint"])
    bar = stacked_bar(P, [(P["seg"][k], bd[k]) for k in SEG_ORDER], size, bar_w, now, busy)
    t, mrow, b = battery(P, bar, bar_w, pcol, PREFIX, avail, now, busy,
                         tl=title, tr=cache_seg if cache_seg.w else None, bl=legend if legend.w else None,
                         hue=P["accent"])
    rows = [T("  ") + t + pad(ctx_text[0], stat_w), spin + T(" ") + mrow + pad(ctx_text[1], stat_w),
            T("  ") + b + pad(ctx_text[2], stat_w)]
    x = PREFIX + bar_w + 4 + stat_w
    for sm in shown:
        x += GAP
        t, mrow, b = battery(P, mini_bar(P, sm["pct"], SMALL_IN, sm["col"]), SMALL_IN, sm["col"],
                             x, avail, now, busy, tl=T(sm["label"], P["dim"]),
                             hue=P["seg"]["tools"] if sm["label"] == "5h" else P["seg"]["sys"])
        sw = max(tt.w for tt in sm["text"])
        for r_i, part in enumerate((t, mrow, b)):
            rows[r_i] = rows[r_i] + T(" " * GAP) + part + pad(sm["text"][r_i], sw)
        x += small_w(sm)

    line1 = fit(left1, [], avail)
    sys.stdout.write("\n".join([line1.ansi] + [r.ansi.rstrip() for r in rows]) + "\n")

    try:
        tmp = cpath + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, cpath)
    except Exception:
        pass


if __name__ == "__main__":
    main()
