#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNTIME_DIR="$HOME/.local/share/codex-agent-monitor"
PLIST_PATH="$HOME/Library/LaunchAgents/com.stc.agent-monitor.plist"
LOG_DIR="$HOME/Library/Logs/AgentMonitor"
USER_ID="$(id -u)"

mkdir -p "$(dirname "$PLIST_PATH")" "$LOG_DIR" "$RUNTIME_DIR"

python3 - "$APP_DIR" "$RUNTIME_DIR" "$PLIST_PATH" "$LOG_DIR" <<'PY'
import plistlib
import shutil
import sys
from pathlib import Path

app_dir = Path(sys.argv[1])
runtime_dir = Path(sys.argv[2])
plist_path = Path(sys.argv[3])
log_dir = Path(sys.argv[4])

for name in ("server.py", "index.html"):
    shutil.copy2(app_dir / name, runtime_dir / name)

assets_src = app_dir / "assets"
assets_dst = runtime_dir / "assets"
if assets_src.exists():
    shutil.copytree(assets_src, assets_dst, dirs_exist_ok=True)

data = {
    "Label": "com.stc.agent-monitor",
    "ProgramArguments": ["/usr/bin/python3", str(runtime_dir / "server.py")],
    "WorkingDirectory": str(runtime_dir),
    "EnvironmentVariables": {
        "HOST": "127.0.0.1",
        "PORT": "8799",
    },
    "RunAtLoad": True,
    "KeepAlive": True,
    "StandardOutPath": str(log_dir / "agent-monitor.out.log"),
    "StandardErrorPath": str(log_dir / "agent-monitor.err.log"),
}

with plist_path.open("wb") as f:
    plistlib.dump(data, f)
PY

launchctl bootout "gui/${USER_ID}" "$PLIST_PATH" 2>/dev/null || true
launchctl bootstrap "gui/${USER_ID}" "$PLIST_PATH"
launchctl enable "gui/${USER_ID}/com.stc.agent-monitor"
launchctl kickstart -k "gui/${USER_ID}/com.stc.agent-monitor"

echo "Agent Monitor autostart is enabled."
echo "Open: http://localhost:8799/"
