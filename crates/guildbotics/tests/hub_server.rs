//! Exercise the Hub script with a real Git source tree and stubbed OS tools.
#![cfg(unix)]

use std::{
    fs,
    os::unix::fs::{PermissionsExt, symlink},
    path::{Path, PathBuf},
    process::{Command, Output},
};

const SCRIPT: &str = include_str!("../../../scripts/hub-server.sh");
const TOOLS: &str = include_str!("fixtures/hub-tools.sh");

struct Hub {
    temp: tempfile::TempDir,
    home: PathBuf,
    repo: PathBuf,
    path: String,
}

impl Hub {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let home = root.join("home with spaces");
        let repo = root.join("checkout");
        let tools = root.join("tools");
        for directory in [&home, &repo.join("scripts"), &tools, &root.join("branvil")] {
            fs::create_dir_all(directory).unwrap();
        }
        fs::write(repo.join("scripts/hub-server.sh"), SCRIPT).unwrap();
        fs::write(root.join("os"), "Darwin\n").unwrap();
        let stub = tools.join("stub");
        fs::write(&stub, TOOLS).unwrap();
        fs::set_permissions(&stub, fs::Permissions::from_mode(0o755)).unwrap();
        for name in ["uname", "id", "launchctl", "npm", "cargo", "plutil"] {
            symlink(&stub, tools.join(name)).unwrap();
        }
        let path = format!("{}:/usr/bin:/bin", tools.display());
        let hub = Self {
            temp,
            home,
            repo,
            path,
        };
        hub.git(&["init", "-q"]);
        hub.commit("old");
        hub
    }

    fn root(&self) -> &Path {
        self.temp.path()
    }

    fn builds(&self) -> PathBuf {
        self.home
            .join("Library/Application Support/GuildBotics/Hub/builds")
    }

    fn plist(&self) -> PathBuf {
        self.home
            .join("Library/LaunchAgents/local.guildbotics.plist")
    }

    fn command(&self, program: &str) -> Command {
        let mut command = Command::new(program);
        command
            .current_dir(&self.repo)
            .env_clear()
            .env("HOME", &self.home)
            .env("PATH", &self.path)
            .env("FAKE_ROOT", self.root())
            .env("BRANVIL_DATA_DIR", self.root().join("branvil"))
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_AUTHOR_NAME", "Fixture")
            .env("GIT_AUTHOR_EMAIL", "fixture@example.test")
            .env("GIT_COMMITTER_NAME", "Fixture")
            .env("GIT_COMMITTER_EMAIL", "fixture@example.test");
        command
    }

    fn git(&self, args: &[&str]) {
        let result = self.command("git").args(args).output().unwrap();
        assert!(result.status.success(), "{}", text(&result));
    }

    fn commit(&self, name: &str) {
        fs::write(self.repo.join("build-id"), name).unwrap();
        self.git(&["add", "scripts/hub-server.sh", "build-id"]);
        self.git(&["commit", "-qm", name]);
    }

    fn run(&self, action: &str, port: Option<&str>, success: bool) -> Output {
        for file in ["calls", "actions"] {
            fs::write(self.root().join(file), "").unwrap();
        }
        let mut command = self.command("sh");
        if let Some(port) = port {
            command.env("GUILDBOTICS_SERVER_PORT", port);
        }
        let result = command
            .args(["scripts/hub-server.sh", action])
            .output()
            .unwrap();
        assert_eq!(result.status.success(), success, "{}", text(&result));
        result
    }

    fn lines(&self, name: &str) -> String {
        fs::read_to_string(self.root().join(name)).unwrap()
    }

    fn mark(&self, name: &str) {
        fs::write(self.root().join(name), "").unwrap();
    }

    fn installed_build(&self) -> PathBuf {
        fs::read_link(self.builds().join("current")).unwrap()
    }

    fn install(&self) {
        self.run("install", None, true);
    }
}

fn text(result: &Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&result.stdout),
        String::from_utf8_lossy(&result.stderr)
    )
}

fn in_order(haystack: &str, needles: &[&str]) {
    let mut rest = haystack;
    for needle in needles {
        rest = rest
            .split_once(needle)
            .unwrap_or_else(|| panic!("missing {needle:?} in {rest:?}"))
            .1;
    }
}

