#!/bin/sh
# Build and run the integrated Hub as one macOS user agent.
set -eu
umask 077

usage() {
    cat <<'EOF'
Usage: scripts/hub-server.sh install | update | restart | status | recover | uninstall

install    Build HEAD, install the CLI and skill, and start the LaunchAgent.
           Set BRANVIL_DATA_DIR to the absolute path of the initialized data.
update     Build HEAD, back up Branvil, rehearse the upgrade, and switch builds.
restart    Let in-flight work finish, then start the current build again.
status     Show the LaunchAgent and its last exit.
recover    Resume an interrupted update or return to the previous build.
uninstall  Let in-flight work finish and remove the LaunchAgent.

Run install and update from the Hub owner's macOS desktop session. See
docs/hub-server.md for Keychain prompts, backup locations, and recovery.
EOF
}
fail() { printf '%s\n' "$*" >&2; exit 1; }
action=${1:-help}
[ "$#" -le 1 ] || fail "No options are accepted."
case "$action" in
    -h|--help|help) usage; exit 0 ;;
    install|update|restart|status|recover|uninstall) ;;
    *) usage >&2; exit 2 ;;
esac
[ "$(uname -s)" = Darwin ] || fail "Supported system: macOS."

repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
label=local.guildbotics
domain=gui/$(id -u)
target=$domain/$label
plist=$HOME/Library/LaunchAgents/$label.plist
log=$HOME/Library/Logs/guildbotics.log
hub="$HOME/Library/Application Support/GuildBotics/Hub"
builds=$hub/builds
current=$builds/current
pending=$builds/update-from
backups=$hub/backups

if [ "$action" = status ]; then exec launchctl print "$target"; fi

# Use one lock for the build pointer, backup rotation and LaunchAgent changes.
mkdir -p "$builds"
lock=$hub/operation
mkdir "$lock" 2>/dev/null || fail "Another Hub operation holds $lock. See docs/hub-server.md."
stage=setup
finish() {
    if [ "$1" -ne 0 ]; then
        active=$(readlink "$current" 2>/dev/null || true)
        active=${active%/*}
        message="$(date -u +%FT%TZ): Hub $action failed during $stage (exit $1, active revision: ${active##*/}, requested revision: ${revision:-unknown})."
        printf '%s\n' "$message" | tee "$hub/last-failure" >&2 || true
    fi
    rm -rf "$lock"
}
trap 'finish "$?"' 0
trap 'exit 130' INT
trap 'exit 143' HUP TERM

loaded() { launchctl print "$target" >/dev/null 2>&1; }
job_pid() { launchctl print "$target" 2>/dev/null | awk '$1 == "pid" && $2 == "=" { print $3; exit }'; }
setting() { plutil -extract "$1" raw -o - "$plist" 2>/dev/null; }
installed() { [ "$(setting ProgramArguments.0)" = "$current" ]; }
require_agent() {
    installed && [ -x "$current" ] ||
        fail "The integrated LaunchAgent is not installed. Run: $0 install"
}
assert_stopped() {
    holder=$("$1" server hub-probe) || return
    [ -z "$holder" ] || fail "A GuildBotics server is running (PID $holder). Stop it before continuing."
}
stop_agent() {
    holder=$("$current" server hub-probe) || return
    if [ -n "$holder" ]; then
        [ "$holder" = "$(job_pid)" ] ||
            fail "A GuildBotics server outside this LaunchAgent is running (PID $holder)."
        "$current" server stop || return
    fi
    if loaded; then launchctl bootout "$target"; fi
}
agent_ready() {
    holder=$("$current" server hub-probe --ready) &&
        [ -n "$holder" ] && [ "$holder" = "$(job_pid)" ]
}
bootstrap() {
    write_plist && launchctl enable "$target" && launchctl bootstrap "$domain" "$plist"
}
wait_ready() {
    # The proof is served after both servers are ready. Match the lock holder
    # to this job, and allow the owner time to answer macOS access dialogs.
    deadline=$(($(date +%s) + 1200))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if agent_ready; then return 0; fi
        job=$(launchctl print "$target") || return 1
        if printf '%s\n' "$job" | awk '
            $1 == "pid" && $2 == "=" { running = 1 }
            $1 == "last" && $2 == "exit" && $3 == "code" && $5 ~ /^[0-9]+$/ { exited = 1 }
            END { exit !(exited && !running) }
        '; then
            break
        fi
        sleep 1
    done
    return 1
}
start_agent() {
    if bootstrap && wait_ready || agent_ready; then return 0; fi
    printf 'The Hub did not start. See %s.\n' "$log" >&2
    return 1
}
point_current() {
    ln -sf "$1" "$lock/current" && mv -f "$lock/current" "$current"
}
admin_upgrade_and_check() {
    code=0
    "$1" branvil admin upgrade --data-dir "$2" || code=$?
    case "$code" in
        0|3) "$1" branvil admin check --data-dir "$2" ;;
        *) return "$code" ;;
    esac
}

