//! The Hub's automatic update loop. It watches origin/main of the Hub
//! checkout and runs `scripts/hub-server.sh update` for a commit that is safe
//! to install. launchd runs it from the signed build (`builds/current`), so
//! macOS and the owner see GuildBotics rather than an interpreter. It asks
//! that build for every fact, as an operator would.

use serde_json::{Map, Value, json};
use std::{
    io::{Read, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

/// The server agent's settings that every command of this loop runs with.
const SETTINGS: [&str; 4] = [
    "GUILDBOTICS_HOME",
    "BRANVIL_DATA_DIR",
    "GUILDBOTICS_SERVER_PORT",
    "GUILDBOTICS_IDENTITY_ROOT",
];
/// Settings the server agent always records.
const REQUIRED: [&str; 2] = ["GUILDBOTICS_HOME", "BRANVIL_DATA_DIR"];

struct Hub {
    checkout: PathBuf,
    instance: String,
    state_dir: PathBuf,
    current: PathBuf,
    settings: Vec<(&'static str, Option<String>)>,
    wait_notice: u64,
    state: Map<String, Value>,
}

/// Runs the loop: once with `once`, otherwise every interval until the
/// active build changes, when launchd starts the new build's loop.
pub fn run(checkout: PathBuf, once: bool) -> anyhow::Result<()> {
    let seconds = |name: &str, default: u64| -> anyhow::Result<u64> {
        let value = std::env::var(name).map_or(Ok(default), |value| value.parse::<u64>())?;
        anyhow::ensure!(value > 0, "Update intervals must be positive.");
        Ok(value)
    };
    let interval = seconds("GUILDBOTICS_HUB_UPDATE_INTERVAL", 300)?;
    let wait_notice = seconds("GUILDBOTICS_HUB_WAIT_NOTICE", 3600)?;
    // GUILDBOTICS_HUB_INSTANCE names a Hub copy, as in hub-server.sh.
    let instance = std::env::var("GUILDBOTICS_HUB_INSTANCE").unwrap_or_default();
    let home =
        PathBuf::from(std::env::var_os("HOME").ok_or_else(|| anyhow::anyhow!("HOME is not set"))?);
    let suffix = |separator: &str| {
        if instance.is_empty() {
            String::new()
        } else {
            format!("{separator}{instance}")
        }
    };
    let state_dir = home
        .join("Library/Application Support/GuildBotics")
        .join(format!("Hub{}", suffix("-")));
    let plist = home
        .join("Library/LaunchAgents")
        .join(format!("local.guildbotics{}.plist", suffix(".")));
    let state_path = state_dir.join("auto-update.json");
    let state = match std::fs::read_to_string(&state_path) {
        Ok(text) => serde_json::from_str(&text)?,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Map::new(),
        Err(error) => return Err(error.into()),
    };
    let mut hub = Hub {
        checkout,
        instance,
        current: state_dir.join("builds/current"),
        state_dir,
        settings: Vec::new(),
        wait_notice,
        state,
    };
    for key in SETTINGS {
        let key_path = format!("EnvironmentVariables.{key}");
        let plist = plist.to_string_lossy();
        match hub.run(
            Path::new("plutil"),
            &["-extract", &key_path, "raw", "-o", "-", &plist],
            None,
            None,
        ) {
            Ok(value) => hub.settings.push((key, Some(value))),
            Err(error) if REQUIRED.contains(&key) => return Err(error),
            // Like hub-server.sh, use the agent's setting rather than the shell's.
            Err(_) => hub.settings.push((key, None)),
        }
    }
    if once {
        hub.tick();
        return Ok(());
    }
    let own = gb_base::home::python_package();
    let own = own
        .parent()
        .and_then(Path::file_name)
        .map(ToOwned::to_owned);
    loop {
        hub.tick();
        let active = hub.run(
            &hub.current.clone(),
            &["server", "hub-probe", "--source"],
            None,
            None,
        );
        if active.is_ok_and(|source| Path::new(&source).file_name() != own.as_deref()) {
            return Ok(());
        }
        std::thread::sleep(Duration::from_secs(interval));
    }
}

fn quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', r"'\''"))
}

fn file_identity(path: &Path) -> anyhow::Result<String> {
    let metadata = std::fs::metadata(path)?;
    let birth = metadata.created()?.duration_since(UNIX_EPOCH)?.as_millis();
    Ok(format!("{}:{}:{birth}", metadata.dev(), metadata.ino()))
}

fn now_millis() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

impl Hub {
    /// Runs `program` in the checkout with the agent's settings and returns
    /// its trimmed output. With `waiting`, it notifies once that the run is
    /// still going after the wait notice time.
    fn run(
        &mut self,
        program: &Path,
        args: &[&str],
        input: Option<&str>,
        waiting: Option<&str>,
    ) -> anyhow::Result<String> {
        let mut command = Command::new(program);
        command
            .args(args)
            .current_dir(&self.checkout)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());
        for (key, value) in &self.settings {
            match value {
                Some(value) => command.env(key, value),
                None => command.env_remove(key),
            };
        }
        let mut child = command.spawn()?;
        let mut stdin = child.stdin.take().expect("piped");
        let _ = stdin.write_all(input.unwrap_or_default().as_bytes());
        drop(stdin);
        let mut stdout = child.stdout.take().expect("piped");
        let reader = std::thread::spawn(move || {
            let mut output = String::new();
            stdout.read_to_string(&mut output).map(|_| output)
        });
        let started = Instant::now();
        let mut noticed = false;
        let status = loop {
            if let Some(status) = child.try_wait()? {
                break status;
            }
            if let Some(sha) = waiting.filter(|_| !noticed)
                && started.elapsed() >= Duration::from_secs(self.wait_notice)
            {
                noticed = true;
                self.notify(
                    sha,
                    "waiting",
                    "Update is still waiting or building",
                    "keep waiting; inspect the Hub log",
                    None,
                );
            }
            std::thread::sleep(Duration::from_millis(50));
        };
        let output = reader.join().expect("stdout reader")?;
        let name = program.file_name().unwrap_or_default().to_string_lossy();
        anyhow::ensure!(
            status.success(),
            "{name} {} exited {}",
            args.first().copied().unwrap_or_default(),
            status
                .code()
                .map_or("by a signal".into(), |code| code.to_string())
        );
        Ok(output.trim().to_owned())
    }

    fn git(&mut self, args: &[&str]) -> anyhow::Result<String> {
        let checkout = self.checkout.to_string_lossy().into_owned();
        let args: Vec<&str> = ["-C", checkout.as_str()]
            .into_iter()
            .chain(args.iter().copied())
            .collect();
        self.run(Path::new("git"), &args, None, None)
    }

    fn cli(&mut self, args: &[&str]) -> anyhow::Result<Value> {
        let current = self.current.clone();
        Ok(serde_json::from_str(
            &self.run(&current, args, None, None)?,
        )?)
    }

    fn save(&self) -> anyhow::Result<()> {
        self.save_state(&self.state)
    }

    fn save_state(&self, state: &Map<String, Value>) -> anyhow::Result<()> {
        let path = self.state_dir.join("auto-update.json");
        let pending = self.state_dir.join("auto-update.json.new");
        std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(&pending)?
            .write_all(Value::Object(state.clone()).to_string().as_bytes())?;
        std::fs::rename(pending, path)?;
        Ok(())
    }

    // Guidance runs this Hub's build and script with its settings, whatever
    // the shell it is pasted into sets: a bare guildbotics may run another Hub's build.
    fn guildbotics(&self) -> String {
        let unset = self.settings.iter().filter(|(_, value)| value.is_none());
        let set = self
            .settings
            .iter()
            .filter_map(|(key, value)| value.as_ref().map(|value| (key, value)));
        let mut words = vec!["env".to_owned()];
        words.extend(unset.map(|(key, _)| format!("-u {key}")));
        words.extend(set.map(|(key, value)| format!("{key}={}", quote(value))));
        words.push(quote(&self.current.to_string_lossy()));
        words.join(" ")
    }

    fn hub_server(&self) -> String {
        let instance = if self.instance.is_empty() {
            "-u GUILDBOTICS_HUB_INSTANCE".to_owned()
        } else {
            format!("GUILDBOTICS_HUB_INSTANCE={}", self.instance)
        };
        let script = self.checkout.join("scripts/hub-server.sh");
        format!("env {instance} {}", quote(&script.to_string_lossy()))
    }

    fn notify(&mut self, sha: &str, stage: &str, reason: &str, next: &str, key: Option<String>) {
        let key = key.unwrap_or_else(|| format!("{sha}:{stage}"));
        if self.list("notified").contains(&Value::String(key.clone())) {
            return;
        }
        // A copy's notice names it: its commands need GUILDBOTICS_HUB_INSTANCE.
        let copy = if self.instance.is_empty() {
            String::new()
        } else {
            format!(" copy {0} (GUILDBOTICS_HUB_INSTANCE={0})", self.instance)
        };
        let message = format!("Hub{copy} {sha}: {stage}: {reason}. Next: {next}.");
        let now = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        eprintln!("{now} {message}");
        self.object("pendingNotices")
            .insert(key.clone(), message.clone().into());
        self.persist();
        self.deliver(&key, &message);
    }

    fn deliver(&mut self, key: &str, message: &str) {
        let current = self.current.clone();
        match self.run(&current, &["server", "hub-notify"], Some(message), None) {
            // With no webhook, the agent's stderr log is the notification destination.
            Ok(_) => {
                self.list("notified").push(key.into());
                self.object("pendingNotices").remove(key);
                self.persist();
            }
            Err(error) => eprintln!("{error}"),
        }
    }

    fn persist(&self) {
        if let Err(error) = self.save() {
            eprintln!("{error}");
        }
    }

    fn list(&mut self, name: &str) -> &mut Vec<Value> {
        let value = self.state.entry(name).or_insert_with(|| json!([]));
        if !value.is_array() {
            *value = json!([]);
        }
        value.as_array_mut().expect("array")
    }

    fn object(&mut self, name: &str) -> &mut Map<String, Value> {
        let value = self.state.entry(name).or_insert_with(|| json!({}));
        if !value.is_object() {
            *value = json!({});
        }
        value.as_object_mut().expect("object")
    }

    // State-derived failures have an episode lifetime independent of commits.
    fn clear_blocker(&mut self, stage: &str) {
        if self.object("blockers").remove(stage).is_some() {
            self.persist();
        }
    }

    fn blocker(
        &mut self,
        sha: &str,
        stage: &str,
        reason: &str,
        next: &str,
        identity: Option<String>,
        delay: u64,
    ) {
        let identity = identity.unwrap_or_else(|| reason.to_owned());
        let known = self
            .object("blockers")
            .get(stage)
            .and_then(|episode| episode["identity"].as_str())
            == Some(&identity);
        if !known {
            let now = now_millis();
            let episode =
                json!({"identity": identity, "since": now as u64, "key": format!("{stage}-{now}")});
            self.object("blockers").insert(stage.to_owned(), episode);
            self.persist();
        }
        let episode = self.object("blockers")[stage].clone();
        let since = u128::from(episode["since"].as_u64().unwrap_or_default());
        if now_millis().saturating_sub(since) >= u128::from(delay) * 1000 {
            let key = episode["key"].as_str().map(ToOwned::to_owned);
            self.notify(sha, stage, reason, next, key);
        }
    }

    fn failure_record(&self) -> anyhow::Result<String> {
        match std::fs::read_to_string(self.state_dir.join("last-failure")) {
            Ok(text) => Ok(text.trim().to_owned()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(String::new()),
            Err(error) => Err(error.into()),
        }
    }

    fn restore_service(&mut self) -> anyhow::Result<()> {
        let path = self.state_dir.join("service-before");
        let saved = match std::fs::read(&path) {
            Ok(saved) => saved,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(error.into()),
        };
        let saved: Value = serde_json::from_slice(&saved)?;
        let before: bool = serde_json::from_value(saved["service_running"].clone())?;
        let service = self.cli(&["service", "status"])?;
        anyhow::ensure!(
            service["server_running"] == true,
            "Server is unavailable; service resume remains pending"
        );
        if service["running"].as_bool() != Some(before) {
            let current = self.current.clone();
            self.run(
                &current,
                &["service", if before { "start" } else { "stop" }],
                None,
                None,
            )?;
        }
        std::fs::remove_file(path)?;
        Ok(())
    }

    fn schema_version(&mut self, sha: &str) -> anyhow::Result<u64> {
        let text = self.git(&["show", &format!("{sha}:crates/branvil/src/store.rs")])?;
        text.lines()
            .find_map(|line| {
                line.strip_prefix("pub const SCHEMA_VERSION: u64 = ")
                    .and_then(|value| value.strip_suffix(';'))
            })
            .ok_or_else(|| anyhow::anyhow!("Storage schema version missing"))?
            .parse()
            .map_err(Into::into)
    }

    fn tick(&mut self) {
        let mut sha = self
            .state
            .get("attempt")
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .to_owned();
        let mut stage = "preflight";
        if let Err(error) = self.inspect(&mut sha, &mut stage) {
            self.blocker(
                &sha,
                stage,
                &error.to_string(),
                "inspect the auto-update log and correct the cause",
                None,
                0,
            );
        }
    }

    /// Finish only the activation we recorded, under the script's operation lock.
    /// Upkeep builds snapshots but never owns this post-activation cleanup.
    fn finish_snapshot(&mut self, sha: &str) {
        match self.try_finish_snapshot() {
            Ok(()) => self.clear_blocker("snapshot"),
            Err(error) => self.blocker(
                sha,
                "snapshot",
                &format!("Snapshot cleanup failed: {error}"),
                "inspect environment status and environment prune; cleanup will retry automatically while update checks continue",
                None,
                0,
            ),
        }
    }

    fn try_finish_snapshot(&mut self) -> anyhow::Result<()> {
        let Some(request) = self.state.get("snapshotCleanup").cloned() else {
            return Ok(());
        };
        let operation = self.state_dir.join("operation");
        match std::fs::create_dir(&operation) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => return Ok(()),
            Err(error) => return Err(error.into()),
        }
        let result = self.finish_snapshot_locked(&request);
        std::fs::remove_dir(operation)?;
        result
    }

    fn finish_snapshot_locked(&mut self, request: &Value) -> anyhow::Result<()> {
        let sha = request["sha"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("Missing cleanup revision"))?;
        let workspace = request["workspace"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("Missing cleanup workspace"))?;
        if self.state_dir.join("builds/update-from").try_exists()? {
            return Ok(());
        }
        let current = self.current.clone();
        let source = self.run(&current, &["server", "hub-probe", "--source"], None, None)?;
        if Path::new(&source).file_name().and_then(|s| s.to_str()) != Some(sha) {
            // Rollback (or a later manual activation) superseded this request.
            self.state.remove("snapshotCleanup");
            return self.save();
        }
        let after = self.cli(&[
            "environment",
            "status",
            "--format",
            "json",
            "--workspace",
            workspace,
        ])?;
        match after["snapshot"]["state"].as_str() {
            Some("ready") => {
                self.run(
                    &current,
                    &["environment", "prune", "--workspace", workspace],
                    None,
                    None,
                )?;
                self.state.remove("snapshotCleanup");
                self.save()?;
            }
            Some("building") => {}
            Some(snapshot @ ("missing" | "stale" | "failed")) => {
                let next = format!(
                    "run {} environment build --workspace {}; cleanup will retry automatically",
                    self.guildbotics(),
                    quote(workspace)
                );
                self.notify(
                    sha,
                    "snapshot",
                    &format!("Snapshot is {snapshot}"),
                    &next,
                    Some(format!("{sha}:snapshot:{snapshot}")),
                );
            }
            _ => anyhow::bail!("Snapshot state is unavailable"),
        }
        Ok(())
    }

    fn inspect(&mut self, sha: &mut String, stage: &mut &'static str) -> anyhow::Result<()> {
        let pending: Vec<(String, String)> = self
            .object("pendingNotices")
            .iter()
            .map(|(key, message)| (key.clone(), message.as_str().unwrap_or_default().to_owned()))
            .collect();
        for (key, message) in pending {
            self.deliver(&key, &message);
        }
        let operation = self.state_dir.join("operation");
        if operation.exists() {
            let next = format!(
                "verify that its processes ended before removing {}",
                quote(&operation.to_string_lossy())
            );
            let wait = self.wait_notice;
            self.blocker(
                sha,
                "operation",
                "The same Hub operation lock has persisted",
                &next,
                Some(file_identity(&operation)?),
                wait,
            );
            return Ok(());
        }
        self.clear_blocker("operation");
        let interrupted = self.state_dir.join("builds/update-from");
        if interrupted.exists() {
            let next = format!("run {} recover", self.hub_server());
            self.blocker(
                sha,
                "recovery",
                "An interrupted update remains",
                &next,
                Some(file_identity(&interrupted)?),
                0,
            );
            return Ok(());
        }
        self.clear_blocker("recovery");
        self.restore_service()?;
        self.finish_snapshot(sha);
        // A cleanup that could not release the shared operation lock is still active.
        if operation.try_exists()? {
            return Ok(());
        }
        self.git(&["fetch", "origin", "main"])?;
        let head = self.git(&["rev-parse", "--verify", "HEAD^{commit}"])?;
        let repo = self.checkout.to_string_lossy().into_owned();
        let history = self.cli(&[
            "server",
            "hub-verified",
            "--checkout",
            &repo,
            "--since",
            &head,
        ])?;
        let current = self.current.clone();
        let source = self.run(&current, &["server", "hub-probe", "--source"], None, None)?;
        // Repair missing distribution of the running build, including manual updates.
        if let Some(commit) = Path::new(&source).file_name().and_then(|v| v.to_str()) {
            let checkout = self.checkout.to_string_lossy().into_owned();
            let _ = self.cli(&[
                "server",
                "hub-clients",
                "--checkout",
                &checkout,
                "--sha",
                commit,
            ]);
        }
        self.clear_blocker("preflight");
        let checkout = quote(&repo);
        *stage = "checkout";
        if history["since_on_main"].as_bool() != Some(true) {
            self.blocker(
                &head,
                stage,
                "Checkout cannot fast-forward to the verified commit",
                &format!("restore {checkout} to a checkout on origin/main"),
                None,
                0,
            );
            return Ok(());
        }
        self.clear_blocker(stage);
        let verified = history["verified"]
            .as_array()
            .ok_or_else(|| anyhow::anyhow!("Invalid verified history"))?;
        if verified.is_empty() {
            self.clear_blocker("migration");
            return Ok(());
        }
        *stage = "migration";
        let running = Path::new(&source)
            .file_name()
            .and_then(|v| v.to_str())
            .ok_or_else(|| anyhow::anyhow!("Invalid running build source"))?;
        let version = self.schema_version(running)?;
        let mut candidate = None;
        for commit in verified {
            let commit = commit
                .as_str()
                .ok_or_else(|| anyhow::anyhow!("Invalid verified SHA"))?;
            anyhow::ensure!(
                commit.len() == 40 && commit.bytes().all(|b| b.is_ascii_hexdigit()),
                "Invalid verified SHA"
            );
            let target_version = self.schema_version(commit)?;
            if candidate.is_none() && target_version <= version.saturating_add(1) {
                candidate = Some(commit.to_owned());
            }
        }
        let Some(candidate) = candidate else {
            self.blocker(
                &head,
                stage,
                "No verified commit raises the storage format by one step",
                "wait for a compatible verified commit or update manually",
                None,
                0,
            );
            return Ok(());
        };
        *sha = candidate;
        let result = self
            .state
            .get("result")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned();
        if self.state.get("attempt").and_then(Value::as_str) == Some(sha.as_str())
            && ["failed", "updating"].contains(&result.as_str())
        {
            if result == "updating" {
                let next = format!(
                    "inspect the Hub log; run {} recover if needed, or update manually",
                    self.hub_server()
                );
                self.notify(
                    sha,
                    "update",
                    "Previous automatic attempt ended without an outcome",
                    &next,
                    None,
                );
            }
            return Ok(());
        }
        let checkout = quote(&self.checkout.to_string_lossy());
        if !self.git(&["status", "--porcelain"])?.is_empty() {
            self.notify(
                sha,
                "checkout",
                "Checkout has uncommitted changes",
                &format!("commit or remove the changes in {checkout}"),
                None,
            );
            return Ok(());
        }
        *stage = "clients";
        self.cli(&["server", "hub-clients", "--checkout", &repo, "--sha", sha])?;
        self.clear_blocker(stage);
        *stage = "migration";
        let declared = !self
            .git(&["ls-tree", "--name-only", sha, "--", "hub-update.json"])?
            .is_empty();
        let migration: Value = if declared {
            serde_json::from_str(&self.git(&["show", &format!("{sha}:hub-update.json")])?)?
        } else {
            json!({"manual_update_version": 0})
        };
        let installed = self.cli(&["server", "hub-migration"])?;
        let required = migration["manual_update_version"]
            .as_u64()
            .ok_or_else(|| anyhow::anyhow!("Invalid migration declaration"))?;
        self.clear_blocker(stage);
        if required
            > installed["manual_update_version"]
                .as_u64()
                .unwrap_or_default()
        {
            let reason = format!(
                "Manual update required: {}",
                migration["instructions"].as_str().unwrap_or("null")
            );
            let next = format!(
                "follow those instructions, then run {} update",
                self.hub_server()
            );
            self.notify(sha, stage, &reason, &next, None);
            return Ok(());
        }
        *stage = "image";
        let environment = self.cli(&["environment", "status", "--format", "json"])?;
        self.clear_blocker(stage);
        if let Some(image) = environment["image"]["reference"]
            .as_str()
            .filter(|_| environment["image"]["present"] != true)
        {
            let next = format!(
                "load the declared image with {} environment image load",
                self.guildbotics()
            );
            self.notify(
                sha,
                stage,
                &format!("Custom base image is not loaded: {image}"),
                &next,
                None,
            );
            return Ok(());
        }
        *stage = "service";
        let service = self.cli(&["service", "status"])?;
        if service["server_running"] != true {
            let next = format!(
                "inspect the server log and run {} restart or recover",
                self.hub_server()
            );
            self.blocker(sha, stage, "Server is not running", &next, None, 0);
            return Ok(());
        }
        self.clear_blocker(stage);
        let workspace = self.cli(&["workspace", "current", "--format", "json"])?;
        let workspace = workspace["workspace"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("Workspace is unavailable"))?;
        let previous_failure = self.failure_record()?;
        let subject = self.git(&["show", "-s", "--format=%s", sha])?;
        // Do not expose an unstarted attempt to blocker persistence on save failure.
        let mut intent = self.state.clone();
        intent.insert("attempt".into(), sha.as_str().into());
        intent.insert("result".into(), "updating".into());
        intent.insert(
            "snapshotCleanup".into(),
            json!({"sha":sha,"workspace":workspace}),
        );
        self.save_state(&intent)?;
        self.state = intent;
        *stage = "update";
        if let Err(error) = self.run(
            &current,
            &[
                "server",
                "hub-phase",
                "--phase",
                "preparing",
                "--commit",
                sha,
                "--subject",
                &subject,
            ],
            None,
            None,
        ) {
            eprintln!("Hub phase notification failed: {error:#}");
        }
        if let Err(error) = self.git(&["merge", "--ff-only", sha]) {
            let _ = self.run(&current, &["server", "hub-phase", "--clear"], None, None);
            return Err(error);
        }
        match self.run(
            Path::new("sh"),
            &["scripts/hub-server.sh", "update"],
            None,
            Some(sha),
        ) {
            Ok(output) => {
                println!("{output}");
                self.state.insert("result".into(), "success".into());
            }
            Err(error) => {
                if let Err(error) =
                    self.run(&current, &["server", "hub-phase", "--clear"], None, None)
                {
                    eprintln!("Hub phase notification failed: {error:#}");
                }
                self.state.insert("result".into(), "failed".into());
                let failure = self.failure_record()?;
                let mut detail = if !failure.is_empty() && failure != previous_failure {
                    failure
                } else {
                    error.to_string()
                };
                if interrupted.exists() {
                    detail.push_str(" Interrupted update remains; recovery required.");
                }
                let next = format!(
                    "inspect the Hub log; run {} recover if needed; a newer verified commit is required for automatic retry",
                    self.hub_server()
                );
                self.notify(sha, stage, &detail, &next, None);
            }
        }
        self.save()?;
        if !interrupted.exists() {
            self.restore_service()?;
        }
        if self.state.get("result").and_then(Value::as_str) == Some("success") {
            *stage = "snapshot";
            self.finish_snapshot(sha);
        }
        Ok(())
    }
}
