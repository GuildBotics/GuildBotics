//! `guildbotics server start` runs Branvil in its own process, and Branvil
//! starts its commands again on this executable: a push runs the hooks as
//! `guildbotics branvil hook`.

#![cfg(unix)]

use std::io::{BufRead, BufReader};
use std::path::Path;
use std::process::{Child, Command, Output, Stdio};
use std::sync::mpsc;
use std::time::Duration;

const BIN: &str = env!("CARGO_BIN_EXE_guildbotics");

fn guildbotics(data: &Path, args: &[&str]) -> Output {
    Command::new(BIN)
        .args(args)
        .env("BRANVIL_DATA_DIR", data)
        .stdin(Stdio::null())
        .output()
        .unwrap()
}

/// Git with no configuration but its own, answering the credential prompt
/// with the owner's token.
fn git(dir: &Path, home: &Path, token: &str, args: &[&str]) -> Output {
    let askpass = home.join("askpass.sh");
    std::fs::write(
        &askpass,
        "#!/bin/sh\ncase \"$1\" in\n  Username*) echo owner ;;\n  *) printf '%s\\n' \"$TEST_TOKEN\" ;;\nesac\n",
    )
    .unwrap();
    std::fs::set_permissions(
        &askpass,
        std::os::unix::fs::PermissionsExt::from_mode(0o700),
    )
    .unwrap();
    let global = home.join("gitconfig");
    std::fs::write(&global, "").unwrap();
    let mut cmd = Command::new("git");
    for (key, _) in std::env::vars() {
        if key.starts_with("GIT_") || key == "SSH_ASKPASS" {
            cmd.env_remove(key);
        }
    }
    cmd.current_dir(dir)
        .args(["-c", "credential.helper=", "-c", "core.askPass="])
        .args(args)
        .env("HOME", home)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", &global)
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GIT_ASKPASS", &askpass)
        .env("TEST_TOKEN", token)
        .env("GIT_AUTHOR_NAME", "Test")
        .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
        .env("GIT_COMMITTER_NAME", "Test")
        .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
        .stdin(Stdio::null())
        .output()
        .unwrap()
}

fn assert_ok(output: &Output) {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

/// Starts the server and returns it with the URL it says Branvil listens on.
fn start(data: &Path) -> (Child, String) {
    let mut child = Command::new(BIN)
        .args(["server", "start"])
        .env("BRANVIL_DATA_DIR", data)
        .env("NO_COLOR", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let (lines, received) = mpsc::channel();
    let stderr = child.stderr.take().unwrap();
    std::thread::spawn(move || {
        for line in BufReader::new(stderr).lines().map_while(Result::ok) {
            let _ = lines.send(line);
        }
    });
    let url = loop {
        let line = received
            .recv_timeout(Duration::from_secs(30))
            .expect("the server says where it listens");
        if let Some((_, rest)) = line.split_once("listening on ") {
            break rest.split_whitespace().next().unwrap().to_string();
        }
    };
    (child, url)
}

#[test]
fn branvil_runs_in_the_server_and_its_hooks_run_on_this_executable() {
    let dir = tempfile::tempdir().unwrap();
    let data = dir.path().join("data");
    assert_ok(&guildbotics(
        &data,
        &[
            "branvil",
            "init",
            "--owner",
            "owner",
            "--listen",
            "127.0.0.1:0",
        ],
    ));
    let token = std::fs::read_to_string(data.join("secrets/owner.token")).unwrap();
    let (server, url) = start(&data);

    for args in [
        ["branvil", "workspace", "create", "acme"],
        ["branvil", "repo", "create", "acme/demo"],
    ] {
        let mut args = args.to_vec();
        args.extend(["--url", &url]);
        assert_ok(&guildbotics(&data, &args));
    }
    let hook = std::fs::read_to_string(data.join("hooks/pre-receive")).unwrap();
    assert!(
        hook.contains(&format!("exec '{BIN}' 'branvil' 'hook' 'pre-receive'")),
        "{hook}"
    );

    let home = dir.path().join("home");
    let work = dir.path().join("work");
    std::fs::create_dir_all(&home).unwrap();
    std::fs::create_dir_all(&work).unwrap();
    std::fs::write(work.join("README.md"), "demo\n").unwrap();
    for args in [
        vec!["init", "-q", "-b", "main"],
        vec!["add", "README.md"],
        vec!["commit", "-q", "-m", "first"],
    ] {
        assert_ok(&git(&work, &home, "", &args));
    }
    // The pre-receive hook refuses a push it cannot run.
    let remote = format!("{url}/git/acme/demo.git");
    assert_ok(&git(
        &work,
        &home,
        token.trim(),
        &["push", "-q", &remote, "main"],
    ));
    let listed = git(&work, &home, token.trim(), &["ls-remote", &remote, "main"]);
    assert_ok(&listed);
    let head = git(&work, &home, "", &["rev-parse", "HEAD"]);
    assert!(
        String::from_utf8_lossy(&listed.stdout)
            .starts_with(String::from_utf8_lossy(&head.stdout).trim())
    );

    // Nothing but the server's own notices on stdout, and a stop asked for is
    // a successful one.
    assert_ok(
        &Command::new("kill")
            .args(["-INT", &server.id().to_string()])
            .output()
            .unwrap(),
    );
    let stopped = server.wait_with_output().unwrap();
    assert!(stopped.status.success(), "{:?}", stopped.status);
    assert_eq!(String::from_utf8_lossy(&stopped.stdout), "");
}

#[test]
fn an_empty_branvil_data_dir_is_refused_rather_than_the_current_directory() {
    let dir = tempfile::tempdir().unwrap();
    let started = Command::new(BIN)
        .args(["server", "start"])
        .current_dir(dir.path())
        .env("BRANVIL_DATA_DIR", "")
        .stdin(Stdio::null())
        .output()
        .unwrap();

    assert!(!started.status.success());
    assert!(String::from_utf8_lossy(&started.stderr).contains("BRANVIL_DATA_DIR is empty"));
    assert_eq!(std::fs::read_dir(dir.path()).unwrap().count(), 0);
}
