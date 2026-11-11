//! Exercise the Hub script with a real Git source tree and stubbed OS tools.
#![cfg(unix)]

use std::{
    fs,
    os::unix::fs::{PermissionsExt, symlink},
    path::{Path, PathBuf},
    process::{Command, Output},
};

const SCRIPT: &str = include_str!("../../../scripts/hub-server.sh");
const AUTO: &str = include_str!("../../../scripts/hub-auto-update.mjs");
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
        fs::write(repo.join("scripts/hub-auto-update.mjs"), AUTO).unwrap();
        fs::write(
            repo.join("hub-update.json"),
            "{\"manual_update_version\":0,\"instructions\":null}",
        )
        .unwrap();
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
            "rustc",
        ] {
            symlink(&stub, tools.join(name)).unwrap();
        }
        // The loop runs on the Node.js that runs the tests, wherever it is installed.
        let node = std::env::split_paths(&std::env::var_os("PATH").unwrap())
            .map(|directory| directory.join("node"))
            .find(|node| node.is_file())
            .expect("Node.js must be in PATH");
        symlink(node, tools.join("node")).unwrap();
        let path = format!("{}:/usr/local/bin:/usr/bin:/bin", tools.display());
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
        self.git(&["add", "scripts", "build-id", "hub-update.json"]);
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

    /// The revision directories that keep a build's source tree.
    fn source_trees(&self) -> Vec<String> {
        let mut trees: Vec<_> = fs::read_dir(self.builds())
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .filter(|path| path.is_dir() && !path.is_symlink())
            .map(|path| path.file_name().unwrap().to_string_lossy().into_owned())
            .collect();
        trees.sort();
        trees
    }

    fn installed_tree(&self) -> String {
        let build = self.installed_build();
        build
            .parent()
            .unwrap()
            .file_name()
            .unwrap()
            .to_string_lossy()
            .into_owned()
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
fn every_unload_waits_until_launchd_no_longer_lists_the_agent() {
    let hub = Hub::new();
    // An install whose start fails leaves no listed job behind.
    hub.mark("slow-bootout");
    hub.fail_start("old");
    hub.run("install", None, false);
    assert!(!hub.root().join("loaded").exists());
    fs::remove_file(hub.root().join("slow-bootout")).unwrap();
    fs::remove_file(hub.root().join("fail-start")).unwrap();
    hub.install();
    let old = hub.installed_build();
    // launchd keeps listing the job briefly after bootout returns.
    hub.mark("slow-bootout");
    hub.run("restart", None, true);
    assert_eq!(hub.lines("holder"), "42\n");

    hub.commit("new");
    hub.fail_start("new");
    let out = hub.run("update", None, false);
    assert!(text(&out).contains("The previous build is running again"));
    assert_eq!(hub.installed_build(), old);

    hub.run("uninstall", None, true);
    assert!(!hub.root().join("loaded").exists());
    fs::remove_file(hub.root().join("fail-start")).unwrap();
    hub.install();
    assert_eq!(hub.lines("holder"), "42\n");

    // A reinstall replaces the stopped agent that launchd still lists.
    fs::remove_file(hub.root().join("holder")).unwrap();
    fs::remove_file(hub.root().join("job-pid")).unwrap();
    hub.install();
    assert_eq!(hub.lines("holder"), "42\n");
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
            "guildbotics-bin|server|hub-data|backup|--to|",
            "current|branvil|restore|",
            "guildbotics-bin|branvil|admin|upgrade|",
            "guildbotics-bin|branvil|admin|check|",
            "guildbotics-bin|server|hub-data|upgrade|--data-dir|",
            "guildbotics-bin|server|hub-data|check|--data-dir|",
            "current|server|stop",
            "launchctl|bootout|",
            "guildbotics-bin|branvil|admin|upgrade|",
            "guildbotics-bin|branvil|admin|check|",
            "guildbotics-bin|server|hub-data|upgrade\n",
            "guildbotics-bin|server|hub-data|check\n",
            "launchctl|bootstrap|",
            "current|install",
            "current|environment|prune",
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
    assert!(!hub.builds().join("update-from").exists());
}

#[test]
fn an_update_removes_builds_that_no_process_started_before_it_runs() {
    let hub = Hub::new();
    hub.install();
    let old = hub.installed_tree();
    // An interrupted build leaves its source tree behind.
    fs::create_dir_all(hub.builds().join("interrupted/node_modules")).unwrap();
    let elsewhere = hub.root().join("elsewhere");
    fs::create_dir(&elsewhere).unwrap();
    symlink(&elsewhere, hub.builds().join("linked")).unwrap();
    hub.commit("new");
    hub.fail_start("new");
    hub.run("update", None, false);
    // A rollback runs the previous build, from its source tree.
    assert!(hub.source_trees().contains(&old));

    fs::remove_file(hub.root().join("fail-start")).unwrap();
    hub.run("update", None, true);
    let new = hub.installed_tree();
    // A process started before this update still runs the old build.
    let mut kept = vec![old.clone(), new.clone()];
    kept.sort();
    assert_eq!(hub.source_trees(), kept);
    assert!(elsewhere.exists());

    hub.commit("newer");
    hub.run("update", None, true);
    let mut kept = vec![new, hub.installed_tree()];
    kept.sort();
    assert_eq!(hub.source_trees(), kept);
    assert!(hub.builds().join("current").exists());
}

#[test]
fn an_update_keeps_the_build_the_installed_cli_runs() {
    let hub = Hub::new();
    hub.install();
    let old = hub.installed_tree();
    hub.mark("fail-install");
    hub.commit("new");
    // Every new build fails to install the CLI, which stays the old build.
    hub.run("update", None, false);
    let new = hub.installed_tree();
    hub.commit("newer");
    hub.run("update", None, false);
    // The server ran new until this update, and the CLI still runs old.
    let mut kept = vec![old, new, hub.installed_tree()];
    kept.sort();
    assert_eq!(hub.source_trees(), kept);
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
    assert!(!hub.lines("actions").contains("|environment|prune\n"));
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
    assert!(hub.lines("actions").contains("new|environment|prune"));
    let failure = fs::read_to_string(hub.builds().parent().unwrap().join("last-failure")).unwrap();
    assert!(failure.contains("installing the CLI and skill"));
    assert!(failure.contains(new.parent().unwrap().file_name().unwrap().to_str().unwrap()));
    assert!(failure.contains("Z: Hub update failed"));
    assert_eq!(hub.lines("actions").matches("old|server|stop").count(), 1);
    assert!(!hub.lines("actions").contains("new|server|stop"));
    // The CLI that was installed still runs the old build, from its source tree.
    assert!(old.exists());
    assert!(text(&out).contains(&format!(
        "Retry CLI and skill installation with: GUILDBOTICS_HOME='{}' BRANVIL_DATA_DIR='{}' '{}' install",
        hub.home.join(".guildbotics").display(),
        hub.root().join("branvil").display(),
        hub.builds().join("current").display()
    )));

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
    for stamp in ["2001", "2002"] {
        assert!(backups.join(format!("branvil-update-{stamp}")).exists());
    }
    assert!(!backups.join("branvil-update-2003").exists());
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

impl Hub {
    fn auto_candidate(&self) {
        self.git(&["branch", "-M", "main"]);
        let remote = self.root().join("origin.git");
        self.git(&["clone", "--bare", ".", remote.to_str().unwrap()]);
        self.git(&["remote", "add", "origin", remote.to_str().unwrap()]);
        self.commit("new");
        self.git(&["push", "origin", "main"]);
        self.git(&["reset", "--hard", "HEAD^"]);
    }

    fn auto_state(&self) -> serde_json::Value {
        serde_json::from_str(
            &fs::read_to_string(self.builds().parent().unwrap().join("auto-update.json")).unwrap(),
        )
        .unwrap()
    }
}

#[test]
fn auto_update_requires_successful_checks_and_notifies_failure_once() {
    for checks in ["pending", "unregistered", "failure", "success"] {
        let hub = Hub::new();
        hub.install();
        hub.auto_candidate();
        fs::write(hub.root().join("checks"), checks).unwrap();
        hub.run("auto-once", None, true);
        if checks == "success" {
            hub.assert_runs_signed("new");
        } else {
            hub.assert_runs_signed("old");
            hub.run("auto-once", None, true);
            if checks == "failure" {
                assert_eq!(hub.lines("notifications").lines().count(), 1);
                // A rerun on the same commit can turn a failed check green.
                fs::write(hub.root().join("checks"), "success").unwrap();
                hub.run("auto-once", None, true);
                hub.assert_runs_signed("new");
            } else {
                assert!(!hub.root().join("notifications").exists());
            }
        }
    }
}

#[test]
fn auto_update_resumes_only_a_previously_running_service_even_after_rollback() {
    for running in [true, false] {
        for rollback in [true, false] {
            let hub = Hub::new();
            hub.install();
            hub.auto_candidate();
            hub.mark("service-autostart");
            if running {
                hub.mark("service-running");
            }
            if rollback {
                hub.fail_start("new");
            }
            hub.run("auto-once", None, true);
            assert_eq!(hub.root().join("service-running").exists(), running);
            assert_eq!(
                hub.auto_state()["result"],
                if rollback { "failed" } else { "success" }
            );
            if rollback {
                hub.assert_runs_signed("old");
                fs::remove_file(hub.root().join("fail-start")).unwrap();
                hub.run("auto-once", None, true);
                assert!(!hub.lines("calls").contains("npm|"));
                hub.assert_runs_signed("old");
                assert_eq!(hub.lines("notifications").lines().count(), 1);
            } else {
                hub.assert_runs_signed("new");
            }
        }
    }
}

#[test]
fn auto_update_defers_unsafe_states_and_reports_actionable_blockers() {
    for condition in [
        "operation",
        "interrupted",
        "dirty",
        "diverged",
        "unresponsive",
        "service-error",
        "image",
        "manual",
    ] {
        let hub = Hub::new();
        hub.install();
        hub.auto_candidate();
        match condition {
            "operation" => {
                fs::create_dir(hub.builds().parent().unwrap().join("operation")).unwrap()
            }
            "interrupted" => fs::write(hub.builds().join("update-from"), "interrupted").unwrap(),
            "dirty" => fs::write(hub.repo.join("untracked"), "change").unwrap(),
            "diverged" => hub.commit("local"),
            "unresponsive" | "service-error" => hub.mark(condition),
            "image" => fs::write(
                hub.root().join("environment"),
                r#"{"image":{"reference":"custom:latest","present":false}}"#,
            )
            .unwrap(),
            "manual" => {
                hub.git(&["merge", "--ff-only", "origin/main"]);
                fs::write(
                    hub.repo.join("hub-update.json"),
                    r#"{"manual_update_version":1,"instructions":"docs/hub-server.md"}"#,
                )
                .unwrap();
                hub.commit("manual");
                hub.git(&["push", "origin", "main"]);
                hub.git(&["reset", "--hard", "HEAD~2"]);
            }
            _ => unreachable!(),
        }
        hub.run("auto-once", None, true);
        hub.assert_runs_signed("old");
        assert!(!hub.lines("actions").contains("server|stop"));
        if condition != "operation" {
            assert!(!hub.lines("notifications").is_empty());
            hub.run("auto-once", None, true);
            assert_eq!(hub.lines("notifications").lines().count(), 1);
        }
    }
}

#[test]
fn auto_update_reports_missing_snapshot_after_activation() {
    for snapshot in ["missing", "stale", "failed", "building", "ready"] {
        let hub = Hub::new();
        hub.install();
        hub.auto_candidate();
        if snapshot != "ready" {
            hub.mark("fail-prune");
        }
        fs::write(
            hub.root().join("environment"),
            format!(r#"{{"image":{{"reference":null}},"snapshot":{{"state":"{snapshot}"}}}}"#),
        )
        .unwrap();
        hub.run("auto-once", None, true);
        hub.assert_runs_signed("new");
        assert_eq!(
            hub.root().join("notifications").exists(),
            snapshot != "ready"
        );
        assert!(hub.lines("actions").contains("new|install"));
        if snapshot != "ready" {
            assert!(hub.lines("notifications").contains("environment prune"));
            assert!(
                !hub.lines("notifications")
                    .contains("a new commit is required")
            );
        }
    }
}

#[test]
fn recover_starts_the_saved_candidate_when_live_data_has_already_been_upgraded() {
    let hub = Hub::new();
    hub.install();
    let old = fs::read(hub.builds().join("current")).unwrap();
    hub.commit("new");
    hub.mark("change-schema");
    hub.run("update", None, true);
    let new = fs::read(hub.builds().join("current")).unwrap();
    for name in ["holder", "job-pid", "loaded"] {
        fs::remove_file(hub.root().join(name)).unwrap();
    }
    // A reboot interrupted activation, leaving old current and upgraded data.
    for (name, bytes) in [
        ("current", old.clone()),
        ("update-from", old),
        ("update-to", new),
    ] {
        fs::write(hub.builds().join(name), bytes).unwrap();
        fs::set_permissions(hub.builds().join(name), fs::Permissions::from_mode(0o755)).unwrap();
    }
    hub.run("recover", None, true);
    hub.assert_runs_signed("new");
    assert!(!hub.builds().join("update-from").exists());
    assert!(!hub.builds().join("update-to").exists());
}

#[test]
fn auto_agent_registration_preserves_build_path_and_leaves_server_running() {
    let hub = Hub::new();
    hub.install();
    fs::write(
        hub.repo.join("rust-toolchain.toml"),
        include_str!("../../../rust-toolchain.toml"),
    )
    .unwrap();
    hub.mark("auto-loaded");
    hub.run("auto-install", None, true);
    assert!(
        hub.lines("calls").contains("sleep|1")
            || hub
                .lines("calls")
                .matches("launchctl|print|gui/1234/local.guildbotics.update")
                .count()
                >= 3
    );
    let plist = hub
        .home
        .join("Library/LaunchAgents/local.guildbotics.update.plist");
    let contents = fs::read_to_string(&plist).unwrap();
    assert!(contents.contains("local.guildbotics.update"));
    assert!(contents.contains("hub-auto-update.mjs"));
    assert!(contents.contains(&hub.path));
    assert_eq!(hub.lines("holder"), "42\n");
    hub.run("auto-uninstall", None, true);
    assert!(!plist.exists());
    assert_eq!(hub.lines("holder"), "42\n");
}

#[test]
fn a_hub_copy_runs_beside_the_hub_under_its_own_names() {
    let hub = Hub::new();
    let copy = [("GUILDBOTICS_HUB_INSTANCE", "copy")];
    let copy_home = hub.root().join("copy home");
    let state = hub
        .home
        .join("Library/Application Support/GuildBotics/Hub-copy");
    let agents = hub.home.join("Library/LaunchAgents");
    let logs = hub.home.join("Library/Logs");
    hub.run_with(
        "install",
        &[
            copy[0],
            ("GUILDBOTICS_HUB_SIGNING_IDENTITY", IDENTITY),
            ("GUILDBOTICS_HOME", copy_home.to_str().unwrap()),
        ],
        true,
    );
    let mut calls = hub.lines("calls");
    let plist = fs::read_to_string(agents.join("local.guildbotics.copy.plist")).unwrap();
    assert!(plist.contains(&format!(
        "<key>ProgramArguments</key><array><string>{}</string>",
        state.join("builds/current").display()
    )));
    assert!(plist.contains(&logs.join("guildbotics-copy.log").display().to_string()));
    in_order(
        &hub.lines("calls"),
        &[
            // The same program keeps the Hub's signing identifier.
            &format!("codesign|--force|-s|{IDENTITY}|-i|local.guildbotics|"),
            "launchctl|enable|gui/1234/local.guildbotics.copy",
            "launchctl|bootstrap|gui/1234|",
        ],
    );
    hub.auto_candidate();
    // Guidance runs the copy's build with the copy's settings.
    hub.mark("fail-prune");
    fs::write(
        hub.root().join("environment"),
        r#"{"image":{"reference":null},"snapshot":{"state":"missing"}}"#,
    )
    .unwrap();
    let out = hub.run_with("auto-once", &copy, true);
    calls += &hub.lines("calls");
    let guidance = format!(
        "GUILDBOTICS_HOME='{}' BRANVIL_DATA_DIR='{}' '{}' environment ",
        copy_home.display(),
        hub.root().join("branvil").display(),
        state.join("builds/current").display()
    );
    assert!(text(&out).contains(&format!("run: {guidance}prune")));
    assert!(
        hub.lines("notifications")
            .contains(&format!("run {guidance}build"))
    );
    assert!(state.join("auto-update.json").exists());
    assert!(
        fs::read_to_string(state.join("builds/current"))
            .unwrap()
            .contains("# build: new\n")
    );
    fs::write(
        hub.repo.join("rust-toolchain.toml"),
        include_str!("../../../rust-toolchain.toml"),
    )
    .unwrap();
    hub.run_with("auto-install", &copy, true);
    calls += &hub.lines("calls");
    let auto = fs::read_to_string(agents.join("local.guildbotics.copy.update.plist")).unwrap();
    assert!(auto.contains("<key>GUILDBOTICS_HUB_INSTANCE</key><string>copy</string>"));
    assert!(
        auto.contains(
            &logs
                .join("guildbotics-copy-update.log")
                .display()
                .to_string()
        )
    );
    assert!(calls.contains("launchctl|enable|gui/1234/local.guildbotics.copy.update"));
    assert!(calls.contains("launchctl|bootout|gui/1234/local.guildbotics.copy\n"));
    // Nothing of the Hub's own agents or state is touched.
    assert!(!hub.builds().exists());
    assert!(!hub.plist().exists());
    assert!(!agents.join("local.guildbotics.update.plist").exists());
    assert!(!calls.contains("gui/1234/local.guildbotics|"));
    assert!(!calls.contains("gui/1234/local.guildbotics\n"));
    assert!(!calls.contains("gui/1234/local.guildbotics.update"));

    // Guidance for the copy names it, so that pasting it leaves the Hub alone.
    fs::create_dir(state.join("operation")).unwrap();
    hub.run_with("auto-once", &copy, true);
    fs::remove_dir(state.join("operation")).unwrap();
    fs::write(state.join("builds/update-from"), "interrupted").unwrap();
    hub.run_with("auto-once", &copy, true);
    assert!(
        hub.lines("notifications")
            .contains("Hub copy copy (GUILDBOTICS_HUB_INSTANCE=copy) ")
    );
    let out = hub.run_with("restart", &copy, false);
    assert!(
        text(&out).contains("Run: GUILDBOTICS_HUB_INSTANCE=copy scripts/hub-server.sh recover")
    );

    // "update" would be the Hub's own automatic update agent.
    for name in ["Copy/1", "update"] {
        let out = hub.run_with("restart", &[("GUILDBOTICS_HUB_INSTANCE", name)], false);
        assert!(text(&out).contains("GUILDBOTICS_HUB_INSTANCE must be"));
        assert!(!hub.lines("calls").contains("launchctl"));
    }
}

#[test]
fn auto_update_uses_the_agents_identity_root_instead_of_the_shells() {
    for installed in [Some("identity"), None] {
        let hub = Hub::new();
        let identity = installed.map(|name| hub.root().join(name).display().to_string());
        let mut env = vec![("GUILDBOTICS_HUB_SIGNING_IDENTITY", IDENTITY)];
        if let Some(identity) = &identity {
            env.push(("GUILDBOTICS_IDENTITY_ROOT", identity));
        }
        hub.run_with("install", &env, true);
        hub.auto_candidate();
        fs::write(hub.root().join("checks"), "failure").unwrap();
        hub.run_with(
            "auto-once",
            &[("GUILDBOTICS_IDENTITY_ROOT", "/unrelated/identity")],
            true,
        );
        assert_eq!(
            hub.lines("notify-identity-root"),
            format!("{}\n", identity.unwrap_or_default())
        );
    }
}

#[test]
fn auto_registration_refuses_every_operation_before_bootout() {
    for action in ["auto-install", "auto-uninstall"] {
        let hub = Hub::new();
        hub.install();
        let lock = hub.builds().parent().unwrap().join("operation");
        fs::create_dir(&lock).unwrap();
        let out = hub.run(action, None, false);
        assert!(text(&out).contains("Another Hub operation"));
        assert!(!hub.lines("calls").contains("launchctl|bootout"));
        assert!(lock.exists());
        assert_eq!(hub.lines("holder"), "42\n");
    }
}

#[test]
fn auto_state_notifications_follow_each_episode_instead_of_the_commit() {
    for stage in ["operation", "recovery", "preflight", "service"] {
        let hub = Hub::new();
        hub.install();
        hub.auto_candidate();
        fs::write(hub.root().join("checks"), "pending").unwrap();
        for episode in 1..=2 {
            let file = match stage {
                "operation" => hub.builds().parent().unwrap().join("operation"),
                "recovery" => hub.builds().join("update-from"),
                "preflight" => hub.root().join("origin.git"),
                _ => hub.root().join("unresponsive"),
            };
            match stage {
                "operation" => fs::create_dir(&file).unwrap(),
                "preflight" => fs::rename(&file, hub.root().join("offline.git")).unwrap(),
                _ => fs::write(&file, "").unwrap(),
            }
            if stage == "service" {
                fs::write(hub.root().join("checks"), "success").unwrap();
            }
            hub.run("auto-once", None, true);
            if stage == "operation" {
                // Ordinary short operations do not notify, and a new lock has a fresh delay.
                assert_eq!(hub.root().join("notifications").exists(), episode > 1);
                let mut state = hub.auto_state();
                state["blockers"][stage]["since"] = serde_json::json!(0);
                fs::write(
                    hub.builds().parent().unwrap().join("auto-update.json"),
                    state.to_string(),
                )
                .unwrap();
                hub.run("auto-once", None, true);
            }
            assert_eq!(hub.lines("notifications").lines().count(), episode);
            hub.run("auto-once", None, true);
            assert_eq!(hub.lines("notifications").lines().count(), episode);
            match stage {
                "operation" => fs::remove_dir(&file).unwrap(),
                "preflight" => fs::rename(hub.root().join("offline.git"), &file).unwrap(),
                _ => fs::remove_file(&file).unwrap(),
            }
            if ["operation", "recovery"].contains(&stage) {
                continue;
            }
            // Allow service status to succeed without actually running update.
            if stage == "service" {
                hub.mark("fail-sign");
            }
            hub.run("auto-once", None, true);
            if stage == "service" {
                // A failed update would suppress the next status read; clear only test attempt.
                let mut state = hub.auto_state();
                state["attempt"] = serde_json::Value::Null;
                fs::write(
                    hub.builds().parent().unwrap().join("auto-update.json"),
                    state.to_string(),
                )
                .unwrap();
                fs::remove_file(hub.root().join("fail-sign")).unwrap();
                // Keep only the service notices in this test's observation.
                let notices = hub.lines("notifications");
                fs::write(
                    hub.root().join("notifications"),
                    notices
                        .lines()
                        .filter(|line| line.contains(": service:"))
                        .map(|line| format!("{line}\n"))
                        .collect::<String>(),
                )
                .unwrap();
            }
        }
    }
}

#[test]
fn auto_failed_update_notification_contains_its_stage_and_recovery_requirement() {
    let hub = Hub::new();
    hub.install();
    hub.auto_candidate();
    hub.fail_start("new");
    hub.mark("change-schema");
    hub.run("auto-once", None, true);
    let notice = hub.lines("notifications");
    assert!(notice.contains("activating the new build"));
    assert!(notice.contains("recovery required"));
}

#[test]
fn auto_retries_unsent_notifications_even_after_the_build_is_current() {
    let hub = Hub::new();
    hub.install();
    hub.auto_candidate();
    fs::write(
        hub.root().join("environment"),
        r#"{"image":{"reference":null},"snapshot":{"state":"missing"}}"#,
    )
    .unwrap();
    hub.mark("fail-notify");
    hub.run("auto-once", None, true);
    hub.assert_runs_signed("new");
    assert!(!hub.root().join("notifications").exists());
    fs::remove_file(hub.root().join("fail-notify")).unwrap();
    hub.run("auto-once", None, true);
    assert!(hub.lines("notifications").contains("Snapshot is missing"));
    hub.run("auto-once", None, true);
    assert_eq!(hub.lines("notifications").lines().count(), 1);
}

#[test]
fn auto_invalid_migration_is_one_failure_episode() {
    let hub = Hub::new();
    hub.install();
    hub.auto_candidate();
    hub.git(&["merge", "--ff-only", "origin/main"]);
    fs::write(
        hub.repo.join("hub-update.json"),
        r#"{"manual_update_version":"invalid"}"#,
    )
    .unwrap();
    hub.commit("invalid-migration");
    hub.git(&["push", "origin", "main"]);
    hub.git(&["reset", "--hard", "HEAD~2"]);
    for _ in 0..2 {
        hub.run("auto-once", None, true);
    }
    assert_eq!(hub.lines("notifications").lines().count(), 1);
    hub.assert_runs_signed("old");
}

#[test]
fn cleanup_failure_keeps_the_activated_build_and_never_rolls_back() {
    for recovery in [0, 1, 2] {
        let hub = Hub::new();
        hub.install();
        let old = fs::read(hub.builds().join("current")).unwrap();
        hub.commit("new");
        if recovery != 0 {
            hub.run("update", None, true);
            if recovery == 2 {
                let new = fs::read(hub.builds().join("current")).unwrap();
                for name in ["holder", "job-pid", "loaded"] {
                    fs::remove_file(hub.root().join(name)).unwrap();
                }
                for (name, bytes) in [("current", old.clone()), ("update-to", new)] {
                    fs::write(hub.builds().join(name), bytes).unwrap();
                    fs::set_permissions(hub.builds().join(name), fs::Permissions::from_mode(0o755))
                        .unwrap();
                }
            }
            fs::write(hub.builds().join("update-from"), old).unwrap();
        }
        hub.mark("fail-prune");
        let out = hub.run(if recovery != 0 { "recover" } else { "update" }, None, true);
        hub.assert_runs_signed("new");
        assert!(!hub.builds().join("update-from").exists());
        in_order(
            &hub.lines("actions"),
            &["new|install", "new|environment|prune"],
        );
        assert!(text(&out).contains(&format!(
            "run: GUILDBOTICS_HOME='{}' BRANVIL_DATA_DIR='{}' '{}' environment prune",
            hub.home.join(".guildbotics").display(),
            hub.root().join("branvil").display(),
            hub.builds().join("current").display()
        )));
    }
}

#[test]
fn guildbotics_rehearsal_failure_keeps_the_old_server_running() {
    let hub = Hub::new();
    hub.install();
    let old = hub.installed_build();
    hub.commit("new");
    hub.mark("fail-gb-rehearsal");
    hub.run("update", None, false);
    hub.assert_still_runs_old(&old);
}

#[test]
fn guildbotics_live_failure_checks_old_data_and_reports_backups() {
    let hub = Hub::new();
    hub.install();
    hub.commit("new");
    hub.mark("change-gb-schema");
    hub.mark("fail-gb-live");
    let out = hub.run("update", None, false);
    assert!(text(&out).contains("Backups:"));
    assert!(text(&out).contains("/backups"));
    assert!(!hub.root().join("holder").exists());
    assert!(hub.lines("calls").contains("launchctl|disable|"));
    assert!(hub.lines("calls").contains("current|server|hub-data|check"));
    assert!(hub.builds().join("update-from").exists());
}

#[test]
fn guildbotics_backup_rotation_retains_three_complete_generations() {
    let hub = Hub::new();
    hub.install();
    let backups = hub.builds().parent().unwrap().join("backups");
    for stamp in ["2000", "2001", "2002", "2003"] {
        let path = backups.join(format!("guildbotics-update-{stamp}"));
        fs::create_dir_all(&path).unwrap();
        if stamp != "2003" {
            fs::write(path.join("manifest.json"), "").unwrap();
        }
    }
    hub.commit("new");
    hub.run("update", None, true);
    assert!(!backups.join("guildbotics-update-2000").exists());
    assert!(!backups.join("guildbotics-update-2003").exists());
    for stamp in ["2001", "2002"] {
        assert!(backups.join(format!("guildbotics-update-{stamp}")).exists());
    }
}

#[test]
fn update_uses_the_installed_identity_root_instead_of_the_shells_selection() {
    let hub = Hub::new();
    let identity = hub.root().join("identity");
    let name = identity.to_str().unwrap();
    hub.run_with(
        "install",
        &[
            ("GUILDBOTICS_HUB_SIGNING_IDENTITY", IDENTITY),
            ("GUILDBOTICS_IDENTITY_ROOT", name),
        ],
        true,
    );
    hub.commit("new");
    hub.run_with(
        "update",
        &[("GUILDBOTICS_IDENTITY_ROOT", "/unrelated/identity")],
        true,
    );
    let plist = fs::read_to_string(hub.plist()).unwrap();
    assert!(plist.contains(&format!(
        "<key>GUILDBOTICS_IDENTITY_ROOT</key><string>{name}</string>"
    )));
    assert!(!plist.contains("/unrelated/identity"));
}
