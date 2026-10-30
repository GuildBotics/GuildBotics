#!/bin/sh
# A per-user OpenSSH forward, independent of any application runtime.
set -eu
umask 077

usage() {
    cat <<'EOF'
Usage: hub-forward.sh install --hub <SSH destination> --port <port> [--port <port> ...]
                              [--agent-socket <absolute path>] (Linux only)
       hub-forward.sh status | uninstall

install replaces the complete port list and starts the service now and at login.
First connect with ssh <destination> to accept the host key. Use an unencrypted
key or an unlocked ssh-agent. On Linux, pass its stable socket path explicitly;
without --agent-socket the service disables agent use. No SSH configuration or
keys are written. Restart attempts are throttled to 15-second intervals.
EOF
}
fail() { printf '%s\n' "$*" >&2; exit 1; }
action=${1:-help}
[ "$#" -eq 0 ] || shift
case "$action" in
    -h|--help|help) usage; exit 0 ;;
    install|status|uninstall) ;;
    *) usage >&2; exit 1 ;;
esac
hub= agent_socket= ports=
while [ "$#" -gt 0 ]; do
    [ "$action" = install ] || fail "Only install accepts options."
    [ "$#" -ge 2 ] || fail "Missing option value: $1"
    case "$1" in
        --hub) hub=$2 ;;
        --agent-socket) agent_socket=$2 ;;
        --port)
            case "$2" in ''|*[!0-9]*|0*) fail "Port must be an integer from 1 to 65535." ;; esac
            [ "${#2}" -le 5 ] && [ "$2" -le 65535 ] || fail "Port out of range."
            case " $ports " in *" $2 "*) ;; *) ports="${ports:+$ports }$2" ;; esac ;;
        *) fail "Unknown option: $1" ;;
    esac
    shift 2
done
os=$(uname -s)
case "$os" in Darwin|Linux) ;; *) fail "Supported systems: macOS and Linux." ;; esac
if [ "$action" = install ]; then
    # A destination is one operand, never an option, URI, or remote command.
    case "$hub" in ''|-*|*[!a-zA-Z0-9_.@:%+\[\]-]*) fail "Invalid SSH destination; use an SSH config alias or [user@]host." ;; esac
    [ -n "$ports" ] || fail "At least one --port is required."
    case "$agent_socket" in
        '') ;;
        /*) [ "$os" = Linux ] || fail "--agent-socket is Linux only." ;;
        *) fail "Agent socket must be an absolute path." ;;
    esac
    # Service formats cannot represent embedded line breaks safely.
    case "$HOME$agent_socket" in *'
'*) fail "Paths must not contain line breaks." ;; esac
    if printf '%s' "$HOME$agent_socket" | LC_ALL=C grep '[[:cntrl:]]' >/dev/null; then
        fail "Paths must not contain control characters."
    fi
    ssh_path=$(command -v ssh) || fail "OpenSSH client (ssh) is required."
    case "$ssh_path" in /*) ;; *) fail "ssh must resolve to an absolute executable path." ;; esac
fi
name=hub-forward
if [ "$os" = Darwin ]; then
    domain=gui/$(id -u)
    target=$domain/local.$name
    file=$HOME/Library/LaunchAgents/local.$name.plist
else
    file=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$name.service
fi
if [ "$action" = status ]; then
    if [ "$os" = Darwin ]; then
        launchctl print "$target"
    else
        systemctl --user status "$name.service"
    fi
    exit $?
fi
if [ "$os" = Darwin ]; then
    # Do not suppress an actual failure to stop a loaded job.
    if launchctl print "$target" >/dev/null 2>&1; then
        launchctl bootout "$target"
    fi
elif [ "$action" = uninstall ] && [ ! -f "$file" ]; then
    exit 0
fi
if [ "$action" = uninstall ]; then
    if [ "$os" = Linux ]; then systemctl --user disable --now "$name.service"; fi
    rm -f "$file"
    if [ "$os" = Linux ]; then systemctl --user daemon-reload; fi
    exit 0
fi
set -- "$ssh_path" -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=3 -o BatchMode=yes -o StrictHostKeyChecking=yes \
    -o ControlMaster=no -o ControlPath=none
if [ "$os" = Linux ]; then
    if [ -n "$agent_socket" ]; then
        set -- "$@" -o IdentityAgent=SSH_AUTH_SOCK
    else
        set -- "$@" -o IdentityAgent=none
    fi
fi
for port in $ports; do set -- "$@" -L "127.0.0.1:$port:127.0.0.1:$port"; done
set -- "$@" -- "$hub"
mkdir -p "$(dirname "$file")"
temp=$(mktemp "$file.XXXXXX")
trap 'rm -f "$temp"' 0 HUP INT TERM
if [ "$os" = Darwin ]; then
    xml() { printf '%s' "$1" | sed 's/\&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g; s/"/\&quot;/g; s/'"'"'/\&apos;/g'; }
    {
        printf '%s\n' '<?xml version="1.0" encoding="UTF-8"?>' '<plist version="1.0"><dict>'
        printf '<key>Label</key><string>local.%s</string>\n' "$name"
        printf '%s\n' '<key>ProgramArguments</key><array>'
        for arg do printf '<string>%s</string>\n' "$(xml "$arg")"; done
        printf '%s\n' '</array>' '<key>RunAtLoad</key><true/>' '<key>KeepAlive</key><true/>' \
            '<key>ThrottleInterval</key><integer>15</integer>'
        printf '<key>StandardErrorPath</key><string>%s/Library/Logs/%s.log</string>\n' "$(xml "$HOME")" "$name"
        printf '%s\n' '</dict></plist>'
    } >"$temp"
    mkdir -p "$HOME/Library/Logs"
    mv -f "$temp" "$file"
    launchctl enable "$target"
    launchctl bootstrap "$domain" "$file"
else
    # systemd parses its own words, specifiers (%) and environment references ($).
    unit_word() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/%/%%/g; s/\$/$$/g'; }
    {
        printf '%s\n' '[Unit]' 'Description=SSH loopback port forwarding' 'StartLimitIntervalSec=0' \
            '[Service]' 'Type=simple' 'Restart=always' 'RestartSec=15'
        # Environment= does not perform dollar expansion.
        printf 'Environment="SSH_AUTH_SOCK=%s"\n' "$(printf '%s' "$agent_socket" | sed 's/\\/\\\\/g; s/"/\\"/g; s/%/%%/g')"
        # systemd forbids dollar signs in the executable word, even quoted.
        # env execs ssh, so the service still tracks the SSH process itself.
        printf 'ExecStart=/usr/bin/env '
        for arg do printf '"%s" ' "$(unit_word "$arg")"; done
        printf '\n%s\n' '[Install]' 'WantedBy=default.target'
    } >"$temp"
    mv -f "$temp" "$file"
    systemctl --user daemon-reload
    systemctl --user enable "$name.service"
    systemctl --user restart "$name.service"
fi
printf 'Installed %s. Use status to inspect the process and its last exit.\n' "$name"
