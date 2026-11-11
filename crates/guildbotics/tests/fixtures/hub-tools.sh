#!/bin/sh
set -eu
name=${0##*/}
{
    printf '%s' "$name"
    for arg do printf '|%s' "$arg"; done
    printf '\n'
} >>"$FAKE_ROOT/calls"
case "$name" in
    uname) cat "$FAKE_ROOT/os" ;;
    id) printf '1234\n' ;;
    npm) : ;;
    rustc) printf 'rustc %s (fixture)\n' "$(sed -n 's/^channel = "\([^"]*\)"/\1/p' rust-toolchain.toml)" ;;
    cargo)
        while [ "$1" != --target-dir ]; do shift; done
        mkdir -p "$2/release"
        # A build is this stub marked with the commit's build-id and, like Cargo's
        # CARGO_MANIFEST_DIR, the physical path of its source tree.
        { cat "$0"; printf '# build: %s\n# source: %s\n' "$(cat build-id)" "$(pwd -P)"; } >"$2/release/guildbotics"
        chmod +x "$2/release/guildbotics"
        ;;
    security) cat "$FAKE_ROOT/identities" ;;
    codesign)
        if [ "$1" = -dv ]; then
            # Like codesign, name no team for a file that is not signed at all.
            team=$(sed -n 's/^# team: //p' "$2")
            if [ -n "$team" ]; then printf 'TeamIdentifier=%s\n' "$team" >&2; fi
            exit 0
        fi
        [ ! -f "$FAKE_ROOT/fail-sign" ] || exit 1
        if [ -f "$FAKE_ROOT/no-team" ]; then team="not set"
        else team=$(cat "$FAKE_ROOT/team" 2>/dev/null || printf FIXTURE123); fi
        # A new signature replaces the previous one.
        { grep -v '^# \(signed\|team\): ' "$6"; printf '# signed: %s %s\n# team: %s\n' "$3" "$5" "$team"; } >"$6.signed"
        chmod +x "$6.signed"
        mv -f "$6.signed" "$6"
        ;;
    plutil)
        if [ "$1" = -lint ]; then exit 0; fi
        case "$2" in
            ProgramArguments.0) key=ProgramArguments ;;
            EnvironmentVariables.*) key=${2#*.} ;;
            ExitTimeOut)
                sed -n 's|^<key>ExitTimeOut</key><integer>\([0-9]*\)</integer>|\1|p' "$6" | grep . && exit 0
                exit 1
                ;;
            *) exit 2 ;;
        esac
        line=$(sed -n "s|^<key>$key</key>.*<string>\\([^<]*\\)</string>.*|\\1|p" "$6")
        if [ "$key" = ProgramArguments ]; then
            line=$(sed -n 's|^<key>ProgramArguments</key><array><string>\([^<]*\)</string>.*|\1|p' "$6")
        fi
        [ -n "$line" ] || exit 1
        printf '%s\n' "$line" | sed 's/&lt;/</g; s/&gt;/>/g; s/&amp;/\&/g'
        ;;
    launchctl)
        case "$1" in
            print)
                case "$2" in */local.guildbotics*.update)
                    [ -f "$FAKE_ROOT/auto-loaded" ] || exit 1
                    if [ -f "$FAKE_ROOT/auto-unloading" ]; then rm -f "$FAKE_ROOT/auto-loaded" "$FAKE_ROOT/auto-unloading"; fi
                    exit 0 ;;
                esac
                # After a slow bootout, launchd lists the job for one more look.
                if [ -f "$FAKE_ROOT/unloading" ]; then
                    rm -f "$FAKE_ROOT/unloading"
                elif [ -f "$FAKE_ROOT/unloaded" ]; then
                    rm -f "$FAKE_ROOT/loaded" "$FAKE_ROOT/unloaded"
                fi
                [ -f "$FAKE_ROOT/loaded" ] || exit 1
                if [ -f "$FAKE_ROOT/job-pid" ]; then
                    printf '  pid = %s\n' "$(cat "$FAKE_ROOT/job-pid")"
                    printf '  last exit code = (never exited)\n'
                elif [ -f "$FAKE_ROOT/start-failed" ]; then
                    printf '  last exit code = 1\n'
                    if [ -f "$FAKE_ROOT/ready-after-exit" ]; then
                        printf '42\n' >"$FAKE_ROOT/job-pid"
                        printf '42\n' >"$FAKE_ROOT/holder"
                    fi
                fi
                ;;
            enable|disable) : ;;
            bootstrap)
                case "$3" in *local.guildbotics*.update.plist) exit 0 ;; esac
                # Like launchd, refuse a job that is still listed.
                if [ -f "$FAKE_ROOT/loaded" ]; then
                    printf 'Bootstrap failed: 5: Input/output error\n' >&2
                    exit 5
                fi
                touch "$FAKE_ROOT/loaded"
                bin=$(plutil -extract ProgramArguments.0 raw -o - "$3")
                build=$(sed -n 's/^# build: //p' "$bin")
                if [ "$build" = "$(cat "$FAKE_ROOT/fail-start" 2>/dev/null)" ]; then
                    touch "$FAKE_ROOT/start-failed"
                else
                    printf '42\n' >"$FAKE_ROOT/job-pid"
                    printf '42\n' >"$FAKE_ROOT/holder"
                    if [ -f "$FAKE_ROOT/service-autostart" ]; then touch "$FAKE_ROOT/service-running"; fi
                fi
                ;;
            bootout)
                case "$2" in */local.guildbotics*.update) touch "$FAKE_ROOT/auto-unloading"; exit 0 ;; esac
                if [ -f "$FAKE_ROOT/job-pid" ]; then rm -f "$FAKE_ROOT/holder"; fi
                rm -f "$FAKE_ROOT/job-pid" "$FAKE_ROOT/start-failed"
                if [ -f "$FAKE_ROOT/slow-bootout" ]; then
                    touch "$FAKE_ROOT/unloading" "$FAKE_ROOT/unloaded"
                    printf 'Boot-out failed: 36: Operation now in progress\n' >&2
                    exit 36
                else
                    rm -f "$FAKE_ROOT/loaded"
                fi
                ;;
            *) exit 2 ;;
        esac
        ;;
    current|guildbotics-bin|update-from|update-to|guildbotics)
        build=$(sed -n 's/^# build: //p' "$0")
        printf '%s' "$build" >>"$FAKE_ROOT/actions"
        for arg do printf '|%s' "$arg" >>"$FAKE_ROOT/actions"; done
        printf '\n' >>"$FAKE_ROOT/actions"
        case "$1:${2:-}" in
            server:hub-data)
                case "$3" in
                    backup) touch "$5/manifest.json" ;;
                    upgrade|check)
                        if [ "$#" -gt 3 ]; then
                            [ -f "$5/manifest.json" ] || exit 1
                            [ ! -f "$FAKE_ROOT/fail-gb-rehearsal" ] || exit 1
                        else
                            [ ! -f "$FAKE_ROOT/holder" ] || exit 1
                            if [ "$build" = old ]; then
                                [ ! -f "$FAKE_ROOT/gb-new-schema" ] || exit 1
                            else
                                if [ "$3" = upgrade ] && [ -f "$FAKE_ROOT/change-gb-schema" ]; then touch "$FAKE_ROOT/gb-new-schema"; fi
                                [ ! -f "$FAKE_ROOT/fail-gb-live" ] || exit 1
                            fi
                        fi
                        ;;
                    *) exit 2 ;;
                esac
                ;;
            server:hub-checks)
                state=$(cat "$FAKE_ROOT/checks" 2>/dev/null || printf success)
                printf '{"state":"%s"}\n' "$state"
                ;;
            server:hub-migration) printf '{"manual_update_version":0,"instructions":null}\n' ;;
            server:hub-notify)
                [ ! -f "$FAKE_ROOT/fail-notify" ] || exit 1
                printf '%s\n' "${GUILDBOTICS_IDENTITY_ROOT:-}" >"$FAKE_ROOT/notify-identity-root"
                cat >>"$FAKE_ROOT/notifications"
                printf '\n' >>"$FAKE_ROOT/notifications"
                printf '{"sent":false}\n'
                ;;
            service:status)
                [ ! -f "$FAKE_ROOT/service-error" ] || exit 1
                if [ -f "$FAKE_ROOT/unresponsive" ]; then
                    printf '{"server_running":false,"running":false,"state":"stopped"}\n'
                    exit 0
                fi
                if [ -f "$FAKE_ROOT/service-running" ]; then active=true; else active=false; fi
                printf '{"server_running":true,"running":%s}\n' "$active"
                ;;
            service:start) touch "$FAKE_ROOT/service-running" ;;
            service:stop) rm -f "$FAKE_ROOT/service-running" ;;
            environment:prune)
                [ ! -f "$HOME/Library/Application Support/GuildBotics/Hub${GUILDBOTICS_HUB_INSTANCE:+-$GUILDBOTICS_HUB_INSTANCE}/builds/update-from" ] || exit 1
                [ ! -f "$FAKE_ROOT/fail-prune" ] || exit 1
                ;;
            environment:status)
                if [ -f "$FAKE_ROOT/environment" ]; then cat "$FAKE_ROOT/environment"
                else printf '{"image":{"reference":null},"snapshot":{"state":"ready"}}\n'; fi
                ;;
            server:hub-probe)
                if [ "${3:-}" = --source ]; then
                    sed -n 's/^# source: //p' "$0"
                    exit 0
                fi
                if [ "${3:-}" = --exit-timeout ]; then
                    if [ "$build" = new ]; then printf '92\n'; else printf '91\n'; fi
                    exit 0
                fi
                if [ -f "$FAKE_ROOT/holder" ]; then cat "$FAKE_ROOT/holder"
                elif [ "$#" -eq 3 ]; then exit 1
                fi
                ;;
            server:stop)
                [ ! -f "$FAKE_ROOT/fail-stop" ] || exit 1
                rm -f "$FAKE_ROOT/holder" "$FAKE_ROOT/job-pid" "$FAKE_ROOT/service-running"
                ;;
            install:)
                [ "$build" = old ] || [ ! -f "$FAKE_ROOT/fail-install" ] || exit 1
                # Like guildbotics install, copy this executable to the machine root.
                mkdir -p "$GUILDBOTICS_HOME/bin"
                cp "$0" "$GUILDBOTICS_HOME/bin/guildbotics"
                ;;
            branvil:backup) touch "$6/manifest.json" ;;
            branvil:restore)
                [ -f "$4/manifest.json" ] || exit 1
                mkdir "$6"
                touch "$6/rehearsed"
                ;;
            branvil:admin)
                case "$3:$5" in
                    upgrade:*/operation/rehearsal)
                        [ -f "$5/rehearsed" ] || exit 1
                        touch "$5/upgraded"
                        exit 3
                        ;;
                    upgrade:*)
                        [ ! -f "$FAKE_ROOT/holder" ] || exit 1
                        if [ -f "$FAKE_ROOT/change-schema" ]; then touch "$FAKE_ROOT/new-schema"; fi
                        exit 0
                        ;;
                    check:*/operation/rehearsal)
                        [ -f "$5/upgraded" ] && [ ! -f "$FAKE_ROOT/fail-rehearsal" ]
                        ;;
                    check:*)
                        [ ! -f "$FAKE_ROOT/holder" ] &&
                        { [ "$build" != old ] || [ ! -f "$FAKE_ROOT/new-schema" ]; }
                        ;;
                    *) exit 2 ;;
                esac
                ;;
            *) exit 2 ;;
        esac
        ;;
    *) exit 2 ;;
esac
