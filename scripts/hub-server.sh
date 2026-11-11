#!/bin/sh
# Build and run the integrated Hub as one macOS user agent.
set -eu
umask 077

usage() {
    cat <<'EOF'
Usage: scripts/hub-server.sh install | update | restart | status | recover | uninstall | auto-install | auto-uninstall | auto-once

install    Build HEAD, install the CLI and skill, and start the LaunchAgent.
           Set BRANVIL_DATA_DIR to the absolute path of the initialized data,
           and GUILDBOTICS_HUB_SIGNING_IDENTITY to the code signing identity.
update     Build HEAD, back up Branvil, rehearse the upgrade, and switch builds.
restart    Let in-flight work finish, then start the current build again.
status     Show the LaunchAgent and its last exit.
recover    Resume an interrupted update or return to the previous build.
uninstall  Let in-flight work finish and remove the LaunchAgent.
auto-install    Register the main-watching update agent using this PATH.
auto-uninstall  Stop and unregister automatic updates.
auto-once       Run one automatic update inspection (also used by tests).

Run install and update from the Hub owner's macOS desktop session. See
docs/hub-server.md for Keychain prompts, backup locations, and recovery.
EOF
}
fail() { printf '%s\n' "$*" >&2; exit 1; }
action=${1:-help}
[ "$#" -le 1 ] || fail "No options are accepted."
case "$action" in
    -h|--help|help) usage; exit 0 ;;
    install|update|restart|status|recover|uninstall|auto-install|auto-uninstall|auto-once) ;;
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
# launchd runs a signed copy of the active build at one fixed path, so macOS
# keeps the Documents folder access it granted to that path. The copy is the
# only record of the active build: it names the source tree it was built from.
current=$builds/current
# During an update, the copy that ran before it: what a rollback restores.
pending=$builds/update-from
signing=$hub/signing-identity
backups=$hub/backups

if [ "$action" = status ]; then exec launchctl print "$target"; fi

# The loop invokes update, which acquires its own operation lock.
if [ "$action" = auto-once ]; then exec node "$repo/scripts/hub-auto-update.mjs" --once; fi

# Use one lock for the build pointer, backup rotation and LaunchAgent changes.
mkdir -p "$builds"
lock=$hub/operation
mkdir "$lock" 2>/dev/null || fail "Another Hub operation holds $lock. See docs/hub-server.md."
stage=setup
finish() {
    if [ "$1" -ne 0 ]; then
        running=$("$current" server hub-probe --source 2>/dev/null || true)
        message="$(date -u +%FT%TZ): Hub $action failed during $stage (exit $1, active revision: ${running##*/}, requested revision: ${revision:-unknown})."
        printf '%s\n' "$message" | tee "$hub/last-failure" >&2 || true
    fi
    rm -rf "$lock"
}
trap 'finish "$?"' 0
trap 'exit 130' INT
trap 'exit 143' HUP TERM

case "$action" in
    auto-install|auto-uninstall)
        auto_label=local.guildbotics.update
        auto_plist=$HOME/Library/LaunchAgents/$auto_label.plist
        auto_target=$domain/$auto_label
        if launchctl print "$auto_target" >/dev/null 2>&1; then
            launchctl bootout "$auto_target" || true
            auto_waited=0
            while launchctl print "$auto_target" >/dev/null 2>&1; do
                [ "$auto_waited" -lt 30 ] || fail "The automatic update agent did not stop."
                sleep 1
                auto_waited=$((auto_waited + 1))
            done
        fi
        if [ "$action" = auto-uninstall ]; then rm -f "$auto_plist"; exit 0; fi
        node --version | grep -Eq '^v24\.' || fail "Node.js 24 must be in PATH."
        npm --version >/dev/null
        git --version >/dev/null
        rust_version=$(sed -n 's/^channel = "\([^"]*\)"/\1/p' "$repo/rust-toolchain.toml")
        rustc --version | grep -F "rustc $rust_version " >/dev/null || fail "Put the declared Rust toolchain in PATH."
        mkdir -p "$(dirname "$auto_plist")" "$HOME/Library/Logs"
        auto_xml() { printf '%s' "$1" | sed 's/\&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'; }
        {
            printf '%s\n' '<?xml version="1.0" encoding="UTF-8"?>' '<plist version="1.0"><dict>'
            printf '<key>Label</key><string>%s</string>\n' "$auto_label"
            printf '<key>ProgramArguments</key><array><string>%s</string><string>%s</string></array>\n' "$(auto_xml "$(command -v node)")" "$(auto_xml "$repo/scripts/hub-auto-update.mjs")"
            printf '<key>WorkingDirectory</key><string>%s</string>\n' "$(auto_xml "$repo")"
            printf '<key>EnvironmentVariables</key><dict><key>PATH</key><string>%s</string><key>HOME</key><string>%s</string></dict>\n' "$(auto_xml "$PATH")" "$(auto_xml "$HOME")"
            printf '%s\n' '<key>RunAtLoad</key><true/>' '<key>KeepAlive</key><true/>' '<key>ThrottleInterval</key><integer>15</integer>'
            printf '<key>StandardOutPath</key><string>%s</string><key>StandardErrorPath</key><string>%s</string>\n' "$(auto_xml "$HOME/Library/Logs/guildbotics-update.log")" "$(auto_xml "$HOME/Library/Logs/guildbotics-update.log")"
            printf '%s\n' '</dict></plist>'
        } >"$auto_plist"
        plutil -lint "$auto_plist"
        launchctl enable "$domain/$auto_label"
        launchctl bootstrap "$domain" "$auto_plist"
        exit 0
        ;;