#[test]
fn install_runs_one_server_at_login_and_installs_the_cli() {
    let hub = Hub::new();
    let out = hub.run("install", Some("9876"), true);
    let plist = fs::read_to_string(hub.plist()).unwrap();
    let log = hub.home.join("Library/Logs/guildbotics.log");
    let current = hub.builds().join("current");
    for entry in [
        format!(
            "<key>ProgramArguments</key><array><string>{}</string><string>server</string><string>start</string></array>",
            current.display()
        ),
        format!("<key>PATH</key><string>{}</string>", hub.path),
        "<key>GUILDBOTICS_SERVER_PORT</key><string>9876</string>".into(),
        "<key>RunAtLoad</key><true/>".into(),
        "<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>".into(),
        "<key>ExitTimeOut</key><integer>90</integer>".into(),
        format!(
            "<key>StandardOutPath</key><string>{}</string>",
            log.display()
        ),
        format!(
            "<key>StandardErrorPath</key><string>{}</string>",
            log.display()
        ),
    ] {
        assert!(plist.contains(&entry), "{entry}");
    }
    in_order(
        &hub.lines("calls"),
        &[
            "launchctl|enable|gui/1234/local.guildbotics",
            "launchctl|bootstrap|gui/1234|",
        ],
    );
    assert!(hub.lines("actions").contains("old|install\n"));
    assert!(text(&out).contains(log.to_str().unwrap()));
    assert_eq!(
        fs::read_to_string(hub.installed_build().parent().unwrap().join("build-id")).unwrap(),
        "old"
    );
}

#[test]
fn install_leaves_an_unspecified_port_to_the_server() {
    let hub = Hub::new();
    hub.install();
    let plist = fs::read_to_string(hub.plist()).unwrap();
    assert!(!plist.contains("GUILDBOTICS_SERVER_PORT"));
    assert!(plist.contains("<key>BRANVIL_DATA_DIR</key>"));
}

#[test]
fn install_leaves_an_existing_server_alone() {
    let hub = Hub::new();
    fs::write(hub.root().join("holder"), "999\n").unwrap();
    let out = hub.run("install", None, false);
    assert!(text(&out).contains("A GuildBotics server is running"));
    assert!(!hub.plist().exists());
    assert!(!hub.lines("actions").contains("server|stop"));
    assert!(!hub.lines("calls").contains("launchctl|bootstrap"));
    assert_eq!(hub.lines("holder"), "999\n");
}

#[test]
fn reinstall_replaces_only_a_stopped_agent() {
    let hub = Hub::new();
    hub.install();
    hub.run("install", None, false);
    assert!(!hub.lines("actions").contains("server|stop"));
    assert!(!hub.lines("calls").contains("launchctl|bootout"));
    fs::remove_file(hub.root().join("holder")).unwrap();
    fs::remove_file(hub.root().join("job-pid")).unwrap();
    hub.install();
    in_order(
        &hub.lines("calls"),
        &[
            "launchctl|bootout|",
            "launchctl|enable|",
            "launchctl|bootstrap|",
        ],
    );
}

#[test]
fn restart_waits_for_graceful_stop_before_bootstrap() {
    let hub = Hub::new();
    hub.install();
    hub.run("restart", None, true);
    in_order(
        &hub.lines("calls"),
        &[
            "current|server|stop",
            "launchctl|bootout|",
            "launchctl|bootstrap|",
        ],
    );
    assert_eq!(hub.lines("holder"), "42\n");
}

#[test]
fn stop_failure_leaves_the_job_and_build_alone() {
    let hub = Hub::new();
    hub.install();
    let old = hub.installed_build();
    hub.mark("fail-stop");
    hub.run("restart", None, false);
    assert!(!hub.lines("calls").contains("launchctl|bootstrap"));
    assert!(!hub.lines("calls").contains("launchctl|bootout"));
    assert_eq!(hub.installed_build(), old);
    assert_eq!(hub.lines("holder"), "42\n");
}

