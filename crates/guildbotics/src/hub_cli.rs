//! Machine-readable commands for the Hub update script.

use serde_json::{Value, json};
use std::{io::Read, path::PathBuf, sync::Arc};

fn error(language: &str, reason: impl std::fmt::Display) -> anyhow::Error {
    anyhow::anyhow!(gb_server::i18n::t(
        language,
        "runtime.service_lock.hub_operation_failed",
        &[("reason", &reason.to_string())]
    ))
}

pub async fn checks(
    repo: Option<String>,
    checkout: Option<PathBuf>,
    sha: String,
) -> anyhow::Result<()> {
    let language = gb_server::client::language().await;
    let origin = checkout
        .map(|path| -> anyhow::Result<String> {
            let output = std::process::Command::new("git")
                .arg("-C")
                .arg(path)
                .args(["remote", "get-url", "origin"])
                .output()?;
            // Git diagnostics can contain credential-bearing origins.
            anyhow::ensure!(output.status.success(), "could not read checkout origin");
            Ok(String::from_utf8(output.stdout)?)
        })
        .transpose()
        .map_err(|e| error(language, e))?;
    let result = branvil::hub::checks(repo.as_deref(), origin.as_deref(), &sha)
        .await
        .map_err(|e| error(language, e))?;
    println!("{result}");
    Ok(())
}

pub async fn notify() -> anyhow::Result<()> {
    let language = gb_server::client::language().await;
    let mut text = String::new();
    std::io::stdin().read_to_string(&mut text)?;
    let sent = branvil::hub::notify(
        Arc::new(gb_server::branvil_vault::IdentityVault::system()),
        &text,
    )
    .await
    .map_err(|e| error(language, e))?;
    println!("{}", json!({"sent":sent}));
    Ok(())
}

fn declaration(text: &str) -> anyhow::Result<Value> {
    let value: Value = serde_json::from_str(text)?;
    let version = value
        .get("manual_update_version")
        .map(|v| {
            v.as_u64()
                .ok_or_else(|| anyhow::anyhow!("invalid manual_update_version"))
        })
        .transpose()?
        .unwrap_or(0);
    let instructions = value.get("instructions").and_then(Value::as_str);
    anyhow::ensure!(
        version == 0 || instructions.is_some_and(|p| p.starts_with("docs/") && !p.contains("..")),
        "manual update needs a document under docs/"
    );
    Ok(json!({"manual_update_version": version, "instructions": instructions}))
}

pub fn migration() -> anyhow::Result<()> {
    let value = declaration(include_str!(concat!(env!("OUT_DIR"), "/hub-update.json")))?;
    println!("{value}");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn declarations_have_zero_default_and_require_instructions_for_manual_steps() {
        assert_eq!(
            declaration("{}").unwrap(),
            json!({"manual_update_version":0,"instructions":null})
        );
        assert_eq!(
            declaration(r#"{"manual_update_version":2,"instructions":"docs/hub-server.md"}"#)
                .unwrap()["manual_update_version"],
            2
        );
        for text in [
            r#"{"manual_update_version":-1}"#,
            r#"{"manual_update_version":1}"#,
            "invalid",
        ] {
            assert!(declaration(text).is_err());
        }
    }
}
