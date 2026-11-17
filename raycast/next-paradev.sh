#!/bin/sh

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Next Paradev
# @raycast.mode silent

# Optional parameters:
# @raycast.icon →
# @raycast.packageName attention-radar
# @raycast.description Open the session that the radar's Next points at.

# Raycast runs a copy of this script from its own folder, so follow the link back to the repo.
SCRIPT="$0"
while [ -L "$SCRIPT" ]; do
  LINK="$(readlink "$SCRIPT")"
  case "$LINK" in /*) SCRIPT="$LINK" ;; *) SCRIPT="$(dirname "$SCRIPT")/$LINK" ;; esac
done
exec /usr/bin/python3 -B "$(cd "$(dirname "$SCRIPT")/.." && pwd)/attention_radar.py" next