esac

loaded() { launchctl print "$target" >/dev/null 2>&1; }
# launchctl bootout can return, or fail with "Operation now in progress",
# while launchd still lists the job, and a bootstrap or install refuses a
# listed job. Wait until it is gone, as long as launchd may take to stop it.
unload() {
    loaded || return 0
    launchctl bootout "$target" || true
    limit=$(($(setting ExitTimeOut) + 30))
    waited=0
    while loaded; do
        if [ "$waited" -ge "$limit" ]; then
            printf 'launchd still lists %s after %s seconds.\n' "$label" "$limit" >&2
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
}
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
    unload
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
    cp "$1" "$lock/current" && mv -f "$lock/current" "$current"
}
# A build is named by its revision directory; the executable reports that
# directory by its physical path, which need not spell $builds.
built_from() {
    tree=$("$1" server hub-probe --source) && printf '%s/%s/guildbotics-bin\n' "$builds" "${tree##*/}"
}
team_of() { codesign -dv "$1" 2>&1 | sed -n 's/^TeamIdentifier=//p'; }
admin_upgrade_and_check() {
    code=0
    "$1" branvil admin upgrade --data-dir "$2" || code=$?
    case "$code" in
        0|3) "$1" branvil admin check --data-dir "$2" ;;
        *) return "$code" ;;
    esac
}

