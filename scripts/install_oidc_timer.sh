#!/bin/bash
# Write ~/.config/company-agent/oidc-sync.env with absolute paths for this machine.
# Preserves existing IdP configuration (issuer/audience/JWKS/secrets): only the three managed
# path keys are rewritten. Writes atomically and keeps a timestamped backup.
# Does not enable the timer; it prints the enable command.
set -eu
ROOT="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
RUNTIME="${POC_RUNTIME:-$ROOT/.runtime-lan}"
TOKEN_DIR="${POC_OIDC_TOKEN_DIR:-$HOME/.config/company-agent/oidc-tokens}"
CONF_DIR="$HOME/.config/company-agent"
ENV_FILE="$CONF_DIR/oidc-sync.env"
MANAGED='^(POC_ROOT|POC_RUNTIME|POC_OIDC_TOKEN_DIR)='
mkdir -p "$CONF_DIR" "$TOKEN_DIR"
tmp="$(mktemp "$CONF_DIR/.oidc-sync.env.XXXXXX")"
if [ -f "$ENV_FILE" ]; then
  cp -p "$ENV_FILE" "$ENV_FILE.bak-$(date -u +%Y%m%dT%H%M%SZ)"
  grep -vE "$MANAGED" "$ENV_FILE" > "$tmp" || true   # keep issuer/audience/JWKS and any secret
  action=updated
else
  : > "$tmp"
  action=installed
fi
{
  printf 'POC_ROOT=%s\n' "$ROOT"
  printf 'POC_RUNTIME=%s\n' "$RUNTIME"
  printf 'POC_OIDC_TOKEN_DIR=%s\n' "$TOKEN_DIR"
} >> "$tmp"
chmod 600 "$tmp"
mv "$tmp" "$ENV_FILE"
echo "$action $ENV_FILE"
echo "enable: systemctl --user enable --now company-agent-oidc-sync.timer"
