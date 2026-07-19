#!/usr/bin/env bash
# Store your ntfy access token so e-mail forwarding works.
#
# The token is read from a hidden prompt, so it never appears in your shell
# history or on screen. It is written to the INSTALLED launchd plist (outside
# this repo) and, optionally, to the GitHub secret used by the cloud runs.
#
#   ./scripts/set-ntfy-token.sh
set -euo pipefail

PLIST="$HOME/Library/LaunchAgents/com.visamonitor.f1.plist"
LABEL="com.visamonitor.f1"
UID_NUM="$(id -u)"

[ -f "$PLIST" ] || { echo "No installed plist at $PLIST -- install the launchd agent first."; exit 1; }

printf 'Paste your ntfy access token (input hidden, then press Enter): '
read -rs TOKEN
echo
[ -n "$TOKEN" ] || { echo "No token entered; nothing changed."; exit 1; }

# 1. local watcher
/usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:VISA_NTFY_TOKEN $TOKEN" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:VISA_NTFY_TOKEN string $TOKEN" "$PLIST"
chmod 600 "$PLIST"
echo "✓ token stored in $PLIST (owner-only)"

# 2. restart so it takes effect
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
for _ in $(seq 1 20); do
  launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1 || break
  sleep 0.5
done
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
echo "✓ watcher restarted"

# 3. cloud runs (optional)
if command -v gh >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  read -rp "Also store it as the GitHub secret VISA_NTFY_TOKEN? [y/N] " ans
  if [[ "$ans" =~ ^[Yy]$ ]]; then
    printf '%s' "$TOKEN" | gh secret set VISA_NTFY_TOKEN
    echo "✓ GitHub secret set"
  fi
fi

echo "Done."
