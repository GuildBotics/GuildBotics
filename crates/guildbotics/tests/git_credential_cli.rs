use std::{
    io::Write,
    path::Path,
    process::{Command, Output, Stdio},
};

fn answer(home: &Path, action: &str, request: &str) -> Output {
    let mut child = Command::new(env!("CARGO_BIN_EXE_guildbotics"))
        .args(["git-credential", action])
        .env(gb_base::home::USER_HOME_ENV, home)
        .env("GUILDBOTICS_HOME", home.join(".guildbotics"))
        .env("GUILDBOTICS_IDENTITY_ROOT", home.join("identity"))
        .env("LANGUAGE", "en")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(request.as_bytes())
        .unwrap();
    child.wait_with_output().unwrap()
}

#[test]
fn git_credential_protocol_ignores_non_get_without_reading_identity() {
    let home = tempfile::tempdir().unwrap();
    let request =
        "protocol=https\nhost=forge.example.invalid:9443\npath=a/b\npassword=fake-do-not-echo\n";
    for action in ["store", "erase", "unknown"] {
        let response = answer(home.path(), action, request);
        assert!(response.status.success());
        assert!(response.stdout.is_empty() && response.stderr.is_empty());
    }
    assert!(!home.path().join(".guildbotics").exists());
}

#[test]
fn git_credential_ignores_invalid_origins_without_reading_identity_or_keychain() {
    let home = tempfile::tempdir().unwrap();
    let identity = home.path().join("identity/.guildbotics/config");
    std::fs::create_dir_all(identity.join("connections")).unwrap();
    // Neither identity configuration nor the OS keychain is needed to reject malformed input.
    std::fs::write(identity.join("connections/branvil.yml"), "broken: [").unwrap();
    std::fs::write(identity.join("secrets.yml"), "broken: [").unwrap();
    for request in [
        "protocol=http\nhost=owner@127.0.0.1:7878\n",
        "protocol=https\nhost=forge.example.invalid/path\n",
        "protocol=http+unsafe\nhost=127.0.0.1:7878\n",
        "protocol=https\n",
        "host=127.0.0.1:7878\n",
        "host=127.0.0.1:7878\r\n",
    ] {
        let response = answer(
            home.path(),
            "get",
            &format!("{request}password=fake-do-not-echo\n\n"),
        );
        assert!(response.status.success());
        assert!(response.stdout.is_empty());
        assert_eq!(
            String::from_utf8(response.stderr).unwrap(),
            "GuildBotics is connected to no service at  on this machine.\n"
        );
    }
    assert!(!home.path().join(".guildbotics").exists());
}
