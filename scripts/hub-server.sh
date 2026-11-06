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
recover    Return to the previous build after an interrupted update.
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
    rm -rf "$lock"
    if [ "$1" -ne 0 ]; then
        message="Hub $action failed during $stage (exit $1)."
        printf '%s\n' "$message" | tee "$hub/last-failure" >&2
    fi
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
bootstrap() {
    launchctl enable "$target" && launchctl bootstrap "$domain" "$plist"
}
wait_ready() {
    # The proof is served after both servers are ready. Match the lock holder
    # to this job, and allow the owner time to answer the Keychain dialogs.
    deadline=$(($(date +%s) + 1200))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        pid=$(job_pid)
        if [ -n "$pid" ] && [ "$("$current" server hub-probe --ready)" = "$pid" ]; then return 0; fi
        job=$(launchctl print "$target") || return 1
        if printf '%s\n' "$job" | awk '$1 == "last" && $2 == "exit" && $3 == "code" && $5 ~ /^[0-9]+$/ { found = 1 } END { exit !found }'; then
            break
        fi
        sleep 1
    done
    printf 'The Hub did not start. See %s.\n' "$log" >&2
    return 1
}
start_agent() { bootstrap && wait_ready; }
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
        npm run build --workspaces
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
    mkdir -p "$(dirname "$plist")" "$(dirname "$log")"
    {
        printf '%s\n' '<?xml version="1.0" encoding="UTF-8"?>' '<plist version="1.0"><dict>'
        entry Label "$label"
        printf '<key>ProgramArguments</key><array><string>%s</string><string>server</string><string>start</string></array>\n' "$(xml "$current")"
        printf '%s\n' '<key>EnvironmentVariables</key><dict>'
        entry PATH "$PATH"
        entry BRANVIL_DATA_DIR "$BRANVIL_DATA_DIR"
        entry GUILDBOTICS_HOME "$GUILDBOTICS_HOME"
        if [ -n "${GUILDBOTICS_SERVER_PORT:-}" ]; then entry GUILDBOTICS_SERVER_PORT "$GUILDBOTICS_SERVER_PORT"; fi
        printf '%s\n' '</dict>' \
            '<key>RunAtLoad</key><true/>' \
            '<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>' \
            '<key>ThrottleInterval</key><integer>15</integer>' \
            '<key>ExitTimeOut</key><integer>90</integer>'
        entry StandardOutPath "$log"
        entry StandardErrorPath "$log"
        printf '%s\n' '</dict></plist>'
    } >"$lock/agent.plist"
    plutil -lint "$lock/agent.plist" >/dev/null
    mv -f "$lock/agent.plist" "$plist"
}
read_plist() {
    # Build with the invoking shell's tools, but run commands against the same
    # machine root, Branvil data directory and port as the installed agent.
    BRANVIL_DATA_DIR=$(setting EnvironmentVariables.BRANVIL_DATA_DIR)
    GUILDBOTICS_HOME=$(setting EnvironmentVariables.GUILDBOTICS_HOME)
    GUILDBOTICS_SERVER_PORT=$(setting EnvironmentVariables.GUILDBOTICS_SERVER_PORT || true)
    export BRANVIL_DATA_DIR GUILDBOTICS_HOME GUILDBOTICS_SERVER_PORT
}
recover() {
    previous=$(cat "$pending") || return
    if loaded; then
        # Prefer a normal shutdown if an interrupted update left a ready server.
        # A failed start can hang before the shutdown API is available.
        if holder=$("$current" server hub-probe --ready) && [ "$holder" = "$(job_pid)" ]; then
            stop_agent || return
        else
            launchctl bootout "$target" || return
        fi
    fi
    assert_stopped "$previous" || return
    point_current "$previous" || return
    # The previous build may refuse the upgraded schema. Leave the data in
    # place and stay stopped for the operator to decide how to restore it.
    if ! "$previous" branvil admin check --data-dir "$BRANVIL_DATA_DIR"; then
        printf 'The previous build cannot check the live data. See docs/hub-server.md before restarting.\n' >&2
        return 1
    fi
    start_agent && "$previous" install && rm -f "$pending"
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
        write_plist
        printf 'Watch for two Keychain prompts when starting a new build.\n'
        if ! start_agent; then
            if loaded; then launchctl bootout "$target"; fi
            exit 1
        fi
        "$current" install
        printf 'Installed %s. Its output goes to %s.\n' "$label" "$log"
        ;;
    update)
        stage="checking the running server"
        holder=$("$current" server hub-probe --ready) && [ -n "$holder" ] && [ "$holder" = "$(job_pid)" ] ||
            fail "Start the installed LaunchAgent before updating. Run: $0 restart"
        build_head
        previous=$(readlink "$current")
        if [ "$candidate" = "$previous" ]; then
            printf 'The Hub already uses %s.\n' "$revision"
            exit 0
        fi
        backup_and_rehearse
        stage="stopping the previous build"
        stop_agent
        assert_stopped "$previous"
        printf '%s\n' "$previous" >"$lock/update-from"
        mv -f "$lock/update-from" "$pending"
        stage="activating the new build"
        printf 'Watch for two Keychain prompts when starting a new build.\n'
        if admin_upgrade_and_check "$candidate" "$BRANVIL_DATA_DIR" &&
            point_current "$candidate" && start_agent && "$current" install; then
            rm -f "$pending"
            printf 'Hub updated to %s.\n' "$revision"
        else
            if recover; then printf 'The previous build is running again.\n' >&2; fi
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
        recover
        ;;
    uninstall)
        stage=uninstall
        stop_agent
        rm -f "$plist"
        ;;
esac