#[test]
fn update_builds_and_rehearses_before_stopping_the_old_server() {
    let hub = Hub::new();
    hub.install();
    let old = hub.installed_build();
    hub.commit("new");
    let out = hub.run("update", None, true);
    in_order(
        &hub.lines("calls"),
        &[
            "npm|ci",
            "npm|run|build|--workspaces",
            "cargo|build|--release|--locked|-p|guildbotics|--target-dir|",
            "current|branvil|backup|",
            "current|branvil|restore|",
            "guildbotics-bin|branvil|admin|upgrade|",
            "guildbotics-bin|branvil|admin|check|",
            "current|server|stop",
            "launchctl|bootout|",
            "guildbotics-bin|branvil|admin|upgrade|",
            "guildbotics-bin|branvil|admin|check|",
            "launchctl|bootstrap|",
            "current|install",
        ],
    );
    assert!(text(&out).contains("two Keychain prompts"));
    assert_ne!(hub.installed_build(), old);
    assert!(old.exists());
    assert!(!hub.builds().join("update-from").exists());
}

#[test]
fn rehearsal_failure_leaves_the_running_build_alone() {
    let hub = Hub::new();
    hub.install();
    let old = hub.installed_build();
    hub.commit("new");
    hub.mark("fail-rehearsal");
    hub.run("update", None, false);
    assert_eq!(hub.installed_build(), old);
    assert_eq!(hub.lines("holder"), "42\n");
    assert!(!hub.lines("calls").contains("current|server|stop"));
    let state = hub.builds().parent().unwrap().to_path_buf();
    assert!(
        fs::read_to_string(state.join("last-failure"))
            .unwrap()
            .contains("rehearsal")
    );
    assert!(!state.join("operation").exists());
}

#[test]
fn failed_start_returns_to_the_previous_compatible_build() {
    let hub = Hub::new();
    hub.install();
    let old = hub.installed_build();
    hub.commit("new");
    hub.mark("fail-start");
    let out = hub.run("update", None, false);
    assert_eq!(hub.installed_build(), old);
    assert_eq!(hub.lines("holder"), "42\n");
    assert!(text(&out).contains("The previous build is running again"));
    assert!(!hub.builds().join("update-from").exists());
    assert!(hub.lines("actions").ends_with("old|install\n"));
}

#[test]
fn failed_start_with_an_incompatible_schema_stays_stopped_for_recovery() {
    let hub = Hub::new();
    hub.install();
    let old = hub.installed_build();
    hub.commit("new");
    hub.mark("fail-start");
    hub.mark("change-schema");
    let out = hub.run("update", None, false);
    assert_eq!(hub.installed_build(), old);
    assert!(!hub.root().join("loaded").exists());
    assert!(hub.builds().join("update-from").exists());
    assert!(text(&out).contains("cannot check the live data"));
    hub.run("restart", None, false);
    fs::remove_file(hub.root().join("new-schema")).unwrap();
    hub.run("recover", None, true);
    assert_eq!(hub.lines("holder"), "42\n");
    assert!(!hub.builds().join("update-from").exists());
}

#[test]
fn update_keeps_only_three_complete_backup_generations() {
    let hub = Hub::new();
    hub.install();
    let backups = hub.builds().parent().unwrap().join("backups");
    for (stamp, complete) in [
        ("2000", true),
        ("2001", true),
        ("2002", true),
        ("2003", false),
    ] {
        let path = backups.join(format!("branvil-update-{stamp}"));
        fs::create_dir_all(&path).unwrap();
        if complete {
            fs::write(path.join("manifest.json"), "").unwrap();
        }
    }
    hub.commit("new");
    hub.run("update", None, true);
    assert!(!backups.join("branvil-update-2000").exists());
    for stamp in ["2001", "2002", "2003"] {
        assert!(backups.join(format!("branvil-update-{stamp}")).exists());
    }
    assert_eq!(fs::read_dir(backups).unwrap().count(), 4);
}

#[test]
fn uninstall_drains_and_removes_the_agent() {
    let hub = Hub::new();
    hub.install();
    hub.run("uninstall", None, true);
    assert!(!hub.plist().exists());
    assert!(!hub.root().join("loaded").exists());
    in_order(
        &hub.lines("calls"),
        &["current|server|stop", "launchctl|bootout|"],
    );
}

#[test]
fn missing_agent_names_the_install_command() {
    let hub = Hub::new();
    for action in ["update", "restart"] {
        assert!(text(&hub.run(action, None, false)).contains("Run:"));
    }
}

#[test]
fn only_macos_is_supported() {
    let hub = Hub::new();
    fs::write(hub.root().join("os"), "Linux\n").unwrap();
    assert!(text(&hub.run("install", None, false)).contains("macOS"));
    assert!(!hub.plist().exists());
}