build_head() {
    stage="checking the signing identity"
    # Match the whole SHA-1 or the whole quoted name of a valid identity.
    security find-identity -v -p codesigning |
        awk -v id="$identity" '$2 == id || index($0, "\"" id "\"") { found = 1 } END { exit !found }' ||
        fail "\"$identity\" is not a valid code signing identity in the Keychain. See docs/hub-server.md."
    stage=build
    revision=$(git -C "$repo" rev-parse --verify HEAD)
    source=$builds/$revision
    candidate=$source/guildbotics-bin
    if [ -x "$candidate" ]; then
        cp "$candidate" "$candidate.new"
    else
        # The binary uses templates and the Desktop build in its source tree.
        # Keep this exact source at its build path even as the Hub checkout advances.
        rm -rf "$source"
        mkdir "$source"
        git -C "$repo" archive --format=tar --output="$lock/source.tar" "$revision"
        # Extract with the current time: Cargo rebuilds what is newer than its last
        # build, and a commit's own time can be older than that.
        tar -xmf "$lock/source.tar" -C "$source"
        rm -f "$lock/source.tar"
        (
            cd "$source"
            npm ci
            npm run build --workspace desktop --workspace crates/branvil/web
            cargo build --release --locked -p guildbotics --target-dir "$hub/target"
        )
        cp "$hub/target/release/guildbotics" "$candidate.new"
    fi
    # Sign a reused build too, so that every build carries the recorded identity.
    stage=signing
    codesign --force -s "$identity" -i "$label" "$candidate.new"
    # Keychain access survives a rebuild only for a signer with a Team ID, and
    # only while that Team ID stays the same.
    team=$(team_of "$candidate.new")
    printf '%s\n' "$team" | grep -Eq '^[0-9A-Z]{10}$' ||
        fail "\"$identity\" has no Team ID, so macOS would ask for Keychain access after every update. See docs/hub-server.md."
    [ "$team" = "${running_team-$team}" ] ||
        fail "\"$identity\" signs for Team ID $team, but the running build is signed for $running_team. To change teams, run $0 uninstall, then install again. See docs/hub-server.md."
    mv -f "$candidate.new" "$candidate"
}
backup_and_rehearse() {
    stage=backup
    mkdir -p "$backups"
    backup=$(mktemp -d "$backups/branvil-update-$(date -u +%Y%m%dT%H%M%SZ).XXXXXX")
    # Use the installed instance's owner credential and address.
    unset BRANVIL_TOKEN BRANVIL_URL
    "$current" branvil backup --data-dir "$BRANVIL_DATA_DIR" --to "$backup"
    printf 'Backup before update: %s\n' "$backup"

    # No other backup writer can run under operation. Remove interrupted copies.
    set --
    for saved in "$backups"/branvil-update-*; do
        if [ "$saved" != "$backup" ] && [ -d "$saved" ] && [ ! -L "$saved" ]; then
            if [ -f "$saved/manifest.json" ]; then set -- "$@" "$saved"
            else rm -rf "$saved"; fi
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
    rm -f "$pending" "$builds/update-to" || return
    stage="pruning obsolete snapshots"
    # Activation is committed: cleanup must never trigger a rollback.
    "$current" environment prune || return
    install_assets
}
rollback() {
    # The start failed before its authenticated readiness was observed.
    unload || return
    assert_stopped "$pending" || return
    point_current "$pending" || return
    # The previous build may refuse the upgraded schema. Leave the data in
    # place and stay stopped for the operator to decide how to restore it.
    if ! "$current" branvil admin check --data-dir "$BRANVIL_DATA_DIR"; then
        launchctl disable "$target" || return
        printf 'The previous build cannot check the live data. See docs/hub-server.md before restarting.\n' >&2
        return 1
    fi
    start_agent && rm -f "$pending" "$builds/update-to"
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
        identity=${GUILDBOTICS_HUB_SIGNING_IDENTITY:-}
        [ -n "$identity" ] ||
            fail "Set GUILDBOTICS_HUB_SIGNING_IDENTITY to the code signing identity. See docs/hub-server.md."
        case "$HOME$PATH$GUILDBOTICS_HOME$BRANVIL_DATA_DIR${GUILDBOTICS_SERVER_PORT:-}$identity" in
            *'
'*) fail "Paths and environment settings must not contain line breaks." ;;
        esac
        export BRANVIL_DATA_DIR GUILDBOTICS_HOME
        if [ -e "$plist" ] || loaded; then
            installed || fail "Remove the existing $label LaunchAgent before installing the integrated server."
        fi
        build_head
        if installed && [ -e "$current" ] && [ "$(built_from "$current")" != "$candidate" ]; then
            fail "A different build is installed. Run: $0 update"
        fi
        stage="checking the stopped server"
        assert_stopped "$candidate"
        "$candidate" branvil admin check --data-dir "$BRANVIL_DATA_DIR"
        unload
        stage="installing the agent"
        point_current "$candidate"
        printf '%s\n' "$identity" >"$signing"
        printf 'Allow Keychain access and Documents folder access if macOS asks. See docs/hub-server.md for details.\n'
        if ! start_agent; then
            unload
            exit 1
        fi
        install_assets
        printf 'Installed %s. Its output goes to %s.\n' "$label" "$log"
        ;;
    update)
        stage="checking the running server"
        agent_ready ||
            fail "Start the installed LaunchAgent before updating. Run: $0 restart"
        [ -f "$signing" ] ||
            fail "No signing identity is recorded. Run $0 uninstall, then install again with GUILDBOTICS_HUB_SIGNING_IDENTITY. See docs/hub-server.md."
        identity=$(cat "$signing")
        running_team=$(team_of "$current")
        build_head
        previous=$(built_from "$current")
        if [ "$candidate" = "$previous" ]; then
            printf 'The Hub already uses %s.\n' "$revision"
            install_assets
            exit 0
        fi
        # Copy while the server still runs; current stays until the switch.
        cp "$current" "$lock/update-from"
        backup_and_rehearse
        stage="stopping the previous build"
        stop_agent
        assert_stopped "$current"
        cp "$candidate" "$builds/update-to"
        mv -f "$lock/update-from" "$pending"
        stage="activating the new build"
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
            unload
            assert_stopped "$current"
            if [ -x "$builds/update-to" ] &&
                "$builds/update-to" branvil admin check --data-dir "$BRANVIL_DATA_DIR" &&
                point_current "$builds/update-to" && start_agent; then
                complete_activation
            else
                rollback
            fi
        fi
        ;;
    uninstall)
        stage=uninstall
        stop_agent
        rm -f "$plist"
        ;;
esac
