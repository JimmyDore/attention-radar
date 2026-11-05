#!/bin/sh
# Link the attention-radar plugin into SwiftBar's plugin folder, then check what it can see.
set -e

REPO="$(cd "$(dirname "$0")" && pwd)"
PLUGIN="$REPO/swiftbar/attention-radar.10s.py"

if [ "$(uname)" != "Darwin" ]; then
  echo "attention-radar needs macOS (it runs in SwiftBar)." >&2
  exit 1
fi

if [ ! -d /Applications/SwiftBar.app ] && [ ! -d "$HOME/Applications/SwiftBar.app" ]; then
  echo "SwiftBar is not installed. Run: brew install --cask swiftbar" >&2
  exit 1
fi

DIR="$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null || true)"
if [ -z "$DIR" ]; then
  DIR="$HOME/Library/Application Support/SwiftBar/Plugins"
  mkdir -p "$DIR"
  defaults write com.ameba.SwiftBar PluginDirectory "$DIR"
  echo "SwiftBar had no plugin folder: set it to $DIR"
fi

chmod +x "$PLUGIN"
TARGET="$DIR/attention-radar.10s.py"
if [ "$(cd "$DIR" && pwd -P)" != "$(cd "$REPO/swiftbar" && pwd -P)" ]; then
  if [ -e "$TARGET" ] && [ ! -L "$TARGET" ]; then
    echo "$TARGET exists and is not a link: remove it first." >&2
    exit 1
  fi
  ln -sf "$PLUGIN" "$DIR/attention-radar.10s.py"
  echo "Linked the plugin into $DIR"
fi

echo
/usr/bin/python3 -B "$REPO/attention_radar.py" doctor || {
  echo
  echo "The radar can't see your sessions yet. Is Paradev running, with Claude Code in a terminal?"
}

open -g "swiftbar://refreshallplugins" 2>/dev/null || open -a SwiftBar
