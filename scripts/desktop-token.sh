#!/usr/bin/env bash
# Shared Local API session-token helpers for the desktop scripts.
#
# The token is always handed to the backend through GUILDBOTICS_APP_API_TOKEN,
# never through argv, and is minted per run instead of being a constant checked
# into the repository. The browser-preview dev scripts run the backend and the
# Vite server in separate terminals, so they exchange the generated token
# through a user-private file under the OS temp directory.

guildbotics_random_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  elif command -v uuidgen >/dev/null 2>&1; then
    uuidgen
  else
    python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
  fi
}

guildbotics_dev_token_dir() {
  local base="${TMPDIR:-/tmp}"
  printf '%s/guildbotics-dev' "${base%/}"
}

guildbotics_dev_token_file() {
  printf '%s/%s.token' "$(guildbotics_dev_token_dir)" "$1"
}
