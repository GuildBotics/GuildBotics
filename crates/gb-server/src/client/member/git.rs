//! A current checkout stays on the CLI machine. The backend receives a
//! bundle, applies its write scope, and handles remote credentials there.
use std::{
    path::{Path, PathBuf},
    process::{Output, Stdio},
};

use serde_json::{Value, json};
use tokio::io::AsyncWriteExt;

use super::{Client, MemberRequest, size};
use crate::member::Phase;

struct Checkout(PathBuf);

impl Checkout {
    async fn open(path: &Path, client: &Client) -> anyhow::Result<Self> {
        let cwd = Self(std::env::current_dir()?);
        let root = cwd
            .text(&["rev-parse", "--show-toplevel"])
            .await
            .map_err(|_| client.git_error("current_repository", &[]))?;
        let root = std::fs::canonicalize(root)?;
        let requested = gb_base::host_path::normalize(path)?;
        let requested = std::fs::canonicalize(requested)?;
        anyhow::ensure!(
            root == requested,
            client.git_error("current_path", &[("path", &root.to_string_lossy())]),
        );
        Ok(Self(root))
    }

    async fn call(&self, args: &[&str], input: &[u8]) -> anyhow::Result<Output> {
        let mut process = tokio::process::Command::new("git")
            .args(args)
            .current_dir(&self.0)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()?;
        let mut pipe = process.stdin.take().expect("piped stdin");
        let ((), output) = tokio::try_join!(
            async move {
                if !input.is_empty() {
                    pipe.write_all(input).await?;
                }
                Ok(())
            },
            process.wait_with_output(),
        )?;
        Ok(output)
    }

    async fn checked(&self, args: &[&str], input: &[u8]) -> anyhow::Result<String> {
        let output = self.call(args, input).await?;
        anyhow::ensure!(
            output.status.success(),
            String::from_utf8_lossy(&output.stderr).trim().to_owned()
        );
        Ok(String::from_utf8(output.stdout)?.trim_end().to_owned())
    }

    async fn text(&self, args: &[&str]) -> anyhow::Result<String> {
        self.checked(args, b"").await
    }

    async fn reference(&self) -> anyhow::Result<String> {
        let branch = self
            .text(&["symbolic-ref", "--quiet", "--short", "HEAD"])
            .await?;
        self.text(&["check-ref-format", "--branch", &branch])
            .await?;
        Ok(branch)
    }

    async fn commit(
        &self,
        message: &str,
        identity: &Value,
        client: &Client,
    ) -> anyhow::Result<Value> {
        anyhow::ensure!(
            !message.trim().is_empty(),
            client.git_error("empty_commit", &[])
        );
        let user = identity["user"]
            .as_str()
            .ok_or_else(|| client.invalid_response())?;
        let email = identity["email"]
            .as_str()
            .ok_or_else(|| client.invalid_response())?;
        let branch = self.reference().await?;
        let staged = self.call(&["diff", "--cached", "--quiet"], b"").await?;
        let staged = match staged.status.code() {
            Some(0) => false,
            Some(1) => true,
            _ => anyhow::bail!(String::from_utf8_lossy(&staged.stderr).trim().to_owned()),
        };
        let merge = self
            .call(&["rev-parse", "-q", "--verify", "MERGE_HEAD"], b"")
            .await?
            .status
            .success();
        let sha = if staged || merge {
            self.checked(
                &[
                    "-c",
                    &format!("user.name={user}"),
                    "-c",
                    &format!("user.email={email}"),
                    "commit",
                    "--no-gpg-sign",
                    "--file=-",
                ],
                format!("{}\n", message.trim()).as_bytes(),
            )
            .await?;
            Some(self.text(&["rev-parse", "--verify", "HEAD"]).await?)
        } else {
            None
        };
        Ok(json!({
            "repo_path": self.0, "branch": branch, "commit_sha": sha,
            "has_changes": sha.is_some(), "status": if sha.is_some() { "committed" } else { "nothing_staged" },
        }))
    }

