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
        crate::i18n::t(language, "cli.server.update_unsupported", &[])
    );
    let port = crate::endpoint::port()?;
    // Prove the forwarded Hub is reachable before opening the consent screen.
    let _ = super::cli_server(&gb_base::home::data()?, language).await?;
    let device = gb_workspace::identity::ensure_device_identity(&gb_base::home::data()?)?;
    let id = device.device_id.replace('-', "");
    let root = gb_base::home::machine_root()?.join("runner");
    std::fs::create_dir_all(&root)?;
    let join_lock = root.join(format!("join-{port}.lock"));
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
    let token = gb_config::secrets::get_os_credential(SERVICE, &account(port))?;
    let root = gb_base::home::machine_root()?.join("runner");
    if cfg!(windows) {
        gb_runner::set_runtime_binary(
            super::runner_runtime::prepare(&root.join("runtime"), language).await?,
        )
        .map_err(anyhow::Error::msg)?;
    }
    gb_runner::run_runner(
        args.default_token(token).default_work(root.join("work")),
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
        "$env:GUILDBOTICS_SERVER_PORT='{port}'; $env:GUILDBOTICS_HOME={}; & {} runner; exit $LASTEXITCODE",
        literal(&machine_root.to_string_lossy()),
        literal(&executable.to_string_lossy())
    );
    format!(r#"$ErrorActionPreference='Stop'
+$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
+$name='guildbotics-runner-'+$sid+'-{port}'
+$action=New-ScheduledTaskAction -Execute (Join-Path $PSHOME 'powershell.exe') -Argument '-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand {}'
+$trigger=New-ScheduledTaskTrigger -AtLogOn -User $sid
+$principal=New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel Limited
+$settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
+Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
+Start-ScheduledTask -TaskName $name
+"#,encode(&loop_script)).replace("\n+","\n")
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
        ] {
            assert!(script.contains(required));
        }
        assert!(!script.contains("--token"));
        assert!(!script.contains("BRANVIL_TOKEN"));
        assert_eq!(literal("a'b"), "'a''b'");
    }
}
