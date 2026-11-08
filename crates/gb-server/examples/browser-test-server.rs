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
    let (_, result) = server
        .serve(
            async { tokio::signal::ctrl_c().await.unwrap() },
            std::future::pending(),
        )
        .await;
    result
}
