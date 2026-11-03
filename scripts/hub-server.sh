#!/bin/sh
# Run the GuildBotics server of this checkout as a per-user macOS service.
set -eu
umask 077

usage() {
    cat <<'EOF'
Usage: hub-server.sh install | update | restart | status | uninstall

install    Install the CLI and the agent skill, then run `guildbotics server
           start` of this checkout as a LaunchAgent: now, at login, and again
           after a crash. The service keeps the PATH (and
           GUILDBOTICS_SERVER_PORT) of the shell that installs it.
update     After updating the checkout: install the dependencies, build the
           screens, install the CLI and the skill again, and restart.
restart    Stop the server after its in-flight work, then start it again.
status     Show the service and its last exit.
uninstall  Stop the server and remove the LaunchAgent.

The server writes its output to ~/Library/Logs/guildbotics.log. `guildbotics
server stop` stops it until the next login or restart. It starts the services
(patrol, chat events) only when the Service screen's setting says so; the
Service screen and `guildbotics service start|stop` start and stop them.
EOF
}
fail() { printf '%s\n' "$*" >&2; exit 1; }
action=${1:-help}
[ "$#" -le 1 ] || fail "No options are accepted."
case "$action" in
    -h|--help|help) usage; exit 0 ;;
    install|update|restart|status|uninstall) ;;
    *) usage >&2; exit 1 ;;
esac
[ "$(uname -s)" = Darwin ] || fail "Supported system: macOS."
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
python=$repo/.venv/bin/python
cli=$repo/.venv/bin/guildbotics
label=local.guildbotics
domain=gui/$(id -u)
target=$domain/$label
file=$HOME/Library/LaunchAgents/$label.plist
log=$HOME/Library/Logs/guildbotics.log

loaded() { launchctl print "$target" >/dev/null 2>&1; }
running() {
    "$python" -c 'import sys; from guildbotics.drivers.app_api_lock import inspect_backend_lock; sys.exit(0 if inspect_backend_lock()[0] else 1)'
}
# Ends a running server after its in-flight work, however long it takes; the
# CLI reaches it on the port it recorded in this machine's lock.
stop_server() {
    [ -x "$cli" ] || return 0
    "$cli" server stop
}
install_assets() { "$python" "$repo/scripts/install_cli_assets.py"; }
interpreter() { "$python" -c 'import os, sys; print(os.path.realpath(sys.executable))'; }

case "$action" in
    status)
        launchctl print "$target"
        exit $?
        ;;
    uninstall)
        stop_server
        if loaded; then launchctl bootout "$target"; fi
        rm -f "$file"
        exit 0
        ;;
    restart)
        loaded || fail "The service is not installed. Run: $0 install"
        stop_server
        # Its job may still be ending after the server: start it regardless.
        launchctl kickstart -k "$target"
        exit 0
        ;;
    update)
        loaded || fail "The service is not installed. Run: $0 install"
        before=$(interpreter)
        (cd "$repo" && uv sync --locked --extra test --extra dev)
        (cd "$repo" && npm ci && npm run build --workspace desktop)
        install_assets
        stop_server
        if [ "$(interpreter)" != "$before" ]; then
            # The keychain asks again for each of its items, which a service
            # without a window cannot answer.
            cat <<EOF
Python changed. Run the server once in a terminal, answer each keychain prompt
with "Always Allow", stop it with Ctrl-C, then start the service:
  $cli server start
  $0 restart
EOF
            exit 0
        fi
        launchctl kickstart -k "$target"
        exit 0
        ;;
esac

# install
[ -x "$cli" ] || fail "$cli is missing. Run uv sync in $repo first."
# The property list cannot carry line breaks safely.
case "$repo$HOME$PATH${GUILDBOTICS_SERVER_PORT-}" in *'
'*) fail "Paths must not contain line breaks." ;; esac
# Never stops a server: one may be this service's or a terminal's.
running && fail "A GuildBotics server is running. Stop it with: $cli server stop"
if loaded; then launchctl bootout "$target"; fi
install_assets
mkdir -p "$(dirname "$file")" "$(dirname "$log")"
temp=$(mktemp "$file.XXXXXX")
trap 'rm -f "$temp"' 0 HUP INT TERM
xml() { printf '%s' "$1" | sed 's/\&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g; s/"/\&quot;/g; s/'"'"'/\&apos;/g'; }
entry() { printf '<key>%s</key><string>%s</string>\n' "$1" "$(xml "$2")"; }
{
    printf '%s\n' '<?xml version="1.0" encoding="UTF-8"?>' '<plist version="1.0"><dict>'
    entry Label "$label"
    printf '<key>ProgramArguments</key><array><string>%s</string><string>server</string><string>start</string></array>\n' "$(xml "$cli")"
    printf '%s\n' '<key>EnvironmentVariables</key><dict>'
    entry PATH "$PATH"
    if [ -n "${GUILDBOTICS_SERVER_PORT-}" ]; then
        entry GUILDBOTICS_SERVER_PORT "$GUILDBOTICS_SERVER_PORT"
    fi
    printf '%s\n' '</dict>'
    # `guildbotics server stop` exits 0 and stays stopped; a crash starts again.
    # SIGTERM cancels in-flight work; launchd kills the server after
    # KILL_AFTER_SECONDS (guildbotics/drivers/app_api_lock.py).
    printf '%s\n' '<key>RunAtLoad</key><true/>' \
        '<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>' \
        '<key>ThrottleInterval</key><integer>15</integer>' \
        '<key>ExitTimeOut</key><integer>90</integer>'
    entry StandardOutPath "$log"
    entry StandardErrorPath "$log"
    printf '%s\n' '</dict></plist>'
} >"$temp"
mv -f "$temp" "$file"
launchctl enable "$target"
launchctl bootstrap "$domain" "$file"
printf 'Installed %s. Its output goes to %s.\n' "$label" "$log"
