#!/usr/bin/env bash
# scripts/install_cron.sh — Install a local launchd job (macOS) or cron job (Linux)
# timed ~15 min after each spin class ends.
#
# Usage:
#   chmod +x scripts/install_cron.sh
#   ./scripts/install_cron.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PYTHON="$REPO_DIR/.venv/bin/python"
SYNC_SCRIPT="$REPO_DIR/src/sync.py"
LOG_FILE="$REPO_DIR/spin-sync.log"
ENV_FILE="$REPO_DIR/.env"

if [[ ! -f "$VENV_PYTHON" ]]; then
  echo "ERROR: virtualenv not found at $VENV_PYTHON"
  echo "Run:  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

if [[ "$(uname)" == "Darwin" ]]; then
  # ---------- macOS: launchd plist ----------
  PLIST_LABEL="com.spinsync.agent"
  PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

  cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${PLIST_LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>WRAPPER_PLACEHOLDER</string>
  </array>

  <!-- Fire ~15 min after each class ends (local system time, DST-aware). -->
  <!-- Mon 08:15, Tue 13:00, Wed 08:30, Sat 11:15, Sun 10:45 + 11:30      -->
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>15</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>13</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>6</integer><key>Hour</key><integer>11</integer><key>Minute</key><integer>15</integer></dict>
    <dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>10</integer><key>Minute</key><integer>45</integer></dict>
    <dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>11</integer><key>Minute</key><integer>30</integer></dict>
  </array>

  <key>StandardOutPath</key>
  <string>${LOG_FILE}</string>
  <key>StandardErrorPath</key>
  <string>${LOG_FILE}</string>

  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
PLIST

  # launchd can't source .env directly, so wrap the call
  WRAPPER="$REPO_DIR/scripts/_run_with_env.sh"
  cat > "$WRAPPER" <<WRAPPER
#!/usr/bin/env bash
set -a
# shellcheck source=/dev/null
source "${ENV_FILE}"
set +a
exec "${VENV_PYTHON}" "${SYNC_SCRIPT}" "\$@"
WRAPPER
  chmod +x "$WRAPPER"

  # Update plist to use wrapper
  /usr/libexec/PlistBuddy -c "Delete :ProgramArguments" "$PLIST_PATH" 2>/dev/null || true
  /usr/libexec/PlistBuddy -c "Add :ProgramArguments array" "$PLIST_PATH"
  /usr/libexec/PlistBuddy -c "Add :ProgramArguments:0 string $WRAPPER" "$PLIST_PATH"

  launchctl unload "$PLIST_PATH" 2>/dev/null || true
  launchctl load   "$PLIST_PATH"

  echo "✓  launchd job installed: $PLIST_LABEL"
  echo "   Fires after each class: Mon 08:15, Tue 13:00, Wed 08:30, Sat 11:15, Sun 10:45 + 11:30 (local time)"
  echo "   Log: $LOG_FILE"
  echo ""
  echo "   To uninstall:  launchctl unload $PLIST_PATH && rm $PLIST_PATH"

else
  # ---------- Linux: cron ----------
  # Class schedule with 15-min buffer after each class ends (local ET times).
  # One entry per slot — cron uses the system clock which tracks DST automatically,
  # so a single local-time entry covers both EDT and EST without duplication.
  #
  # Monday   08:15 ET (class ends 08:00)
  # Tuesday  13:00 ET (class ends 12:45)
  # Wednesday 08:30 ET (class ends 08:15)
  # Saturday  11:15 ET (class ends 11:00)
  # Sunday   10:45 ET (regular, ends 10:30) + 11:30 ET (90-min catch-up, ends 11:00)
  CRON_LINES=(
    "15 8 * * 1  . ${ENV_FILE} && ${VENV_PYTHON} ${SYNC_SCRIPT} >> ${LOG_FILE} 2>&1  # Mon class"
    "0  13 * * 2  . ${ENV_FILE} && ${VENV_PYTHON} ${SYNC_SCRIPT} >> ${LOG_FILE} 2>&1  # Tue class"
    "30 8 * * 3  . ${ENV_FILE} && ${VENV_PYTHON} ${SYNC_SCRIPT} >> ${LOG_FILE} 2>&1  # Wed class"
    "15 11 * * 6  . ${ENV_FILE} && ${VENV_PYTHON} ${SYNC_SCRIPT} >> ${LOG_FILE} 2>&1  # Sat class"
    "45 10 * * 0  . ${ENV_FILE} && ${VENV_PYTHON} ${SYNC_SCRIPT} >> ${LOG_FILE} 2>&1  # Sun regular"
    "30 11 * * 0  . ${ENV_FILE} && ${VENV_PYTHON} ${SYNC_SCRIPT} >> ${LOG_FILE} 2>&1  # Sun 90-min catch-up"
  )

  # Remove any existing spin-sync entries, then add the new ones
  EXISTING=$(crontab -l 2>/dev/null | grep -v "spin-sync\|$SYNC_SCRIPT")
  {
    echo "$EXISTING"
    for line in "${CRON_LINES[@]}"; do
      echo "$line"
    done
  } | crontab -

  echo "✓  Cron jobs installed (${#CRON_LINES[@]} entries)."
  echo "   Fires after each class: Mon 08:15, Tue 13:00, Wed 08:30, Sat 11:15, Sun 10:45 + 11:30 ET"
  echo "   Log: $LOG_FILE"
  echo ""
  echo "   To uninstall:  crontab -e  (delete the spin-sync lines)"
fi
