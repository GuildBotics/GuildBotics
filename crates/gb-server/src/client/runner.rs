//! Remote runner enrollment and the per-user Windows service.
use std::path::Path;
const SERVICE: &str = "guildbotics-runner";
fn account(port: u16) -> String {
    format!("127.0.0.1:{port}")
}

pub async fn join(open: impl FnOnce(&str)) -> anyhow::Result<()> {
    let language = super::language().await;
    anyhow::ensure!(
        cfg!(all(windows, target_arch = "x86_64")),
        crate::i18n::t(language, "cli.server.runner_join_unsupported", &[])
    );
    let port = crate::endpoint::port()?;
    // Prove the forwarded Hub is reachable before opening the consent screen.
    let _ = super::cli_server(&gb_base::home::data()?, language).await?;
    let device = gb_workspace::identity::ensure_device_identity(&gb_base::home::data()?)?;
    let id = device.device_id.replace('-', "");
    let root = gb_base::home::machine_root()?.join("runner");
    // The credential and task are per OS user/port, even when a development
    // machine root differs. Coordinate replacements in that same scope.
    let join_lock = gb_base::home::user_home()
        .ok_or_else(|| {
            anyhow::anyhow!(crate::i18n::t(
                language,
                "cli.server.runner_service_failed",
                &[]
            ))
        })?
        .join(".guildbotics/runner")
        .join(format!("join-{port}.lock"));
    let _enrolling = tokio::task::spawn_blocking(move || {
        gb_base::files::FileLock::hold(&join_lock, gb_base::files::SHARED_WRITE_TIMEOUT)
    })
    .await??;
    let _ = super::runner_runtime::prepare(&root.join("runtime"), language).await?;
    let token = super::connect::connect_runner(port, &id, language, open).await?;
    gb_config::secrets::keep_os_credential(SERVICE, &account(port), &token)?;
    let script = service_script(
        &std::env::current_exe()?,
        port,
        &gb_base::home::machine_root()?,
    );
    let encoded = encode(&script);
    let status = tokio::process::Command::new("powershell.exe")
        .args(["-NoProfile", "-NonInteractive", "-EncodedCommand", &encoded])
        .status()
        .await?;
    anyhow::ensure!(
        status.success(),
        crate::i18n::t(language, "cli.server.runner_service_failed", &[])
    );
    println!(
        "{}",
        crate::i18n::t(language, "cli.server.runner_joined", &[])
    );
    Ok(())
}

pub async fn run(args: gb_runner::RunnerArgs) -> anyhow::Result<u8> {
    let language = super::language().await;
    let port = crate::endpoint::port()?;
    let args = resolve_token(args, language, || {
        gb_config::secrets::get_os_credential(SERVICE, &account(port))
    })?;
    let root = gb_base::home::machine_root()?.join("runner");
    if cfg!(windows) {
        gb_runner::set_runtime_binary(
            super::runner_runtime::prepare(&root.join("runtime"), language).await?,
        )
        .map_err(anyhow::Error::msg)?;
    }
    gb_runner::run_runner(
        args.default_work(root.join("work")),
        format!("http://127.0.0.1:{port}"),
        root,
        async {
            anyhow::bail!(crate::i18n::t(
                language,
                "cli.server.runner_join_required",
                &[]
            ))
        },
    )
    .await
    .map_err(|error| localized_error(error, language))
}

/// Resolve credentials before downloading a runtime or creating runner state.
fn resolve_token(
    args: gb_runner::RunnerArgs,
    language: &str,
    stored: impl FnOnce() -> anyhow::Result<Option<String>>,
) -> anyhow::Result<gb_runner::RunnerArgs> {
    if !args.needs_token() {
        return Ok(args);
    }
    let args = args.default_token(stored()?);
    anyhow::ensure!(
        !args.needs_token(),
        crate::i18n::t(language, "cli.server.runner_join_required", &[])
    );
    Ok(args)
}

