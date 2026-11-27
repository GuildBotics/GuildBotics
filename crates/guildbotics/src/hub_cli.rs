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

pub async fn releases(
    repo: Option<String>,
    checkout: Option<PathBuf>,
    since: String,
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
    let result = async {
        let (base, token) = gb_server::client::local_owner().await?;
        branvil::hub::releases(&base, &token, repo.as_deref(), origin.as_deref(), &since).await
    }
    .await
    .map_err(|e| error(language, e))?;
    println!("{result}");
    Ok(())
}

pub async fn notify() -> anyhow::Result<()> {
    let language = gb_server::client::language().await;
    let mut text = String::new();
    std::io::stdin().read_to_string(&mut text)?;
    let sent = async {
        branvil::hub::notify(
            Arc::new(gb_server::branvil_vault::IdentityVault::system()),
            &gb_server::endpoint::code_host_data()?,
            &text,
        )
        .await
    }
    .await
    .map_err(|e| error(language, e))?;
    println!("{}", json!({"sent":sent}));
    Ok(())
}

/// Removes the build directories under `builds` that are not kept and that no
/// process holds. A tree in use stays until a later update finds it unused,
/// and one that cannot be removed never keeps the others.
pub fn prune(builds: &std::path::Path, keep: &[std::ffi::OsString]) -> anyhow::Result<()> {
    let mut failures = 0;
    for entry in std::fs::read_dir(builds)? {
        let removed = entry.and_then(|entry| {
            if !entry.file_type()?.is_dir() || keep.contains(&entry.file_name()) {
                return Ok(());
            }
            if !gb_base::home::remove_unused_tree(&entry.path())? {
                eprintln!(
                    "{} is in use; a later update removes it.",
                    entry.path().display()
                );
            }
            Ok(())
        });
        if let Err(error) = removed {
            eprintln!("{error}");
            failures += 1;
        }
    }
    anyhow::ensure!(failures == 0, "{failures} builds could not be removed");
    Ok(())
}

pub(crate) fn migration_version(value: &Value) -> anyhow::Result<u64> {
    anyhow::ensure!(value.is_object(), "invalid migration declaration");
    Ok(value
        .get("manual_update_version")
        .map(|v| {
            v.as_u64()
                .ok_or_else(|| anyhow::anyhow!("invalid manual_update_version"))
        })
        .transpose()?
        .unwrap_or(0))
}

pub(crate) fn migration_ticket(value: &Value) -> anyhow::Result<&str> {
    let ticket = value["instructions"]
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("manual update needs a ticket URL"))?;
    let url = reqwest::Url::parse(ticket)?;
    anyhow::ensure!(
        matches!(url.scheme(), "http" | "https")
            && url.host().is_some()
            && url.username().is_empty()
            && url.password().is_none()
            && url.query().is_none()
            && url.fragment().is_none(),
        "manual update needs an HTTP(S) ticket URL without credentials"
    );
    Ok(ticket)
}

fn declaration(text: &str) -> anyhow::Result<Value> {
    let value: Value = serde_json::from_str(text)?;
    let version = migration_version(&value)?;
    let instructions = if version == 0 {
        None
    } else {
        Some(migration_ticket(&value)?)
    };
    Ok(json!({"manual_update_version": version, "instructions": instructions}))
}

pub fn migration() -> anyhow::Result<()> {
    let value = declaration(include_str!(concat!(env!("OUT_DIR"), "/hub-update.json")))?;
    println!("{value}");
    Ok(())
}

/// Fetch and verify both clients for an exact commit before making them available.
pub async fn clients(checkout: PathBuf, sha: String) -> anyhow::Result<()> {
    let output = std::process::Command::new("git")
        .arg("-C")
        .arg(checkout)
        .args(["remote", "get-url", "origin"])
        .output()?;
    anyhow::ensure!(output.status.success(), "could not read origin");
    let origin = std::str::from_utf8(&output.stdout)?.trim();
    let (base, token) = gb_server::client::local_owner().await?;
    branvil::hub::require_verified(&base, &token, origin, &sha).await?;
    gb_server::client_release::prepare(&gb_base::home::data()?, &sha, |artifact| {
        branvil::hub::artifact(&base, &token, origin, &sha, artifact)
    })
    .await?;
    println!("{}", json!({"ready":true}));
    Ok(())
}

/// Write only public build identity beside an archived source tree.
pub fn build_info(checkout: &std::path::Path, to: &std::path::Path) -> anyhow::Result<()> {
    let git = |args: &[&str]| -> anyhow::Result<String> {
        let out = std::process::Command::new("git")
            .arg("-C")
            .arg(checkout)
            .args(args)
            .output()?;
        anyhow::ensure!(out.status.success(), "Cannot read Hub build identity");
        Ok(String::from_utf8(out.stdout)?.trim().to_owned())
    };
    let sha = git(&["rev-parse", "HEAD"])?;
    let subject = git(&["show", "-s", "--format=%s", "HEAD"])?;
    let remote = if git(&["remote"])?.lines().any(|name| name == "origin") {
        Some(git(&["remote", "get-url", "origin"])?)
    } else {
        None
    };
    let remote = remote.map(|remote| {
        if let Ok(mut url) = reqwest::Url::parse(&remote) {
            let _ = url.set_username("");
            let _ = url.set_password(None);
            url.set_query(None);
            url.set_fragment(None);
            url.to_string()
        } else {
            remote
                .rsplit_once('@')
                .map(|(_, host)| format!("git@{host}"))
                .unwrap_or(remote)
        }
    });
    gb_base::files::atomic_write(
        &to.join("hub-build.json"),
        &serde_json::to_vec(&serde_json::json!({"sha":sha,"subject":subject,"remote":remote}))?,
    )?;
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
            declaration(r#"{"manual_update_version":2,"instructions":"https://hub.example/workspaces/team/repos/project/tickets/2"}"#)
                .unwrap()["manual_update_version"],
            2
        );
        for text in [
            r#"{"manual_update_version":-1}"#,
            r#"{"manual_update_version":1}"#,
            r#"{"manual_update_version":1,"instructions":"docs/hub-server.md"}"#,
            r#"{"manual_update_version":1,"instructions":"javascript:alert(1)"}"#,
            "invalid",
        ] {
            assert!(declaration(text).is_err());
        }
    }
}
