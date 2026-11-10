use std::time::Duration;

use clap::Subcommand;
use gb_server::{
    client::{self, OwnerClient},
    i18n,
};
use serde_json::json;

pub const FORCED_STOP_TIMEOUT: Duration = Duration::from_secs(90);

#[derive(Subcommand)]
pub enum ServiceCommand {
    /// Print server and service state as JSON for Hub maintenance
    #[command(hide = true)]
    Status,
    /// Start the services with the settings saved on the Service screen
    Start,
    /// Stop the services after their in-flight work; the server keeps running
    Stop {
        /// Cancel in-flight work instead of waiting for it
        #[arg(long)]
        force: bool,
    },
}

pub async fn run(command: ServiceCommand) -> anyhow::Result<()> {
    let language = client::language().await;
    let text = |key| i18n::t(language, key, &[]);
    let data = gb_base::home::data()?;
    if matches!(command, ServiceCommand::Status) {
        println!("{}", status(&data, language).await?);
        return Ok(());
    }
    let running = gb_server::running(&data, language)?
        .ok_or_else(|| anyhow::anyhow!(text("runtime.service_lock.not_running")))?;
    let owner = OwnerClient::connect(&data, running.port, language)
        .await?
        .ok_or_else(|| anyhow::anyhow!(text("runtime.service_lock.api_unavailable")))?;
    let (path, body, message) = match command {
        ServiceCommand::Status => unreachable!(),
        ServiceCommand::Start => (
            "/scheduler/start",
            None,
            "runtime.service_lock.services_started",
        ),
        ServiceCommand::Stop { force } => (
            "/scheduler/stop",
            Some(json!({"force": force})),
            "runtime.service_lock.services_stopped",
        ),
    };
    owner.post(path, body.as_ref(), None).await?;
    println!("{}", text(message));
    Ok(())
}

async fn status(
    data: &std::path::Path,
    language: &'static str,
) -> anyhow::Result<serde_json::Value> {
    let Some(holder) = gb_server::running(data, language)? else {
        return Ok(json!({"server_running": false, "running": false, "state": "stopped"}));
    };
    let owner = OwnerClient::connect(data, holder.port, language)
        .await?
        .ok_or_else(|| {
            anyhow::anyhow!(i18n::t(
                language,
                "runtime.service_lock.api_unavailable",
                &[]
            ))
        })?;
    let response = owner
        .request(reqwest::Method::GET, "/scheduler/status")
        .timeout(Duration::from_secs(10))
        .send()
        .await?;
    let value: serde_json::Value = client::checked(response).await?.json().await?;
    let running = value["scheduler"]["running"].as_bool().ok_or_else(|| {
        anyhow::anyhow!(i18n::t(
            language,
            "runtime.service_lock.api_unavailable",
            &[]
        ))
    })?;
    Ok(json!({"server_running": true, "running": running, "state": value["scheduler"]["state"]}))
}

/// Print a PID only when this machine's server holds the lock. With `ready`,
/// require its authenticated proof too: the port can be bound before serving.
pub async fn hub_probe(ready: bool) -> anyhow::Result<u8> {
    let language = client::language().await;
    let data = gb_base::home::data()?;
    let Some(holder) = gb_server::running(&data, language)? else {
        return Ok(u8::from(ready));
    };
    if ready
        && (OwnerClient::connect(&data, holder.port, language)
            .await?
            .is_none()
            || gb_server::running(&data, language)?.as_ref() != Some(&holder))
    {
        return Ok(1);
    }
    println!("{}", holder.pid);
    Ok(0)
}

pub async fn stop(force: bool) -> anyhow::Result<()> {
    let language = client::language().await;
    let data = gb_base::home::data()?;
    let text = |key| i18n::t(language, key, &[]);
    let Some(running) = gb_server::running(&data, language)? else {
        println!("{}", text("runtime.service_lock.not_running"));
        return Ok(());
    };
    let still_running =
        || gb_server::running(&data, language).map(|owner| owner.as_ref() == Some(&running));
    let request = async {
        let owner = OwnerClient::connect(&data, running.port, language)
            .await?
            .ok_or_else(|| anyhow::anyhow!(text("runtime.service_lock.api_unavailable")))?;
        owner
            .post(
                "/shutdown",
                Some(&json!({"force": force})),
                Some(Duration::from_secs(5)),
            )
            .await
    }
    .await;
    if let Err(error) = request
        && still_running()?
    {
        return Err(error);
    }
    let wait = async {
        while still_running()? {
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        Ok::<(), anyhow::Error>(())
    };
    if force {
        tokio::time::timeout(FORCED_STOP_TIMEOUT, wait)
            .await
            .map_err(|_| anyhow::anyhow!(text("runtime.service_lock.teardown_timeout")))??;
        println!("{}", text("runtime.service_lock.stopped_after_cancel"));
    } else {
        tokio::pin!(wait);
        match tokio::time::timeout(Duration::from_secs(2), &mut wait).await {
            Ok(result) => result?,
            Err(_) => {
                println!("{}", text("runtime.service_lock.waiting_for_work"));
                wait.await?;
            }
        }
        println!("{}", text("runtime.service_lock.stopped"));
    }
    Ok(())
}