    async fn bundle(&self, reference: &str, known: &[String], path: &str) -> anyhow::Result<bool> {
        let candidates = known
            .iter()
            .map(|sha| gb_member::git::object(sha))
            .collect::<Result<Vec<_>, _>>()?;
        let present = self
            .checked(
                &["cat-file", "--batch-check=%(objectname)"],
                (candidates.join("\n") + "\n").as_bytes(),
            )
            .await?;
        let held = present
            .lines()
            .filter(|sha| candidates.contains(sha))
            .collect::<Vec<_>>();
        let mut revisions = vec!["rev-list", "--max-count=1", reference, "--not"];
        revisions.extend(&held);
        revisions.push("--");
        if self.text(&revisions).await?.is_empty() {
            return Ok(false);
        }
        let revisions = std::iter::once(reference.to_owned())
            .chain(held.into_iter().map(|sha| format!("^{sha}")))
            .collect::<Vec<_>>();
        self.checked(
            &["bundle", "create", "-q", path, "--stdin"],
            (revisions.join("\n") + "\n").as_bytes(),
        )
        .await?;
        Ok(true)
    }

    async fn known(&self) -> anyhow::Result<Vec<String>> {
        if self.text(&["rev-parse", "--is-shallow-repository"]).await? == "true" {
            return Ok(vec![]);
        }
        Ok(self
            .text(&["for-each-ref", "--format=%(objectname) %(objecttype)"])
            .await?
            .lines()
            .filter_map(|line| line.strip_suffix(" commit"))
            .map(str::to_owned)
            .collect())
    }

    async fn track(
        &self,
        refs: &Value,
        bundle: Option<&str>,
        client: &Client,
    ) -> anyhow::Result<bool> {
        let mut updates = String::new();
        for (reference, sha) in refs.as_object().ok_or_else(|| client.invalid_response())? {
            anyhow::ensure!(
                reference.starts_with("refs/remotes/origin/"),
                client.invalid_response()
            );
            self.text(&["check-ref-format", reference]).await?;
            let sha = sha.as_str().ok_or_else(|| client.invalid_response())?;
            let sha = gb_member::git::object(sha)?;
            updates.push_str(&format!("update {reference} {sha}\n"));
        }
        if let Some(bundle) = bundle {
            if !self
                .call(&["bundle", "verify", bundle], b"")
                .await?
                .status
                .success()
            {
                return Ok(false);
            }
            self.text(&["bundle", "unbundle", bundle]).await?;
        }
        self.checked(&["update-ref", "--no-deref", "--stdin"], updates.as_bytes())
            .await?;
        Ok(true)
    }
}

