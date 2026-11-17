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
    if arguments.first().is_some_and(|a| a == "branvil") {
        let result = tokio::task::spawn_blocking(move || {
            branvil::main(
                branvil::Invocation::under(&["branvil"]),
                Arc::new(branvil::vault::Memory::default()),
                arguments.into_iter().map(std::ffi::OsString::from),
            )
        })
        .await?;
        std::process::exit(if result == std::process::ExitCode::SUCCESS {
            0
        } else {
            1
        });
    }
    if !arguments.is_empty() {
        anyhow::ensure!(
            arguments[0] == "member",
            "unsupported test launcher command"
        );
        // The member handshake compares executable hashes. Use the production
        // member client in this binary so both ends retain the real check.
        std::process::exit(i32::from(
            gb_server::client::member::run(arguments[1..].to_vec()).await?,
        ));
    }
    let server = gb_server::Server::start_with_keychain(Arc::new(Vault::default())).await?;
    let embedded = if std::env::var_os("GUILDBOTICS_BROWSER_TEST_BRANVIL").is_some() {
        let invocation = branvil::Invocation::under(&["branvil"]);
        let vault: Arc<dyn branvil::vault::Vault> = Arc::new(branvil::vault::Memory::default());
        let init_vault = vault.clone();
        let initialized = tokio::task::spawn_blocking(move || {
            branvil::main(
                invocation,
                init_vault,
                ["branvil", "init"].map(std::ffi::OsString::from),
            )
        })
        .await?;
        anyhow::ensure!(
            initialized == std::process::ExitCode::SUCCESS,
            "test Branvil init failed"
        );
        let base = server.base_url();
        let session = server.browser_session();
        Some(async move {
            let embedded = branvil::prepare_embedded(invocation, vault, &base, session).await?;
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