build_head() {
    stage=build
    revision=$(git -C "$repo" rev-parse --verify HEAD)
    source=$builds/$revision
    candidate=$source/guildbotics-bin
    if [ -x "$candidate" ]; then return; fi
    # The binary uses templates and the Desktop build in its source tree.
    # Keep this exact source at its build path even as the Hub checkout advances.
    rm -rf "$source"
    mkdir "$source"
    git -C "$repo" archive --format=tar --output="$lock/source.tar" "$revision"
    tar -xf "$lock/source.tar" -C "$source"
    rm -f "$lock/source.tar"
    (
        cd "$source"
        npm ci
        npm run build --workspace desktop --workspace crates/branvil/web
        cargo build --release --locked -p guildbotics --target-dir "$hub/target"
    )
    cp "$hub/target/release/guildbotics" "$source/guildbotics-bin.new"
    mv -f "$source/guildbotics-bin.new" "$candidate"
}
backup_and_rehearse() {
    stage=backup
    mkdir -p "$backups"
    backup=$(mktemp -d "$backups/branvil-update-$(date -u +%Y%m%dT%H%M%SZ).XXXXXX")
    # Use the installed instance's owner credential and address.
    unset BRANVIL_TOKEN BRANVIL_URL
    "$current" branvil backup --data-dir "$BRANVIL_DATA_DIR" --to "$backup"
    printf 'Backup before update: %s\n' "$backup"

    # Keep the latest three completed backups. Incomplete backups have no manifest.
    set --
    for saved in "$backups"/branvil-update-*; do
        if [ "$saved" != "$backup" ] && [ -d "$saved" ] && [ ! -L "$saved" ] && [ -f "$saved/manifest.json" ]; then
            set -- "$@" "$saved"
        fi
    done
    while [ "$#" -gt 2 ]; do rm -rf "$1"; shift; done

    stage=rehearsal
    rehearsal=$lock/rehearsal
    "$current" branvil restore --from "$backup" --data-dir "$rehearsal"
    admin_upgrade_and_check "$candidate" "$rehearsal"
}

xml() { printf '%s' "$1" | sed 's/\&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'; }
entry() { printf '<key>%s</key><string>%s</string>\n' "$1" "$(xml "$2")"; }
write_plist() {
    exit_timeout=$("$current" server hub-probe --exit-timeout) || return
    mkdir -p "$(dirname "$plist")" "$(dirname "$log")" || return
    {
        printf '%s\n' '<?xml version="1.0" encoding="UTF-8"?>' '<plist version="1.0"><dict>'
        entry Label "$label"
        printf '<key>ProgramArguments</key><array><string>%s</string><string>server</string><string>start</string></array>\n' "$(xml "$current")"
        printf '%s\n' '<key>EnvironmentVariables</key><dict>'
        entry PATH "$agent_path"
        entry BRANVIL_DATA_DIR "$BRANVIL_DATA_DIR"
        entry GUILDBOTICS_HOME "$GUILDBOTICS_HOME"
        if [ -n "${GUILDBOTICS_SERVER_PORT:-}" ]; then entry GUILDBOTICS_SERVER_PORT "$GUILDBOTICS_SERVER_PORT"; fi
        printf '%s\n' '</dict>' \
            '<key>RunAtLoad</key><true/>' \
            '<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>' \
            '<key>ThrottleInterval</key><integer>15</integer>' \
            "<key>ExitTimeOut</key><integer>$exit_timeout</integer>"
        entry StandardOutPath "$log"
        entry StandardErrorPath "$log"
        printf '%s\n' '</dict></plist>'
    } >"$lock/agent.plist" || return
    plutil -lint "$lock/agent.plist" >/dev/null || return
    mv -f "$lock/agent.plist" "$plist"
}
read_plist() {
    # Build with the invoking shell's tools, but run commands against the same
    # machine root, Branvil data directory and port as the installed agent.
    agent_path=$(setting EnvironmentVariables.PATH)
    BRANVIL_DATA_DIR=$(setting EnvironmentVariables.BRANVIL_DATA_DIR)
    GUILDBOTICS_HOME=$(setting EnvironmentVariables.GUILDBOTICS_HOME)
    GUILDBOTICS_SERVER_PORT=$(setting EnvironmentVariables.GUILDBOTICS_SERVER_PORT || true)
    export BRANVIL_DATA_DIR GUILDBOTICS_HOME GUILDBOTICS_SERVER_PORT
}
install_assets() {
    stage="installing the CLI and skill"
    if "$current" install; then return 0; fi
    printf 'The Hub is running. Retry CLI and skill installation with: "%s" install\n' "$current" >&2
    return 1
}
complete_activation() {
    rm -f "$pending" && install_assets
}
rollback() {
    previous=$(cat "$pending") || return
    # The start failed before its authenticated readiness was observed.
    if loaded; then launchctl bootout "$target" || return; fi
    assert_stopped "$previous" || return
    point_current "$previous" || return
    # The previous build may refuse the upgraded schema. Leave the data in
    # place and stay stopped for the operator to decide how to restore it.
    if ! "$previous" branvil admin check --data-dir "$BRANVIL_DATA_DIR"; then
        printf 'The previous build cannot check the live data. See docs/hub-server.md before restarting.\n' >&2
        return 1
    fi
    start_agent && rm -f "$pending"
}

