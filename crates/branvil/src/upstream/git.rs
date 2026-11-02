//! The Git commands that talk to the upstream host and how their results are
//! read.
//!
//! Every command runs through `Git::run_env` (sanitized environment, empty
//! global configuration, time limit) with `credential.helper` cleared, so no
//! system or user credential store is consulted or written. The token reaches
//! Git only through `GIT_ASKPASS`, which is this executable answering from its
//! environment (`askpass_main`).

use std::path::Path;

use crate::git::Git;

const ENV_ASKPASS: &str = "BRANVIL_ASKPASS";
const ENV_USERNAME: &str = "BRANVIL_UPSTREAM_USERNAME";
const ENV_PASSWORD: &str = "BRANVIL_UPSTREAM_PASSWORD";
/// Messages kept from Git.
const MAX_ERROR: usize = 600;

/// `branvil` started by Git as `GIT_ASKPASS`: answers the username or the
/// password from the environment Branvil gave the Git child. Returns `None`
/// when this process is not an askpass call.
pub fn askpass_main() -> Option<std::process::ExitCode> {
    std::env::var_os(ENV_ASKPASS)?;
    let args: Vec<String> = std::env::args().collect();
    let prompt = match args.as_slice() {
        [_, prompt] if prompt.starts_with("Username") || prompt.starts_with("Password") => {
            prompt.clone()
        }
        _ => return None,
    };
    let value = std::env::var(if prompt.starts_with("Username") {
        ENV_USERNAME
    } else {
        ENV_PASSWORD
    })
    .unwrap_or_default();
    if value.is_empty() {
        // No token: fail instead of asking anyone.
        return Some(std::process::ExitCode::FAILURE);
    }
    println!("{value}");
    Some(std::process::ExitCode::SUCCESS)
}

/// Who Git authenticates as.
pub struct Login<'a> {
    pub username: &'a str,
    pub token: &'a str,
}

/// Keeps Git's message short and free of the token.
pub fn clean(text: &str, token: &str) -> String {
    let mut out = text.trim().to_string();
    if !token.is_empty() {
        out = out.replace(token, "***");
    }
    if out.len() > MAX_ERROR {
        let mut cut = MAX_ERROR;
        while !out.is_char_boundary(cut) {
            cut -= 1;
        }
        out.truncate(cut);
        out.push('…');
    }
    out
}

async fn run(
    git: &Git,
    dir: &Path,
    args: &[&str],
    login: &Login<'_>,
) -> Result<crate::git::Output, String> {
    let exe = std::env::current_exe()
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_default();
    let env = [
        ("GIT_ASKPASS", exe.as_str()),
        (ENV_ASKPASS, "1"),
        (ENV_USERNAME, login.username),
        (ENV_PASSWORD, login.token),
        // SSH is not offered, but a URL that names it must never prompt.
        ("GIT_SSH_COMMAND", "ssh -o BatchMode=yes"),
    ];
    let mut full = vec!["-c", "credential.helper=", "-c", "core.askPass="];
    full.extend_from_slice(args);
    git.run_env(Some(dir), &full, &env)
        .await
        .map_err(|e| clean(&e.message, login.token))
}

/// The most useful line of a failed command's stderr.
fn failure_message(stderr: &str, token: &str) -> String {
    let line = stderr
        .lines()
        .map(str::trim)
        .filter(|l| l.starts_with("fatal:") || l.starts_with("error:"))
        .find(|l| !l.contains("failed to push some refs"))
        .or_else(|| stderr.lines().map(str::trim).find(|l| !l.is_empty()))
        .unwrap_or("git failed without a message");
    clean(line, token)
}

/// What a push did.
#[derive(Debug, Clone, PartialEq)]
pub enum Pushed {
    Done,
    /// The host has something Branvil did not put there, or refused the
    /// update for good (a protection rule): retrying does not help.
    Refused(String),
    /// Anything else (network, credential): the next poll retries.
    Failed(String),
}

/// Pushes `sha` to `refs/heads/<branch>`, replacing only `lease` (what
/// Branvil pushed before; `""` means the branch must not exist).
pub async fn push(
    git: &Git,
    dir: &Path,
    url: &str,
    login: &Login<'_>,
    sha: &str,
    branch: &str,
    lease: &str,
) -> Pushed {
    let refname = format!("refs/heads/{branch}");
    let lease = format!("--force-with-lease={refname}:{lease}");
    let spec = format!("{sha}:{refname}");
    let args = [
        "push",
        "--porcelain",
        "--no-verify",
        &lease,
        "--",
        url,
        &spec,
    ];
    let out = match run(git, dir, &args, login).await {
        Ok(out) => out,
        Err(message) => return Pushed::Failed(message),
    };
    let line = out.text().lines().find_map(|line| {
        let mut fields = line.splitn(3, '\t');
        let flag = fields.next()?;
        let spec = fields.next()?;
        (spec.rsplit_once(':').map(|(_, to)| to) == Some(refname.as_str()))
            .then(|| (flag.to_string(), fields.next().unwrap_or("").to_string()))
    });
    match line {
        Some((flag, _)) if [" ", "*", "+", "="].contains(&flag.as_str()) => Pushed::Done,
        Some((flag, summary)) if flag == "!" => Pushed::Refused(clean(
            &format!("The host refused the branch {branch}: {summary}."),
            login.token,
        )),
        _ if out.ok() => Pushed::Done,
        _ => Pushed::Failed(failure_message(&out.stderr, login.token)),
    }
}

/// Fetches the host's `branch` into `into` (a hidden ref).
pub async fn fetch(
    git: &Git,
    dir: &Path,
    url: &str,
    login: &Login<'_>,
    branch: &str,
    into: &str,
) -> Result<(), String> {
    let spec = format!("+refs/heads/{branch}:{into}");
    let args = ["fetch", "--no-tags", "--quiet", "--", url, &spec];
    let out = run(git, dir, &args, login).await?;
    if out.ok() {
        Ok(())
    } else {
        Err(failure_message(&out.stderr, login.token))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn messages_are_cut_and_never_show_the_token() {
        assert_eq!(
            clean(" fatal: bad s3cr3t value ", "s3cr3t"),
            "fatal: bad *** value"
        );
        assert!(clean(&"x".repeat(5000), "t").len() < 700);
    }

    #[test]
    fn failures_skip_the_summary_line() {
        let err = "error: failed to push some refs to 'x'\nfatal: Authentication failed for 'https://h/x.git/'\n";
        assert_eq!(
            failure_message(err, ""),
            "fatal: Authentication failed for 'https://h/x.git/'"
        );
    }
}