pub(super) async fn run(
    client: &Client,
    mut request: MemberRequest,
    plan: &Value,
) -> anyhow::Result<Value> {
    let operation = plan["operation"]
        .as_str()
        .ok_or_else(|| client.invalid_response())?;
    let path = plan["repo_path"]
        .as_str()
        .ok_or_else(|| client.invalid_response())?;
    let checkout = Checkout::open(Path::new(path), client).await?;
    let commit = if matches!(operation, "commit" | "publish") {
        let identity = client
            .phase(&mut request, Phase::Identity, json!({}), None, None)
            .await?;
        let commit = checkout.commit(&request.stdin, &identity, client).await?;
        let recorded = client
            .phase(&mut request, Phase::Record, commit, None, None)
            .await?;
        if operation == "commit" {
            return Ok(recorded);
        }
        Some(recorded)
    } else {
        None
    };
    let branch = checkout.reference().await?;
    let reference = format!("refs/heads/{branch}");
    let head = checkout
        .text(&[
            "rev-parse",
            "-q",
            "--verify",
            &format!("{reference}^{{commit}}"),
        ])
        .await?;
    let remotes = checkout
        .text(&["remote", "get-url", "--push", "--all", "origin"])
        .await?;
    anyhow::ensure!(
        !remotes.lines().any(|remote| {
            url::Url::parse(remote).is_ok_and(|url| {
                matches!(url.scheme(), "http" | "https")
                    && (!url.username().is_empty() || url.password().is_some())
            })
        }),
        client.git_error("credentials_in_remote", &[]),
    );
    let data = json!({
        "remotes": remotes.lines().collect::<Vec<_>>(),
        "branch": branch, "head": head, "repo_path": checkout.0,
    });
    let known = client
        .phase(&mut request, Phase::Known, data.clone(), None, None)
        .await?;
    let known: Vec<String> = serde_json::from_value(known["known"].clone())?;
    let temporary = tempfile::tempdir()?;
    let upload = temporary.path().join("upload.bundle");
    let tracking = temporary.path().join("tracking.bundle");
    let upload_name = upload.to_str().ok_or_else(|| client.invalid_response())?;
    let tracking_name = tracking.to_str().ok_or_else(|| client.invalid_response())?;

    let pushed = async {
        for held in [&known[..], &[]] {
            let exists = checkout.bundle(&reference, held, upload_name).await?;
            let receipt = client
                .phase(
                    &mut request,
                    Phase::Push,
                    data.clone(),
                    exists.then_some(upload.as_path()),
                    None,
                )
                .await?;
            if receipt["retry"] != true {
                return Ok(receipt);
            }
            if exists {
                tokio::fs::remove_file(&upload).await?;
            }
        }
        Err(client.invalid_response())
    }
    .await;
    let tracked = async {
        let mut known = checkout.known().await?;
        for _ in 0..2 {
            let mut data = data.clone();
            data["known"] = json!(known);
            let refs = client
                .phase(&mut request, Phase::Tracking, data, None, Some(&tracking))
                .await?;
            let bundle = (tokio::fs::metadata(&tracking).await?.len() > 0).then_some(tracking_name);
            if checkout.track(&refs["refs"], bundle, client).await? {
                return Ok(());
            }
            known.clear();
        }
        Err(client.invalid_response())
    }
    .await;
    let mut pushed = pushed?;
    tracked?;
    if let Some(commit) = commit {
        let status = if !commit["commit_sha"].is_null() || pushed["pushed"] == true {
            "published"
        } else {
            "up_to_date"
        };
        pushed["commit_sha"] = commit["commit_sha"].clone();
        pushed["has_changes"] = commit["has_changes"].clone();
        pushed["status"] = status.into();
    }
    Ok(pushed)
}

impl Client {
    fn git_error(&self, key: &str, params: &[(&str, &str)]) -> anyhow::Error {
        anyhow::anyhow!(crate::i18n::t(
            self.language,
            &format!("cli.member.client.{key}"),
            params
        ))
    }

    async fn phase(
        &self,
        request: &mut MemberRequest,
        phase: Phase,
        data: Value,
        upload: Option<&Path>,
        download: Option<&Path>,
    ) -> anyhow::Result<Value> {
        use futures_util::{StreamExt, stream};
        use tokio_util::io::ReaderStream;

        request.phase = Some(phase);
        request.data = serde_json::from_value(data)?;
        let mut metadata = serde_json::to_vec(&request)?;
        metadata.push(b'\n');
        let mut call = self.owner.request(reqwest::Method::POST, "/member/git");
        if let Some(path) = upload {
            let file = tokio::fs::File::open(path).await?;
            anyhow::ensure!(
                file.metadata().await?.len() <= gb_member::git::MAX_BUNDLE as u64,
                self.invalid_response()
            );
            let metadata = stream::once(async { Ok(metadata.into()) });
            call = call.body(reqwest::Body::wrap_stream(
                metadata.chain(ReaderStream::new(file)),
            ));
        } else {
            call = call.body(metadata);
        }
        let response = super::checked(call.send().await?).await?;
        let (result, mut stream) = self.header(response).await?;
        if result.exit_code != 0 {
            anyhow::bail!(result.stderr.trim_end().to_owned());
        }
        if let Some(path) = download {
            let mut file = tokio::fs::File::create(path).await?;
            let size = size(&result)?;
            anyhow::ensure!(
                size <= gb_member::git::MAX_BUNDLE && stream.prefix.len() <= size,
                self.invalid_response()
            );
            let mut transferred = stream.prefix.len();
            file.write_all(&stream.prefix).await?;
            while let Some(chunk) = stream.response.chunk().await? {
                transferred += chunk.len();
                anyhow::ensure!(transferred <= size, self.invalid_response());
                file.write_all(&chunk).await?;
            }
            anyhow::ensure!(transferred == size, self.invalid_response());
            file.flush().await?;
        } else {
            self.payload(&mut stream, &result, 0).await?;
        }
        Ok(serde_json::from_str(&result.stdout)?)
    }
}