if [ "$action" != install ]; then
    require_agent
    read_plist
fi
if [ -e "$pending" ] && [ "$action" != recover ]; then
    fail "An update has not finished. Run: $0 recover"
fi
case "$action" in
    install)
        agent_path=$PATH
        : "${GUILDBOTICS_HOME:=$HOME/.guildbotics}"
        case "${BRANVIL_DATA_DIR:-}" in
            /*) ;;
            *) fail "Set BRANVIL_DATA_DIR to the absolute path of the initialized Branvil data." ;;
        esac
        case "$GUILDBOTICS_HOME" in /*) ;; *) fail "GUILDBOTICS_HOME must be an absolute path." ;; esac
        case "$HOME$PATH$GUILDBOTICS_HOME$BRANVIL_DATA_DIR${GUILDBOTICS_SERVER_PORT:-}" in
            *'
'*) fail "Paths and environment settings must not contain line breaks." ;;
        esac
        export BRANVIL_DATA_DIR GUILDBOTICS_HOME
        if [ -e "$plist" ] || loaded; then
            installed || fail "Remove the existing $label LaunchAgent before installing the integrated server."
        fi
        build_head
        if [ -L "$current" ] && [ "$(readlink "$current")" != "$candidate" ] && installed; then
            fail "A different build is installed. Run: $0 update"
        fi
        stage="checking the stopped server"
        assert_stopped "$candidate"
        "$candidate" branvil admin check --data-dir "$BRANVIL_DATA_DIR"
        if loaded; then launchctl bootout "$target"; fi
        stage="installing the agent"
        point_current "$candidate"
        printf 'Allow Keychain access and Documents folder access if macOS asks. See docs/hub-server.md for details.\n'
        if ! start_agent; then
            if loaded; then launchctl bootout "$target"; fi
            exit 1
        fi
        install_assets
        printf 'Installed %s. Its output goes to %s.\n' "$label" "$log"
        ;;
    update)
        stage="checking the running server"
        agent_ready ||
            fail "Start the installed LaunchAgent before updating. Run: $0 restart"
        build_head
        previous=$(readlink "$current")
        if [ "$candidate" = "$previous" ]; then
            printf 'The Hub already uses %s.\n' "$revision"
            install_assets
            exit 0
        fi
        backup_and_rehearse
        stage="stopping the previous build"
        stop_agent
        assert_stopped "$previous"
        printf '%s\n' "$previous" >"$lock/update-from"
        mv -f "$lock/update-from" "$pending"
        stage="activating the new build"
        printf 'Allow Keychain access and Documents folder access if macOS asks. See docs/hub-server.md for details.\n'
        if admin_upgrade_and_check "$candidate" "$BRANVIL_DATA_DIR" &&
            point_current "$candidate" && start_agent; then
            complete_activation
            printf 'Hub updated to %s.\n' "$revision"
        else
            if rollback; then printf 'The previous build is running again.\n' >&2; fi
            exit 1
        fi
        ;;
    restart)
        stage=restart
        stop_agent
        assert_stopped "$current"
        start_agent
        ;;
    recover)
        stage=recovery
        [ -f "$pending" ] || fail "There is no interrupted update to recover."
        if agent_ready; then
            complete_activation
        else
            rollback
        fi
        ;;
    uninstall)
        stage=uninstall
        stop_agent
        rm -f "$plist"
        ;;
esac
