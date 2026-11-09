#!/usr/bin/env python3
"""Build a self-contained fake environment and render the status line from it.

    python3 scripts/demo.py /tmp/sl-demo                 # build (dark theme), print one render
    python3 scripts/demo.py /tmp/sl-demo --theme light
    python3 scripts/demo.py /tmp/sl-demo --busy          # render the "Claude is working" state

The fake environment lives entirely under <dir>/home and is passed to statusline.py via $HOME,
so nothing in your real ~/.claude is read or written. Everything in it is fictional.
"""
import json, os, shutil, subprocess, sys, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SID = "5e55a1d0-0000-4000-8000-00000000c0de"


def jl(f, obj):
    f.write(json.dumps(obj, separators=(",", ":")) + "\n")


def filler(tokens):
    """ASCII text that the status line's estimator counts as roughly `tokens` tokens."""
    return ("lorem ipsum dolor sit amet " * (int(tokens * 3.7) // 27 + 1))[: int(tokens * 3.7)]


def build(root, theme):
    home = os.path.join(root, "home")
    shutil.rmtree(home, ignore_errors=True)
    cwd = os.path.join(home, "code", "web-dashboard")
    os.makedirs(cwd)
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    with open(os.path.join(home, ".claude", "settings.json"), "w") as f:
        json.dump({"theme": theme}, f)
    fonts = os.path.join(home, "Library", "Fonts")
    os.makedirs(fonts)
    shutil.copy(os.path.join(REPO, "fonts", "ClaudeMark.ttf"), fonts)

    # a git repo on a feature branch with uncommitted work
    g = lambda *a: subprocess.run(["git", "-C", cwd, *a], capture_output=True, check=True)
    g("init", "-q", "-b", "feat/oauth")
    with open(os.path.join(cwd, "README.md"), "w") as f:
        f.write("# web-dashboard\n")
    g("add", "-A")
    g("-c", "user.name=demo", "-c", "user.email=demo@example.com", "commit", "-q", "-m", "init")
    with open(os.path.join(cwd, "pkce.ts"), "w") as f:
        f.write("export {}\n")

    # project memory
    with open(os.path.join(cwd, "CLAUDE.md"), "w") as f:
        f.write(filler(1400))

    # transcript: request shape + listings + cumulative usage
    pdir = os.path.join(home, ".claude", "projects", cwd.replace("/", "-"))
    os.makedirs(pdir)
    transcript = os.path.join(pdir, SID + ".jsonl")
    with open(transcript, "w") as f:
        jl(f, {"type": "api-request-shape", "shape": {
            "system": [{"type": "text", "text": filler(3100)}],
            "tools": [{"name": "Bash", "description": filler(9000)}, {"name": "Edit", "description": filler(7000)},
                      {"name": "Read", "description": filler(5200)},
                      {"name": "mcp__github__search", "description": filler(3800)}]}})
        jl(f, {"type": "attachment", "attachment": {"type": "skill_listing", "content": filler(5400)}})
        jl(f, {"type": "attachment", "attachment": {"type": "mcp_instructions_delta", "content": filler(900)}})
        jl(f, {"type": "cost-state", "totalCostUSD": 4.12, "modelUsage": {"claude-opus-5-5": {
            "inputTokens": 61_000, "outputTokens": 214_000, "cacheReadInputTokens": 17_900_000,
            "cacheCreationInputTokens": 245_000}}})

    # seed the per-session cache so the cost sparkline has history
    cache = os.path.join(home, ".claude", "statusline", "cache")
    os.makedirs(cache)
    with open(os.path.join(cache, SID + ".json"), "w") as f:
        json.dump({"cost_hist": [0.08, 0.21, 0.12, 0.46, 0.27, 1.18, 0.34, 0.15], "last_cost": 4.12, "grow_at": 0}, f)

    now = time.time()
    data = {
        "session_id": SID, "transcript_path": transcript, "cwd": cwd,
        "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
        "effort": {"level": "high"}, "fast_mode": False,
        "workspace": {"current_dir": cwd, "project_dir": cwd},
        "cost": {"total_cost_usd": 4.12, "total_duration_ms": 8_040_000,
                 "total_lines_added": 348, "total_lines_removed": 97},
        "context_window": {"context_window_size": 200_000, "used_percentage": 59,
                           "current_usage": {"input_tokens": 9, "cache_creation_input_tokens": 2_400,
                                             "cache_read_input_tokens": 115_800, "output_tokens": 640}},
        "prompt_cache": {"warm": True, "expires_at": now + 41 * 60},
        "rate_limits": {"five_hour": {"used_percentage": 38, "resets_at": now + 2 * 3600 + 31 * 60},
                        "seven_day": {"used_percentage": 64, "resets_at": now + 3 * 86400 + 5 * 3600}},
    }
    with open(os.path.join(root, "input.json"), "w") as f:
        json.dump(data, f)
    return home, transcript


def render(root, busy=False, columns=150):
    home = os.path.join(root, "home")
    with open(os.path.join(root, "input.json")) as f:
        transcript = json.load(f)["transcript_path"]
    t = time.time() if busy else time.time() - 600
    os.utime(transcript, (t, t))
    env = dict(os.environ, HOME=home, COLUMNS=str(columns), CLAUDE_PID="1")
    with open(os.path.join(root, "input.json")) as f:
        return subprocess.run([sys.executable, "-I", "-S", os.path.join(REPO, "statusline.py")], stdin=f,
                              env=env, capture_output=True, text=True).stdout


def main():
    a = sys.argv[1:]
    root = os.path.abspath(a[0] if a and not a[0].startswith("-") else "/tmp/sl-demo")
    theme = a[a.index("--theme") + 1] if "--theme" in a else "dark"
    os.makedirs(root, exist_ok=True)
    build(root, theme)
    sys.stdout.write(render(root, busy="--busy" in a))


if __name__ == "__main__":
    main()
