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
const IDENTITY: &str = "0123ABCD";
const IDENTITIES: &str =
    "  1) 0123ABCD \"Apple Development: Fixture (TEAM)\"\n     1 valid identities found\n";

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
        // HOME reaches the real directory through a link, as a path may on macOS.
        let home = root.join("home link");
        let repo = root.join("checkout");
        let tools = root.join("tools");
        for directory in [
            &root.join("home with spaces"),
            &repo.join("scripts"),
            &tools,
            &root.join("branvil"),
        ] {
            fs::create_dir_all(directory).unwrap();
        }
        symlink(root.join("home with spaces"), &home).unwrap();
        fs::write(repo.join("scripts/hub-server.sh"), SCRIPT).unwrap();
        fs::write(root.join("os"), "Darwin\n").unwrap();
        fs::write(root.join("identities"), IDENTITIES).unwrap();
        let stub = tools.join("stub");
        fs::write(&stub, TOOLS).unwrap();
        fs::set_permissions(&stub, fs::Permissions::from_mode(0o755)).unwrap();
        for name in [
            "uname",
            "id",
            "launchctl",
            "npm",
            "cargo",
            "plutil",
            "security",
            "codesign",
        ] {
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
            .env("GIT_COMMITTER_EMAIL", "fixture@example.test")
            // Older than any build, so extracted sources must not keep this time.
            .env("GIT_AUTHOR_DATE", "2001-01-01T00:00:00Z")
            .env("GIT_COMMITTER_DATE", "2001-01-01T00:00:00Z");
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

    /// Runs `action` as the owner does: install is given the signing identity.
    fn run(&self, action: &str, port: Option<&str>, success: bool) -> Output {
        let mut env = vec![];
        if let Some(port) = port {
            env.push(("GUILDBOTICS_SERVER_PORT", port));
        }
        if action == "install" {
            env.push(("GUILDBOTICS_HUB_SIGNING_IDENTITY", IDENTITY));
        }
        self.run_with(action, &env, success)
    }

    fn run_with(&self, action: &str, env: &[(&str, &str)], success: bool) -> Output {
        for file in ["calls", "actions"] {
            fs::write(self.root().join(file), "").unwrap();
        }
        let result = self
            .command("sh")
            .envs(env.iter().copied())
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

    /// Makes launchd's start of `build` fail.
    fn fail_start(&self, build: &str) {
        fs::write(self.root().join("fail-start"), build).unwrap();
    }

    /// The build the running copy names as its source.
    fn installed_build(&self) -> PathBuf {
        let program = fs::read_to_string(self.builds().join("current")).unwrap();
        let source = program
            .lines()
            .find_map(|line| line.strip_prefix("# source: "))
            .unwrap();
        Path::new(source).join("guildbotics-bin")
    }

    /// The executable launchd runs: a copy of the active build signed by `identity`.
    fn assert_runs_signed_by(&self, build: &str, identity: &str) {
        let current = self.builds().join("current");
        assert!(!current.is_symlink());
        let program = fs::read_to_string(current).unwrap();
        assert!(
            program.contains(&format!("# build: {build}\n")),
            "{program}"
        );
        assert!(program.contains(&format!("# signed: {identity} local.guildbotics\n")));
    }

    fn assert_runs_signed(&self, build: &str) {
        self.assert_runs_signed_by(build, IDENTITY);
    }

    /// The update stopped before switching: the old build still runs.
    fn assert_still_runs_old(&self, old: &Path) {
        assert_eq!(self.installed_build(), old);
        self.assert_runs_signed("old");
        assert_eq!(self.lines("holder"), "42\n");
        assert!(!self.lines("calls").contains("current|server|stop"));
        assert!(!self.builds().join("update-from").exists());
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

fn assert_startup_guidance(out: &Output) {
    let output = text(out);
    assert!(output.contains("Keychain access"));
    assert!(output.contains("Documents folder access"));
    assert!(output.contains("docs/hub-server.md"));
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
        "<key>ExitTimeOut</key><integer>91</integer>".into(),
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
    in_order(
        &hub.lines("calls"),
        &[
            "security|find-identity|-v|-p|codesigning",
            "npm|ci",
            &format!("codesign|--force|-s|{IDENTITY}|-i|local.guildbotics|"),
            "launchctl|bootstrap|",
        ],
    );
    assert!(hub.lines("actions").contains("old|install\n"));
    assert!(text(&out).contains(log.to_str().unwrap()));
    assert_startup_guidance(&out);
    assert_eq!(
        fs::read_to_string(hub.installed_build().parent().unwrap().join("build-id")).unwrap(),
        "old"
    );
    hub.assert_runs_signed("old");
    // Cargo rebuilds a source tree only when it is newer than the last build.
    let extracted = fs::metadata(hub.installed_build().parent().unwrap().join("build-id"))
        .unwrap()
        .modified()
        .unwrap();
    assert!(extracted > std::time::SystemTime::now() - std::time::Duration::from_secs(3600));
    assert_eq!(
        fs::read_to_string(hub.builds().parent().unwrap().join("signing-identity")).unwrap(),
        format!("{IDENTITY}\n")
    );
}

#[test]
fn install_needs_a_signing_identity_before_building() {
    let hub = Hub::new();
    let out = hub.run_with("install", &[], false);
    assert!(text(&out).contains("GUILDBOTICS_HUB_SIGNING_IDENTITY"));
    assert!(!hub.lines("calls").contains("npm|"));
    assert!(!hub.plist().exists());
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
    // The reused build is signed again with the identity given now.
    let renewed = "Apple Development: Renewed (TEAM)";
    fs::write(
        hub.root().join("identities"),
        format!("  1) 4567CDEF \"{renewed}\"\n{IDENTITIES}"),
    )
    .unwrap();
    hub.run_with(
        "install",
        &[("GUILDBOTICS_HUB_SIGNING_IDENTITY", renewed)],
        true,
    );
    in_order(
        &hub.lines("calls"),
        &[
            "launchctl|bootout|",
            "launchctl|enable|",
            "launchctl|bootstrap|",
        ],
    );
    assert!(!hub.lines("calls").contains("npm|"));
    hub.assert_runs_signed_by("old", renewed);
}

#[test]
fn install_leaves_a_different_installed_build_to_update() {
    let hub = Hub::new();
    hub.install();
    let old = hub.installed_build();
    fs::remove_file(hub.root().join("holder")).unwrap();
    fs::remove_file(hub.root().join("job-pid")).unwrap();
    hub.commit("new");
    let out = hub.run("install", None, false);
    assert!(text(&out).contains("A different build is installed. Run:"));
    assert_eq!(hub.installed_build(), old);
    assert!(!hub.lines("calls").contains("launchctl|bootstrap"));
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
            "npm|run|build|--workspace|desktop|--workspace|crates/branvil/web",
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
    // The identity install recorded signs the new build; macOS does not ask again.
    in_order(
        &hub.lines("calls"),
        &[
            "security|find-identity|",
            "cargo|build|",
            &format!("codesign|--force|-s|{IDENTITY}|-i|local.guildbotics|"),
            "current|branvil|backup|",
        ],
    );
    hub.assert_runs_signed("new");
    assert!(!text(&out).contains("Keychain access"));
    let plist = fs::read_to_string(hub.plist()).unwrap();
    assert!(plist.contains("<key>ExitTimeOut</key><integer>92</integer>"));
    assert!(plist.contains(&format!("<key>PATH</key><string>{}</string>", hub.path)));
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
fn an_identity_missing_from_the_keychain_stops_the_update_before_building() {
    let hub = Hub::new();
    hub.install();
    let old = hub.installed_build();
    hub.commit("new");
    // Only another certificate is valid; its name merely contains the identity.
    fs::write(
        hub.root().join("identities"),
        format!("  1) 4567CDEF \"Apple Development: {IDENTITY} (TEAM)\"\n"),
    )
    .unwrap();
    let out = hub.run("update", None, false);
    assert!(text(&out).contains("not a valid code signing identity"));
    assert!(!hub.lines("calls").contains("npm|"));
    hub.assert_still_runs_old(&old);
}

#[test]
fn a_signature_without_a_team_id_is_never_installed() {
    let hub = Hub::new();
    hub.mark("no-team");
    let out = hub.run("install", None, false);
    assert!(text(&out).contains("has no Team ID"));
    assert!(!hub.plist().exists());
    assert!(!hub.builds().join("current").exists());

    fs::remove_file(hub.root().join("no-team")).unwrap();
    hub.install();
    let old = hub.installed_build();
    hub.commit("new");
    hub.mark("no-team");
    let out = hub.run("update", None, false);
    assert!(text(&out).contains("has no Team ID"));
    hub.assert_still_runs_old(&old);
}

#[test]
fn an_update_keeps_the_team_the_running_build_was_signed_for() {
    let hub = Hub::new();
    hub.install();
    let old = hub.installed_build();
    hub.commit("new");
    fs::write(hub.root().join("team"), "OTHERTEAM1").unwrap();
    let out = hub.run("update", None, false);
    assert!(text(&out).contains("the running build is signed for FIXTURE123"));
    hub.assert_still_runs_old(&old);

    // A running copy whose Team ID cannot be read is not taken as any team.
    let current = hub.builds().join("current");
    let unsigned = fs::read_to_string(&current)
        .unwrap()
        .replace("# team: FIXTURE123\n", "");
    fs::write(&current, unsigned).unwrap();
    let out = hub.run("update", None, false);
    assert!(text(&out).contains("the running build is signed for ."));
    assert_eq!(hub.installed_build(), old);

    // Changing teams takes a new install, which macOS asks about once.
    hub.run("uninstall", None, true);
    hub.install();
    let program = fs::read_to_string(hub.builds().join("current")).unwrap();
    assert!(program.contains("# build: new\n"));
    assert!(program.contains("# team: OTHERTEAM1\n"));
}

#[test]
fn a_failed_signing_stops_the_update_before_switching() {
    let hub = Hub::new();
    hub.install();
    let old = hub.installed_build();
    hub.commit("new");
    hub.mark("fail-sign");
    hub.run("update", None, false);
    hub.assert_still_runs_old(&old);
    let failure = fs::read_to_string(hub.builds().parent().unwrap().join("last-failure")).unwrap();
    assert!(failure.contains("during signing"));
}

#[test]
fn update_needs_the_identity_recorded_by_install() {
    let hub = Hub::new();
    hub.install();
    let old = hub.installed_build();
    fs::remove_file(hub.builds().parent().unwrap().join("signing-identity")).unwrap();
    hub.commit("new");
    let out = hub.run("update", None, false);
    assert!(
        text(&out).contains("uninstall, then install again with GUILDBOTICS_HUB_SIGNING_IDENTITY")
    );
    assert!(!hub.lines("calls").contains("npm|"));
    hub.assert_still_runs_old(&old);
}

#[test]
fn failed_start_returns_to_the_previous_compatible_build() {
    let hub = Hub::new();
    hub.install();
    let old = hub.installed_build();
    hub.commit("new");
    hub.fail_start("new");
    let out = hub.run("update", None, false);
    let plist = fs::read_to_string(hub.plist()).unwrap();
    assert!(plist.contains("<key>ExitTimeOut</key><integer>91</integer>"));
    assert_eq!(hub.installed_build(), old);
    hub.assert_runs_signed("old");
    assert_eq!(hub.lines("holder"), "42\n");
    assert!(text(&out).contains("The previous build is running again"));
    assert!(!hub.builds().join("update-from").exists());
    // The old build was the last to start; the failure record then names it.
    assert!(
        hub.lines("actions")
            .ends_with("old|server|hub-probe|--ready\nold|server|hub-probe|--source\n")
    );
    assert!(
        fs::read_to_string(hub.builds().parent().unwrap().join("last-failure"))
            .unwrap()
            .contains(&format!(
                "active revision: {}",
                old.parent().unwrap().file_name().unwrap().to_str().unwrap()
            ))
    );
    assert!(!hub.lines("actions").contains("|install\n"));
}

#[test]
fn failed_start_with_an_incompatible_schema_stays_stopped_for_recovery() {
    let hub = Hub::new();
    hub.install();
    let old = hub.installed_build();
    hub.commit("new");
    hub.fail_start("new");
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
fn asset_install_failure_keeps_the_activated_build_and_can_be_retried() {
    let hub = Hub::new();
    hub.install();
    let old = hub.installed_build();
    hub.commit("new");
    hub.mark("change-schema");
    hub.mark("fail-install");
    let out = hub.run("update", None, false);
    let new = hub.installed_build();
    assert_ne!(new, old);
    assert_eq!(hub.lines("holder"), "42\n");
    assert!(hub.root().join("loaded").exists());
    assert!(!hub.builds().join("update-from").exists());
    assert!(text(&out).contains("installing the CLI and skill"));
    let failure = fs::read_to_string(hub.builds().parent().unwrap().join("last-failure")).unwrap();
    assert!(failure.contains("installing the CLI and skill"));
    assert!(failure.contains(new.parent().unwrap().file_name().unwrap().to_str().unwrap()));
    assert!(failure.contains("Z: Hub update failed"));
    assert_eq!(hub.lines("actions").matches("old|server|stop").count(), 1);
    assert!(!hub.lines("actions").contains("new|server|stop"));

    fs::remove_file(hub.root().join("fail-install")).unwrap();
    hub.run("update", None, true);
    assert_eq!(hub.installed_build(), new);
    assert!(hub.lines("actions").contains("new|install"));
    assert!(!hub.lines("calls").contains("launchctl|bootstrap"));
}

#[test]
fn recover_keeps_a_ready_build_after_an_interrupted_activation() {
    let hub = Hub::new();
    hub.install();
    let running = fs::read(hub.builds().join("current")).unwrap();
    hub.commit("new");
    hub.mark("change-schema");
    hub.run("update", None, true);
    let new = hub.installed_build();
    // Stopped after the switch: the copy that ran before is still kept.
    let pending = hub.builds().join("update-from");
    fs::write(&pending, running).unwrap();
    fs::set_permissions(&pending, fs::Permissions::from_mode(0o755)).unwrap();

    hub.run("recover", None, true);
    assert_eq!(hub.installed_build(), new);
    assert!(!hub.builds().join("update-from").exists());
    assert!(!hub.lines("actions").contains("server|stop"));
    assert!(hub.lines("actions").contains("new|install"));

    // A later failed update returns to the build that ran, which reads the data.
    hub.commit("newer");
    hub.fail_start("newer");
    let out = hub.run("update", None, false);
    assert!(text(&out).contains("The previous build is running again"));
    assert_eq!(hub.installed_build(), new);
    assert_eq!(hub.lines("holder"), "42\n");
}

#[test]
fn a_rollback_restores_the_copy_that_ran_whatever_happened_to_its_build() {
    let hub = Hub::new();
    hub.install();
    // A refused reinstall for another team signs the stored build again.
    let other = "Apple Development: Other (TEAM)";
    fs::write(
        hub.root().join("identities"),
        format!("  1) 4567CDEF \"{other}\"\n{IDENTITIES}"),
    )
    .unwrap();
    fs::write(hub.root().join("team"), "OTHERTEAM1").unwrap();
    let out = hub.run_with(
        "install",
        &[("GUILDBOTICS_HUB_SIGNING_IDENTITY", other)],
        false,
    );
    assert!(text(&out).contains("A GuildBotics server is running"));
    fs::remove_file(hub.root().join("team")).unwrap();

    hub.commit("new");
    hub.fail_start("new");
    let out = hub.run("update", None, false);
    assert!(text(&out).contains("The previous build is running again"));
    hub.assert_runs_signed("old");
    assert!(
        fs::read_to_string(hub.builds().join("current"))
            .unwrap()
            .contains("# team: FIXTURE123\n")
    );
}

#[test]
fn a_candidate_that_becomes_ready_as_launchd_reports_an_exit_stays_running() {
    let hub = Hub::new();
    hub.install();
    let old = hub.installed_build();
    hub.commit("new");
    hub.mark("change-schema");
    hub.fail_start("new");
    hub.mark("ready-after-exit");
    hub.run("update", None, true);
    assert_ne!(hub.installed_build(), old);
    assert_eq!(hub.lines("holder"), "42\n");
    assert!(!hub.builds().join("update-from").exists());
    assert!(!hub.lines("actions").contains("new|server|stop"));
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
