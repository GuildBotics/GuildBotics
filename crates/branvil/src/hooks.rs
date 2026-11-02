//! Server-side Git hooks owned by Branvil.
//!
//! Every managed repository's `core.hooksPath` points at `<data>/hooks`, and
//! Smart HTTP sets the same value through the Git child's environment, so the
//! repository's own `hooks/` directory never runs. The hook scripts exec the
//! running `branvil` binary (`branvil hook pre-receive|post-receive`), which
//! calls back to the service on loopback with a per-process secret passed in
//! the Git child's environment:
//!
//! - pre-receive asks the service whether the pushing member may update the
//!   refs (member kinds, protected branches, internal refs); it fails closed.
//! - post-receive asks the service to record the repository's refs. If the
//!   service cannot be reached, the startup rescan records them later.

use std::io::Read;
use std::path::Path;

use serde::{Deserialize, Serialize};

use crate::config::Layout;
use crate::refs::Update;

pub const ENV_URL: &str = "BRANVIL_HOOK_URL";
pub const ENV_SECRET: &str = "BRANVIL_HOOK_SECRET";
pub const ENV_REPO: &str = "BRANVIL_HOOK_REPO";
pub const ENV_MEMBER: &str = "BRANVIL_HOOK_MEMBER";
pub const SECRET_HEADER: &str = "x-branvil-hook";

/// Writes the hook scripts for the running executable.
pub fn install(layout: &Layout) -> anyhow::Result<()> {
    let exe = std::env::current_exe()?;
    let exe = exe.to_string_lossy().replace('\'', r"'\''");
    std::fs::create_dir_all(layout.hooks())?;
    for hook in ["pre-receive", "post-receive"] {
        let path = layout.hooks().join(hook);
        let script = format!(
            "#!/bin/sh\n# Written by Branvil at startup. Repository hooks never run.\nexec '{exe}' hook {hook}\n"
        );
        std::fs::write(&path, script)?;
        make_executable(&path)?;
    }
    Ok(())
}

#[cfg(unix)]
fn make_executable(path: &Path) -> anyhow::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755))?;
    Ok(())
}

#[cfg(not(unix))]
fn make_executable(_path: &Path) -> anyhow::Result<()> {
    Ok(())
}

#[derive(Serialize, Deserialize, Debug)]
pub struct PreReceive {
    pub repo: String,
    pub member: String,
    pub updates: Vec<Update>,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct PostReceive {
    pub repo: String,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct HookAnswer {
    pub ok: bool,
    pub message: String,
}

/// `branvil hook <name>`: runs inside `git receive-pack`. Returns the exit code.
pub async fn run_hook(name: &str) -> i32 {
    let env = |k: &str| std::env::var(k).unwrap_or_default();
    let (url, secret, repo, member) = (
        env(ENV_URL),
        env(ENV_SECRET),
        env(ENV_REPO),
        env(ENV_MEMBER),
    );
    let pre = name == "pre-receive";
    if url.is_empty() || secret.is_empty() || repo.is_empty() {
        eprintln!("branvil: this push did not come through Branvil; refused.");
        return if pre { 1 } else { 0 };
    }
    let mut input = String::new();
    if std::io::stdin().read_to_string(&mut input).is_err() {
        eprintln!("branvil: cannot read the hook input.");
        return 1;
    }
    let client = reqwest::Client::new();
    let result = if pre {
        let updates = input
            .lines()
            .filter_map(|l| {
                let mut parts = l.split_whitespace();
                Some(Update {
                    old: parts.next()?.to_string(),
                    new: parts.next()?.to_string(),
                    refname: parts.next()?.to_string(),
                })
            })
            .collect();
        client
            .post(format!("{url}/-/hooks/pre-receive"))
            .header(SECRET_HEADER, &secret)
            .json(&PreReceive {
                repo,
                member,
                updates,
            })
            .send()
            .await
    } else {
        client
            .post(format!("{url}/-/hooks/post-receive"))
            .header(SECRET_HEADER, &secret)
            .json(&PostReceive { repo })
            .send()
            .await
    };
    match result {
        Ok(resp) => {
            let answer: Option<HookAnswer> = resp.json().await.ok();
            match answer {
                Some(a) if a.ok => 0,
                Some(a) => {
                    eprintln!("branvil: {}", a.message);
                    if pre { 1 } else { 0 }
                }
                None => {
                    eprintln!("branvil: unexpected answer from the service.");
                    if pre { 1 } else { 0 }
                }
            }
        }
        Err(_) => {
            if pre {
                eprintln!("branvil: the service cannot be reached; push refused.");
                1
            } else {
                eprintln!(
                    "branvil: the service cannot be reached; it records this push when it restarts."
                );
                0
            }
        }
    }
}
