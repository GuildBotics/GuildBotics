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
    cargo)
        while [ "$1" != --target-dir ]; do shift; done
        mkdir -p "$2/release"
        cp "$0" "$2/release/guildbotics"
        ;;
    plutil)
        if [ "$1" = -lint ]; then exit 0; fi
        case "$2" in
            ProgramArguments.0) key=ProgramArguments ;;
            EnvironmentVariables.*) key=${2#*.} ;;
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
                [ -f "$FAKE_ROOT/loaded" ] || exit 1
                if [ -f "$FAKE_ROOT/job-pid" ]; then
                    printf '  pid = %s\n' "$(cat "$FAKE_ROOT/job-pid")"
                    printf '  last exit code = (never exited)\n'
                elif [ -f "$FAKE_ROOT/start-failed" ]; then
                    printf '  last exit code = 1\n'
                fi
                ;;
            enable) : ;;
            bootstrap)
                touch "$FAKE_ROOT/loaded"
                bin=$(plutil -extract ProgramArguments.0 raw -o - "$3")
                build=$(cat "$(dirname "$(readlink "$bin")")/build-id")
                if [ "$build" = new ] && [ -f "$FAKE_ROOT/fail-start" ]; then
                    touch "$FAKE_ROOT/start-failed"
                else
                    printf '42\n' >"$FAKE_ROOT/job-pid"
                    printf '42\n' >"$FAKE_ROOT/holder"
                fi
                ;;
            bootout)
                if [ -f "$FAKE_ROOT/job-pid" ]; then rm -f "$FAKE_ROOT/holder"; fi
                rm -f "$FAKE_ROOT/loaded" "$FAKE_ROOT/job-pid" "$FAKE_ROOT/start-failed"
                ;;
            *) exit 2 ;;
        esac
        ;;
    current|guildbotics-bin)
        bin=$0
        if [ -L "$bin" ]; then bin=$(readlink "$bin"); fi
        build=$(cat "$(dirname "$bin")/build-id")
        printf '%s' "$build" >>"$FAKE_ROOT/actions"
        for arg do printf '|%s' "$arg" >>"$FAKE_ROOT/actions"; done
        printf '\n' >>"$FAKE_ROOT/actions"
        case "$1:${2:-}" in
            server:hub-probe)
                if [ -f "$FAKE_ROOT/holder" ]; then cat "$FAKE_ROOT/holder"
                elif [ "$#" -eq 3 ]; then exit 1
                fi
                ;;
            server:stop)
                [ ! -f "$FAKE_ROOT/fail-stop" ] || exit 1
                rm -f "$FAKE_ROOT/holder" "$FAKE_ROOT/job-pid"
                ;;
            install:) : ;;
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
                        { [ "$build" = new ] || [ ! -f "$FAKE_ROOT/new-schema" ]; }
                        ;;
                    *) exit 2 ;;
                esac
                ;;
            *) exit 2 ;;
        esac
        ;;
    *) exit 2 ;;
esac
