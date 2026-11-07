#!/bin/sh
# Install claude-statusline.
#   ./install.sh               link statusline.py into ~/.claude/statusline/ and install the ClaudeMark font
#   ./install.sh --configure   also add the statusLine entry to ~/.claude/settings.json (a backup is made first)
#   ./install.sh --no-font     skip the font (the logo falls back to ✻)
set -e
here=$(cd "$(dirname "$0")" && pwd)
dest="$HOME/.claude/statusline"
configure=0; font=1
for a in "$@"; do
  case "$a" in --configure) configure=1 ;; --no-font) font=0 ;; esac
done

command -v python3 >/dev/null || { echo "claude-statusline needs python3"; exit 1; }

mkdir -p "$dest"
if [ -f "$dest/statusline.py" ] && [ ! -L "$dest/statusline.py" ]; then
  cp "$dest/statusline.py" "$dest/statusline.py.bak"
  echo "backup  $dest/statusline.py.bak"
fi
ln -sf "$here/statusline.py" "$dest/statusline.py"
echo "linked  $dest/statusline.py -> $here/statusline.py"

if [ "$font" = 1 ]; then
  case "$(uname)" in Darwin) fonts="$HOME/Library/Fonts" ;; *) fonts="$HOME/.local/share/fonts" ;; esac
  mkdir -p "$fonts"; cp "$here/fonts/ClaudeMark.ttf" "$fonts/"
  command -v fc-cache >/dev/null && fc-cache -f "$fonts" >/dev/null 2>&1 || true
  echo "font    $fonts/ClaudeMark.ttf  (restart the terminal if the logo shows as a box)"
fi

settings="$HOME/.claude/settings.json"
if [ "$configure" = 1 ]; then
  python3 - "$settings" <<'EOF'
import json, os, shutil, sys
p = sys.argv[1]
data = {}
if os.path.exists(p):
    shutil.copy(p, p + ".bak")
    print("backup  " + p + ".bak")
    with open(p) as f:
        data = json.load(f)
data["statusLine"] = {"type": "command", "command": "python3 -I -S ~/.claude/statusline/statusline.py",
                      "padding": 0, "refreshInterval": 1}
with open(p, "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
print("config  statusLine added to " + p)
EOF
else
  cat <<'EOF'

Add this to ~/.claude/settings.json (or rerun with --configure):

  "statusLine": {
    "type": "command",
    "command": "python3 -I -S ~/.claude/statusline/statusline.py",
    "padding": 0,
    "refreshInterval": 1
  }
EOF
fi
echo "done    restart Claude Code (or run /restart) to see it"