fn literal(text: &str) -> String {
    format!("'{}'", text.replace('\'', "''"))
}
fn encode(text: &str) -> String {
    use base64::Engine;
    let bytes: Vec<u8> = text.encode_utf16().flat_map(u16::to_le_bytes).collect();
    base64::engine::general_purpose::STANDARD.encode(bytes)
}
/// No credentials enter the registered task or the child command line.
pub fn service_script(executable: &Path, port: u16, machine_root: &Path) -> String {
    let loop_script = format!(
        "$env:GUILDBOTICS_SERVER_PORT='{port}'; $env:GUILDBOTICS_HOME={};\n$runner={}\n{}",
        literal(&machine_root.to_string_lossy()),
        literal(&executable.to_string_lossy()),
        include_str!("runner-loop.ps1")
    );
    include_str!("runner-service.ps1")
        .replace("$port = 0", &format!("$port = {port}"))
        .replace(
            "$encoded = ''",
            &format!("$encoded = '{}'", encode(&loop_script)),
        )
}

/// Render the runner's control outcome at the CLI boundary.
pub fn localized_error(error: anyhow::Error, language: &str) -> anyhow::Error {
    if error.is::<gb_runner::runner::VersionChanged>() {
        anyhow::anyhow!(crate::i18n::t(
            language,
            "cli.server.runner_version_changed",
            &[]
        ))
    } else {
        error
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::{CommandFactory, FromArgMatches};

    #[derive(clap::Parser)]
    struct Cli {
        #[command(flatten)]
        args: gb_runner::RunnerArgs,
    }

    fn args(arguments: &[&str]) -> gb_runner::RunnerArgs {
        let matches = Cli::command()
            .mut_arg("token", |arg| arg.env(None::<&str>))
            .try_get_matches_from(std::iter::once("runner").chain(arguments.iter().copied()))
            .unwrap();
        Cli::from_arg_matches(&matches).unwrap().args
    }

    #[test]
    fn credentials_are_required_only_for_serving_and_explicit_values_win() {
        for command in [
            vec!["--token", "fixture"],
            vec!["doctor"],
            vec!["register", "test-runner"],
            vec!["pull", "test:latest"],
            vec!["install-msb", "--from", "test-msb"],
        ] {
            assert!(
                resolve_token(args(&command), "ja", || panic!("unneeded keychain read")).is_ok()
            );
        }
        let stored = resolve_token(args(&[]), "ja", || Ok(Some("fixture".into()))).unwrap();
        assert!(!stored.needs_token());
        for language in ["en", "ja"] {
            let error = resolve_token(args(&[]), language, || Ok(None))
                .err()
                .unwrap();
            let expected = crate::i18n::t(language, "cli.server.runner_join_required", &[]);
            assert_eq!(error.to_string(), expected);
            assert!(expected.contains("guildbotics runner join"));
            assert!(!expected.contains("cli.server."));
        }
        let error = resolve_token(args(&[]), "en", || anyhow::bail!("credential unavailable"))
            .err()
            .unwrap();
        assert_eq!(error.to_string(), "credential unavailable");
    }

    #[test]
    fn environment_token_bypasses_the_keychain() {
        if std::env::var_os("RUNNER_TOKEN_TEST_CHILD").is_none() {
            let result = std::process::Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "client::runner::tests::environment_token_bypasses_the_keychain",
                    "--nocapture",
                ])
                .env("RUNNER_TOKEN_TEST_CHILD", "1")
                .env("BRANVIL_TOKEN", "fixture")
                .output()
                .unwrap();
            assert!(
                result.status.success(),
                "{}",
                String::from_utf8_lossy(&result.stderr)
            );
            assert!(String::from_utf8_lossy(&result.stdout).contains("1 passed"));
            return;
        }
        let matches = Cli::command().try_get_matches_from(["runner"]).unwrap();
        let args = Cli::from_arg_matches(&matches).unwrap().args;
        assert!(resolve_token(args, "ja", || panic!("explicit env token wins")).is_ok());
    }

    #[cfg(not(all(windows, target_arch = "x86_64")))]
    #[tokio::test]
    async fn unsupported_join_explains_the_platform_without_opening_consent() {
        let error = join(|_| panic!("unsupported platform must not connect"))
            .await
            .unwrap_err();
        assert!(error.to_string().contains("Windows x86_64"));
        for language in ["en", "ja"] {
            let message = crate::i18n::t(language, "cli.server.runner_join_unsupported", &[]);
            assert!(message.contains("Windows x86_64"));
            assert!(message.contains("--token"));
            assert!(!message.contains("cli.server."));
        }
    }

    #[test]
    fn service_is_per_user_persistent_and_has_no_token_argument() {
        let script = service_script(
            Path::new("C:/O'Brien/guildbotics.exe"),
            7878,
            Path::new("C:/RunnerHome"),
        );
        for required in [
            "-AtLogOn",
            "Interactive",
            "Limited",
            "-RestartCount 999",
            "-MultipleInstances IgnoreNew",
            "-ExecutionTimeLimit",
            "-EncodedCommand",
            "Start-ScheduledTask",
            "--headless",
            "-StartWhenAvailable",
        ] {
            assert!(script.contains(required));
        }
        assert!(!script.contains("--token"));
        assert!(!script.contains("BRANVIL_TOKEN"));
        use base64::Engine;
        let encoded = script
            .lines()
            .find_map(|line| line.strip_prefix("$encoded = '"))
            .unwrap()
            .trim_end_matches('\'');
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(encoded)
            .unwrap();
        let words: Vec<u16> = bytes
            .chunks_exact(2)
            .map(|pair| u16::from_le_bytes([pair[0], pair[1]]))
            .collect();
        let supervisor = String::from_utf16(&words).unwrap();
        assert!(supervisor.contains("while ($true)"));
        assert!(supervisor.contains("& $runner runner"));
        assert!(supervisor.contains("Start-Sleep -Seconds 15"));
        assert!(supervisor.contains("catch { }"));
        assert!(supervisor.contains("C:/O''Brien/guildbotics.exe"));
        assert!(supervisor.contains("GUILDBOTICS_SERVER_PORT='7878'"));
        assert!(supervisor.contains("GUILDBOTICS_HOME='C:/RunnerHome'"));
        assert!(!supervisor.contains("exit $LASTEXITCODE"));
        assert!(
            script.find("$observed = @(Get-CimInstance").unwrap()
                < script.find("Stop-ScheduledTask").unwrap()
        );
        assert!(script.contains("-MethodName GetOwnerSid"));
        assert!(script.contains("$live.CreationDate -eq $process.CreationDate"));
        assert!(script.contains("$handles[$key] = $target"));
        assert!(
            script.find("$null = Get-RunnerProcess $process").unwrap()
                < script.find("Stop-ScheduledTask").unwrap()
        );
        assert_eq!(literal("a'b"), "'a''b'");
    }

    #[cfg(windows)]
    #[test]
    fn windows_task_replacement_and_supervisor_execute() {
        let temp = tempfile::tempdir().unwrap();
        let stub = temp.path().join("runner ' 東京.ps1");
        let log = temp.path().join("calls.txt");
        std::fs::write(&stub, "[IO.File]::AppendAllText($env:RUNNER_TEST_LOG, ($args[0] + ':' + $env:GUILDBOTICS_SERVER_PORT + ':' + $env:GUILDBOTICS_HOME + [Environment]::NewLine), [Text.UTF8Encoding]::new($false))\nWrite-Error 'fixture stderr' -ErrorAction Continue\nif ((Get-Content -LiteralPath $env:RUNNER_TEST_LOG).Count -eq 1) { exit 0 }; exit 19\n").unwrap();
        let installer = temp.path().join("install.ps1");
        let root = temp.path().join("home ' 東京");
        std::fs::write(&installer, service_script(&stub, 7878, &root)).unwrap();
        let harness = temp.path().join("harness.ps1");
        std::fs::write(&harness, include_str!("runner-tests.ps1")).unwrap();
        let output = std::process::Command::new("powershell.exe")
            .args([
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
            ])
            .arg(&harness)
            .env("RUNNER_TEST_INSTALLER", installer)
            .env("RUNNER_TEST_STUB", &stub)
            .env("RUNNER_TEST_LOG", &log)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        let calls = std::fs::read_to_string(log).unwrap();
        assert_eq!(calls.lines().count(), 2);
        assert!(
            calls
                .lines()
                .all(|line| line == format!("runner:7878:{}", root.display()))
        );
    }
}
