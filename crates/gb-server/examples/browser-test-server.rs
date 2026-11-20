//! The real HTTP server with an ephemeral credential vault for browser tests.
//! No test secret reaches the operating system's keychain.
use std::sync::{Arc, Mutex};

#[derive(Default)]
struct Vault(Mutex<Option<String>>);

impl gb_config::secrets::Keychain for Vault {
    fn read(&self) -> anyhow::Result<Option<String>> {
        Ok(self.0.lock().unwrap().clone())
    }

    fn create(&self, key: &str) -> anyhow::Result<()> {
        let mut stored = self.0.lock().unwrap();
        anyhow::ensure!(stored.is_none(), "test vault already initialized");
        *stored = Some(key.into());
        Ok(())
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let arguments = std::env::args().skip(1).collect::<Vec<_>>();
    // Git runs the hooks the embedded code host writes as this executable's.
    if let [server, hook, name] = arguments.as_slice()
        && server == "server"
        && hook == "git-hook"
    {
        std::process::exit(i32::from(branvil::git_hook(name).await));
    }
    if std::env::var_os("GUILDBOTICS_BLACKBOX_TIMINGS").is_some() {
        use tracing_subscriber::fmt::format::FmtSpan;
        tracing_subscriber::fmt()
            .with_env_filter("member_timing=info")
            .with_ansi(false)
            .with_span_events(FmtSpan::NEW | FmtSpan::CLOSE)
            .with_writer(std::io::stderr)
            .init();
    }
    if !arguments.is_empty() {
        anyhow::ensure!(
            arguments[0] == "member",
            "unsupported test launcher command"
        );
        // Use the production member client, including its version handshake.
        std::process::exit(i32::from(
            gb_server::client::member::run(arguments[1..].to_vec()).await?,
        ));
    }
    let server = gb_server::Server::start_with_keychain(Arc::new(Vault::default())).await?;
    let embedded = if std::env::var_os("GUILDBOTICS_BROWSER_TEST_BRANVIL").is_some() {
        let vault: Arc<dyn branvil::vault::Vault> = Arc::new(branvil::vault::Memory::default());
        let data_dir = gb_server::endpoint::code_host_data()?;
        let base = server.base_url();
        let host = server.host();
        let active_agents = server.active_agents();
        Some(async move {
            let embedded =
                branvil::prepare_embedded(&data_dir, vault, &base, host, active_agents).await?;
            if std::env::var_os("GUILDBOTICS_BLACKBOX_CONTROL").is_some() {
                let data = embedded.data_api();
                tokio::spawn(async move { controls(data).await.expect("test controls") });
            }
            Ok(embedded)
        })
    } else {
        None
    };
    let (_, result) = if let Some(embedded) = embedded {
        server
            .serve_embedded(
                async { tokio::signal::ctrl_c().await.unwrap() },
                std::future::pending(),
                embedded,
            )
            .await
    } else {
        server
            .serve(
                async { tokio::signal::ctrl_c().await.unwrap() },
                std::future::pending(),
            )
            .await
    };
    result
}

// This protocol belongs only to this example process, never the product router.
async fn controls(data: branvil::api::DataApi) -> anyhow::Result<()> {
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt};
    let root = std::path::PathBuf::from(std::env::var_os("BRANVIL_DATA_DIR").unwrap());
    let owner = data.owner();
    let mut lines = tokio::io::BufReader::new(tokio::io::stdin()).lines();
    while let Some(line) = lines.next_line().await? {
        let request: serde_json::Value = serde_json::from_str(&line)?;
        let field = |name: &str| request[name].as_str().unwrap();
        match field("action") {
            "check" => {
                branvil::test_support::check(
                    &data,
                    &root,
                    field("repo"),
                    field("branch"),
                    field("sha"),
                    request["conclusion"].as_str(),
                )
                .await
            }
            "merge" => {
                branvil::test_support::merge(
                    &owner,
                    field("repo"),
                    request["number"].as_u64().unwrap(),
                    field("head"),
                    field("base"),
                )
                .await
            }
            _ => anyhow::bail!("unknown test control"),
        }
        tokio::io::stdout()
            .write_all(b"BLACKBOX_CONTROL_OK\n")
            .await?;
    }
    Ok(())
}
