mod support;
use gb_member::{
    WriteGuard,
    git::{Guest, Output, Pending, Workspace},
};
use gb_ports::*;
use serde_json::json;
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
    process::Stdio,
    sync::Mutex,
    time::{Duration, Instant},
};
use tokio::io::AsyncWriteExt;
struct Current;
impl WriteGuard for Current {
    fn ensure_current(&self) -> gb_member::Result<()> {
        Ok(())
    }
}
type GuestCall = (PathBuf, Vec<String>, BTreeMap<String, String>);
#[derive(Default)]
struct LocalGuest {
    calls: Mutex<Vec<GuestCall>>,
    fail: Mutex<Option<String>>,
}
impl Guest for LocalGuest {
    fn run<'a>(
        &'a self,
        cwd: &'a Path,
        argv: Vec<String>,
        env: BTreeMap<String, String>,
        stdin: Vec<u8>,
        limit: usize,
    ) -> Pending<'a, Output> {
        Box::pin(async move {
            self.calls
                .lock()
                .unwrap()
                .push((cwd.to_owned(), argv.clone(), env.clone()));
            if argv.first().is_some_and(|v| v == "rm")
                && self.fail.lock().unwrap().as_deref() == Some("rm-status")
            {
                return Ok(Output {
                    code: 1,
                    ..Default::default()
                });
            }
            if argv.first().is_some_and(|v| v == "rm")
                && self.fail.lock().unwrap().as_deref() == Some("partial-rm")
            {
                let slot = cwd.join(argv.last().unwrap());
                if slot.join(".git").exists() {
                    std::fs::remove_dir_all(slot.join(".git")).unwrap();
                }
                return Err(gb_member::MemberError::Capability(
                    "injected.partial_removal",
                ));
            }
            if self
                .fail
                .lock()
                .unwrap()
                .as_ref()
                .is_some_and(|v| argv.iter().any(|a| a == v))
            {
                return Err(gb_member::MemberError::Capability("injected.failure"));
            }
            let mut child = tokio::process::Command::new(&argv[0])
                .args(&argv[1..])
                .current_dir(cwd)
                .envs(env)
                .env("GUILDBOTICS_TEST_GUEST", "1")
                .env(
                    "GIT_CONFIG_GLOBAL",
                    if cfg!(windows) { "NUL" } else { "/dev/null" },
                )
                .env("GIT_CONFIG_NOSYSTEM", "1")
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .map_err(|_| gb_member::MemberError::Capability("test.spawn"))?;
            let mut input = child.stdin.take().unwrap();
            let sending = async move {
                input.write_all(&stdin).await.unwrap();
                drop(input);
            };
            let (_, result) = tokio::join!(sending, child.wait_with_output());
            let result = result.unwrap();
            assert!(result.stdout.len() <= limit);
            Ok(Output {
                code: result.status.code().unwrap_or(1),
                stdout: result.stdout,
                stderr: result.stderr,
            })
        })
    }
}
fn git(path: &Path, args: &[&str]) -> String {
    let output = std::process::Command::new("git")
        .args(args)
        .current_dir(path)
        .env("GIT_AUTHOR_NAME", "Fixture")
        .env("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
        .env("GIT_COMMITTER_NAME", "Fixture")
        .env("GIT_COMMITTER_EMAIL", "fixture@example.invalid")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{args:?}: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8_lossy(&output.stdout).trim().into()
}
struct Fixture {
    temp: tempfile::TempDir,
    remote: PathBuf,
    seed: PathBuf,
    services: support::Probe,
    guest: LocalGuest,
}
impl Fixture {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let seed = temp.path().join("seed");
        std::fs::create_dir(&seed).unwrap();
        git(&seed, &["init", "-q", "-b", "main"]);
        std::fs::write(seed.join("readme"), "first\n").unwrap();
        git(&seed, &["add", "."]);
        git(&seed, &["commit", "-qm", "first"]);
        let remote = temp.path().join("remote.git");
        git(
            temp.path(),
            &[
                "clone",
                "-q",
                "--bare",
                seed.to_str().unwrap(),
                remote.to_str().unwrap(),
            ],
        );
        git(
            &seed,
            &["remote", "add", "origin", remote.to_str().unwrap()],
        );
        std::fs::create_dir(temp.path().join("clones")).unwrap();
        let mut services = support::Probe::new(None);
        services
            .git_urls
            .insert("owner/repo".into(), remote.to_string_lossy().into());
        services
            .git_urls
            .insert("fork/repo".into(), format!("file://{}", remote.display()));
        Self {
            temp,
            remote,
            seed,
            services,
            guest: LocalGuest::default(),
        }
    }
    fn workspace<'a>(&'a self, work: &'a str) -> Workspace<'a> {
        Workspace {
            host_root: self.temp.path().join("host"),
            clones: self.temp.path().join("clones"),
            cwd: self.temp.path().join("clones"),
            work: Some(("manual", work)),
            guest: &self.guest,
            services: &self.services,
            guard: &Current,
            name: "Member",
            email: "member@example.invalid",
            deadline: Instant::now() + Duration::from_secs(30),
            render_error: &|error| match error {
                gb_member::MemberError::Service(error) => format!(
                    "Rendered: {} {:?}",
                    error.translation_key(),
                    error.translation_args()
                ),
                other => format!("Rendered: {other}"),
            },
        }
    }
    async fn prepare(&self, work: &str, branch: &str) -> PathBuf {
        let result = self
            .workspace(work)
            .prepare("owner/repo", branch)
            .await
            .unwrap();
        PathBuf::from(result["repo_path"].as_str().unwrap())
    }
    fn stage(&self, path: &Path, value: &str) {
        std::fs::write(path.join("readme"), value).unwrap();
        git(path, &["add", "readme"]);
    }
    fn advance(&self, value: &str) -> String {
        self.stage(&self.seed, value);
        git(&self.seed, &["commit", "-qm", "remote"]);
        git(&self.seed, &["push", "-q", "origin", "main"]);
        git(&self.seed, &["rev-parse", "HEAD"])
    }
}
#[tokio::test]
async fn prepare_reuses_clean_cache_refreshes_remote_and_refuses_previous_owner() {
    let f = Fixture::new();
    let first = f.prepare("one", "ticket/1").await;
    std::fs::create_dir(first.join("target")).unwrap();
    std::fs::write(first.join("target/cache"), "keep").unwrap();
    std::fs::write(first.join(".git/info/exclude"), "target/\n").unwrap();
    let next = f.advance("new remote\n");
    let same = f.prepare("two", "ticket/2").await;
    assert_eq!(first, same);
    assert_eq!(git(&same, &["rev-parse", "HEAD"]), next);
    assert_eq!(
        std::fs::read_to_string(same.join("target/cache")).unwrap(),
        "keep"
    );
    assert!(f.workspace("one").commit(&same, "stale").await.is_err());
    assert!(f.workspace("one").push(&same).await.is_err());
}
#[tokio::test]
async fn unfinished_population_keeps_owner_index_bytes_and_chosen_branch() {
    for state in [
        "staged",
        "untracked",
        "unstaged",
        "history",
        "divergent",
        "merge",
        "cherry",
        "revert",
        "autostash",
        "rebase-merge",
        "rebase-apply",
        "sequencer",
        "bisect",
        "index.lock",
        "HEAD.lock",
        "stash",
        "worktree",
        "skip-worktree",
        "assume-unchanged",
        "detached",
        "tag",
    ] {
        let f = Fixture::new();
        let first = f.prepare("one", "ticket/1").await;
        let head = git(&first, &["rev-parse", "HEAD"]);
        match state {
            "staged" => f.stage(&first, "staged\n"),
            "untracked" => std::fs::write(first.join("untracked"), b"raw\0\xff").unwrap(),
            "unstaged" => {
                git(&first, &["checkout", "-qb", "agent/chosen"]);
                std::fs::write(first.join("readme"), b"raw\0\xff").unwrap();
            }
            "history" | "divergent" => {
                f.stage(&first, "local\n");
                git(&first, &["commit", "-qm", "local"]);
                if state == "divergent" {
                    f.advance("remote\n");
                }
            }
            "skip-worktree" | "assume-unchanged" => {
                git(&first, &["update-index", &format!("--{state}"), "readme"]);
            }
            "detached" => {
                git(&first, &["checkout", "-q", "--detach"]);
            }
            "worktree" => {
                git(
                    &first,
                    &[
                        "worktree",
                        "add",
                        "-qb",
                        "other",
                        f.temp.path().join("wt").to_str().unwrap(),
                    ],
                );
            }
            "stash" => {
                f.stage(&first, "stash\n");
                git(&first, &["stash", "push", "-q"]);
            }
            "tag" => {
                f.stage(&first, "local\n");
                git(&first, &["commit", "-qm", "local"]);
                git(&first, &["tag", "private"]);
                git(&first, &["reset", "--hard", &head]);
            }
            _ => {
                let name = match state {
                    "merge" => "MERGE_HEAD",
                    "cherry" => "CHERRY_PICK_HEAD",
                    "revert" => "REVERT_HEAD",
                    "autostash" => "MERGE_AUTOSTASH",
                    "bisect" => "BISECT_LOG",
                    v => v,
                };
                if ["rebase-merge", "rebase-apply", "sequencer"].contains(&name) {
                    std::fs::create_dir(first.join(".git").join(name)).unwrap();
                } else {
                    std::fs::write(first.join(".git").join(name), &head).unwrap();
                }
            }
        }
        let index = std::fs::read(first.join(".git/index")).unwrap();
        let before = git(&first, &["rev-parse", "HEAD"]);
        let second = f.prepare("two", "ticket/2").await;
        assert_ne!(first, second, "{state}");
        let resumed = f.prepare("one", "ticket/1").await;
        assert_eq!(resumed, first, "{state}");
        assert_eq!(git(&first, &["rev-parse", "HEAD"]), before, "{state}");
        assert_eq!(
            std::fs::read(first.join(".git/index")).unwrap(),
            index,
            "{state}"
        );
        if state == "unstaged" {
            assert_eq!(git(&first, &["branch", "--show-current"]), "agent/chosen");
            assert_eq!(std::fs::read(first.join("readme")).unwrap(), b"raw\0\xff");
        }
    }
}
#[tokio::test]
async fn publish_is_branch_only_keeps_unstaged_work_and_returns_readiness() {
    let mut f = Fixture::new();
    f.services.error = Some(ServiceError::MissingCredential {
        name: "fixture-readiness".into(),
    });
    let path = f.prepare("one", "ticket/1").await;
    f.stage(&path, "ours\n");
    std::fs::write(path.join("untracked"), "keep").unwrap();
    git(&path, &["tag", "private"]);
    git(&path, &["branch", "private"]);
    let result = f
        .workspace("one")
        .publish(&path, "message\n")
        .await
        .unwrap();
    assert_eq!(result["status"], "published");
    assert_eq!(
        result["pull_requests_error"],
        r#"Rendered: integrations.resources.missing_credential {"name": "fixture-readiness"}"#
    );

    assert_eq!(
        git(&f.remote, &["rev-parse", "refs/heads/ticket/1"]),
        result["commit_sha"]
    );
    assert_eq!(
        git(
            &f.remote,
            &["for-each-ref", "--format=%(refname)", "refs/tags/"]
        ),
        ""
    );
    assert!(!git(&f.remote, &["branch", "--format=%(refname)"]).contains("private"));
    assert!(path.join("untracked").is_file());
    assert_eq!(git(&path, &["config", "user.name"]), "Member");
    let again = f.workspace("one").push(&path).await.unwrap();
    assert_eq!(again["status"], "up_to_date");
}
#[tokio::test]
async fn failed_push_keeps_history_and_remote_rejection_is_reported() {
    let f = Fixture::new();
    let path = f.prepare("one", "main").await;
    f.stage(&path, "local\n");
    f.workspace("one").commit(&path, "local").await.unwrap();
    let expected = git(&path, &["rev-parse", "HEAD"]);
    f.advance("diverged\n");
    let error = f.workspace("one").push(&path).await.unwrap_err();
    assert_eq!(error.to_string(), "capabilities.member_git.push_failed");
    let gb_member::MemberError::CapabilityArgs { args, .. } = error else {
        panic!()
    };
    assert_eq!(args["branch"], "main");
    assert!(args["reason"].contains("non-fast-forward"));
    let other = f.prepare("two", "other").await;
    assert_ne!(other, path);
    assert_eq!(git(&path, &["rev-parse", "HEAD"]), expected);
    std::fs::remove_dir_all(&f.remote).unwrap();
    assert!(f.workspace("one").push(&path).await.is_err());
}
#[tokio::test]
async fn initialization_and_reassignment_failures_preserve_their_registration() {
    let f = Fixture::new();
    *f.guest.fail.lock().unwrap() = Some("init".into());
    assert!(
        f.workspace("one")
            .prepare("owner/repo", "ticket/1")
            .await
            .is_err()
    );
    *f.guest.fail.lock().unwrap() = None;
    let path = f.prepare("one", "ticket/1").await;
    *f.guest.fail.lock().unwrap() = Some("checkout".into());
    assert!(
        f.workspace("two")
            .prepare("owner/repo", "ticket/2")
            .await
            .is_err()
    );
    *f.guest.fail.lock().unwrap() = None;
    assert!(f.workspace("two").commit(&path, "stale").await.is_err());
    assert!(f.workspace("one").commit(&path, "noop").await.is_ok());
}
#[tokio::test]
async fn published_and_tag_history_remains_reusable_after_remote_deletion() {
    let f = Fixture::new();
    let first = f.prepare("one", "ticket/1").await;
    f.stage(&first, "published\n");
    f.workspace("one")
        .publish(&first, "published")
        .await
        .unwrap();
    git(&f.remote, &["update-ref", "-d", "refs/heads/ticket/1"]);
    let same = f.prepare("two", "ticket/1").await;
    assert_eq!(same, first);
    assert_eq!(
        git(&same, &["rev-parse", "HEAD"]),
        git(&f.remote, &["rev-parse", "main"])
    );
    git(&f.seed, &["checkout", "-qb", "tag-history"]);
    f.stage(&f.seed, "tag\n");
    git(&f.seed, &["commit", "-qm", "tag"]);
    git(&f.seed, &["tag", "public"]);
    git(&f.seed, &["push", "-q", "origin", "public"]);
    f.prepare("two", "ticket/1").await;
    let tag = git(&first, &["rev-parse", "public"]);
    git(&f.remote, &["update-ref", "-d", "refs/tags/public"]);
    git(&first, &["checkout", "-qB", "from-tag", "public"]);
    assert_eq!(f.prepare("three", "third").await, first);
    assert_eq!(git(&first, &["rev-parse", "public"]), tag);
}
#[tokio::test]
async fn idle_cleanup_retries_and_discarded_paths_cannot_publish() {
    let f = Fixture::new();
    let first = f.prepare("one", "ticket/1").await;
    f.stage(&first, "dirty\n");
    let second = f.prepare("two", "ticket/2").await;
    git(&first, &["reset", "--hard"]);
    *f.guest.fail.lock().unwrap() = Some("rm".into());
    assert_eq!(f.prepare("two", "ticket/2").await, second);
    assert!(first.is_dir());
    *f.guest.fail.lock().unwrap() = None;
    f.prepare("two", "ticket/2").await;
    assert!(!first.exists());
    git(
        f.temp.path(),
        &[
            "clone",
            "-q",
            f.remote.to_str().unwrap(),
            first.to_str().unwrap(),
        ],
    );
    f.guest.calls.lock().unwrap().clear();
    f.prepare("two", "ticket/2").await;
    assert!(first.join(".git").is_dir());
    assert!(
        !f.guest
            .calls
            .lock()
            .unwrap()
            .iter()
            .any(|(_, argv, _)| argv.first().is_some_and(|a| a == "rm"))
    );
    assert!(f.workspace("one").commit(&first, "stale").await.is_err());
}
#[tokio::test]
async fn unregistered_commit_worktree_push_and_scope_boundaries() {
    let f = Fixture::new();
    let manual = f.temp.path().join("clones/manual");
    git(
        f.temp.path(),
        &[
            "clone",
            "-q",
            f.remote.to_str().unwrap(),
            manual.to_str().unwrap(),
        ],
    );
    f.stage(&manual, "manual\n");
    assert!(
        f.workspace("one")
            .commit(&manual, "manual")
            .await
            .unwrap()
            .has_changes
    );
    assert_eq!(
        f.workspace("one")
            .push(&manual)
            .await
            .unwrap_err()
            .to_string(),
        "capabilities.member_git.checkout_required"
    );
    assert!(
        f.workspace("one")
            .publish(&f.seed, "outside")
            .await
            .is_err()
    );
    let path = f.prepare("one", "ticket/1").await;
    let wt = f.temp.path().join("clones/wt");
    git(
        &path,
        &["worktree", "add", "-qb", "feature", wt.to_str().unwrap()],
    );
    f.stage(&wt, "worktree\n");
    let receipt = f.workspace("one").publish(&wt, "feature").await.unwrap();
    assert_eq!(
        git(&f.remote, &["rev-parse", "feature"]),
        receipt["commit_sha"]
    );
    let fork = f
        .workspace("one")
        .prepare("fork/repo", "feature")
        .await
        .unwrap();
    assert!(
        f.workspace("one")
            .push(Path::new(fork["repo_path"].as_str().unwrap()))
            .await
            .is_err()
    );
}
#[tokio::test]
async fn interactive_exchange_retries_history_and_returns_only_missing_tracking() {
    let f = Fixture::new();
    let w = f.workspace("one");
    assert_eq!(
        w.client_git("identity", json!({}), None).await.unwrap().0["user"],
        "Member"
    );
    let remotes = json!([f.remote.to_str().unwrap()]);
    let metadata = json!({"remotes":remotes,"repo_path":"/never-open-on-host"});
    let (known, bytes) = w.client_git("known", metadata.clone(), None).await.unwrap();
    assert!(bytes.is_empty());
    assert!(!known["known"].as_array().unwrap().is_empty());
    git(&f.seed, &["checkout", "-qb", "feature"]);
    f.stage(&f.seed, "current\n");
    git(&f.seed, &["commit", "-qm", "current"]);
    let head = git(&f.seed, &["rev-parse", "HEAD"]);
    let mut metadata = metadata;
    metadata["branch"] = "feature".into();
    metadata["head"] = head.clone().into();
    assert_eq!(
        w.client_git("push", metadata.clone(), None)
            .await
            .unwrap()
            .0["retry"],
        true
    );
    let bundle = f.temp.path().join("in.bundle");
    git(
        &f.seed,
        &["bundle", "create", bundle.to_str().unwrap(), "feature"],
    );
    let receipt = w
        .client_git(
            "push",
            metadata.clone(),
            Some(&std::fs::read(bundle).unwrap()),
        )
        .await
        .unwrap()
        .0;
    assert_eq!(receipt["pushed"], true);
    assert_eq!(git(&f.remote, &["rev-parse", "feature"]), head);
    metadata["known"] = json!([head]);
    let (_, bytes) = w
        .client_git("tracking", metadata.clone(), None)
        .await
        .unwrap();
    assert!(bytes.is_empty());
    metadata["known"] = json!([]);
    let (tracking, bytes) = w
        .client_git("tracking", metadata.clone(), None)
        .await
        .unwrap();
    assert!(!bytes.is_empty());
    assert!(tracking["refs"]["refs/remotes/origin/feature"].is_string());
    metadata["remotes"] = json!(["https://outside.invalid/repo"]);
    assert!(w.client_git("push", metadata, None).await.is_err());
}
#[tokio::test]
async fn show_validates_literal_paths_kinds_limits_default_changes_and_failures() {
    let f = Fixture::new();
    for (name, bytes) in [
        ("literal*", b"literal".as_slice()),
        ("-option", b"dash"),
        ("empty", b""),
        ("binary", b"text\0data"),
        ("invalid", b"\xff"),
    ] {
        std::fs::write(f.seed.join(name), bytes).unwrap();
    }
    std::fs::create_dir(f.seed.join("dir")).unwrap();
    std::fs::write(f.seed.join("dir/規約"), "規約\n").unwrap();
    std::fs::write(f.seed.join("boundary"), vec![b'x'; MAX_PAGE_BYTES / 2]).unwrap();
    std::fs::write(f.seed.join("large"), vec![b'x'; MAX_PAGE_BYTES / 2 + 1]).unwrap();
    git(&f.seed, &["add", "."]);
    let blob = git(&f.seed, &["hash-object", "-w", "readme"]);
    let head = git(&f.seed, &["rev-parse", "HEAD"]);
    git(
        &f.seed,
        &[
            "update-index",
            "--add",
            "--cacheinfo",
            &format!("120000,{blob},link"),
        ],
    );
    git(
        &f.seed,
        &[
            "update-index",
            "--add",
            "--cacheinfo",
            &format!("160000,{head},module"),
        ],
    );
    git(&f.seed, &["commit", "-qm", "objects"]);
    git(&f.seed, &["push", "-q", "origin", "main"]);
    let root = f.temp.path().join("host");
    let paths = [
        "dir",
        "dir/規約",
        "literal*",
        "-option",
        "empty",
        "binary",
        "invalid",
        "large",
        "boundary",
        "link",
        "module",
        "absent",
    ]
    .map(String::from);
    let result = gb_member::git::show(
        &root,
        &f.services,
        "owner/repo",
        &paths,
        Instant::now() + Duration::from_secs(30),
    )
    .await
    .unwrap();
    let items = result["items"].as_array().unwrap();
    assert_eq!(items[0]["entries"], json!([{"name":"規約","kind":"file"}]));
    assert_eq!(items[1]["content"], "規約\n");
    assert_eq!(items[2]["content"], "literal");
    assert_eq!(items[3]["content"], "dash");
    assert_eq!(items[4]["content"], "");
    assert_eq!(items[5]["status"], "binary");
    assert_eq!(items[6]["status"], "binary");
    assert_eq!(items[7]["status"], "too_large");
    assert_eq!(
        items[8]["content"].as_str().unwrap().len(),
        MAX_PAGE_BYTES / 2
    );
    assert_eq!(items[9]["kind"], "symlink");
    assert_eq!(items[10]["kind"], "submodule");
    assert_eq!(items[11]["kind"], "missing");
    for character in ["x", "規", "\u{1}", "🌏"] {
        std::fs::write(f.seed.join("escaped"), character.repeat(MAX_PAGE_BYTES / 8)).unwrap();
        git(&f.seed, &["add", "."]);
        git(&f.seed, &["commit", "-qm", "escape"]);
        git(&f.seed, &["push", "-q", "origin", "main"]);
        let result = gb_member::git::show(
            &root,
            &f.services,
            "owner/repo",
            &vec!["escaped".into(); 20],
            Instant::now() + Duration::from_secs(30),
        )
        .await
        .unwrap();
        assert!(serde_json::to_vec(&result).unwrap().len() < MAX_PAGE_BYTES);
        assert!(
            result["items"]
                .as_array()
                .unwrap()
                .iter()
                .any(|i| i["status"] == "too_large")
        );
    }
    for paths in [
        vec![],
        vec![".".into(); MAX_PAGE_BYTES],
        vec!["../bad".into()],
        vec!["a\\b".into()],
        vec!["/bad".into()],
        vec!["a\0b".into()],
        vec!["".into()],
    ] {
        assert!(
            gb_member::git::show(
                &root,
                &f.services,
                "owner/repo",
                &paths,
                Instant::now() + Duration::from_secs(30)
            )
            .await
            .is_err()
        );
    }
    git(&f.seed, &["checkout", "-qb", "new-default"]);
    f.stage(&f.seed, "new default\n");
    git(&f.seed, &["commit", "-qm", "new default"]);
    git(&f.seed, &["push", "-q", "origin", "new-default"]);
    *f.services.git_default.lock().unwrap() = "new-default".into();
    let next = gb_member::git::show(
        &root,
        &f.services,
        "owner/repo",
        &["readme".into()],
        Instant::now() + Duration::from_secs(30),
    )
    .await
    .unwrap();
    assert_eq!(next["items"][0]["content"], "new default\n");
    std::fs::remove_dir_all(&f.remote).unwrap();
    assert!(
        gb_member::git::show(
            &root,
            &f.services,
            "owner/repo",
            &["missing".into()],
            Instant::now() + Duration::from_secs(30)
        )
        .await
        .is_err()
    );
}
#[tokio::test]
async fn changing_the_anchor_keeps_unpublished_branches_and_repository_slots_separate() {
    let f = Fixture::new();
    let path = f.prepare("one", "main").await;
    f.stage(&path, "local\n");
    f.workspace("one").commit(&path, "local").await.unwrap();
    let head = git(&path, &["rev-parse", "HEAD"]);
    let other = f.prepare("one", "other").await;
    assert_ne!(other, path);
    assert_eq!(git(&path, &["rev-parse", "main"]), head);
    let fork = f
        .workspace("one")
        .prepare("fork/repo", "main")
        .await
        .unwrap();
    assert_ne!(fork["repo_path"], other.to_string_lossy().as_ref());
    let mut missing = f.workspace("one");
    missing.work = None;
    assert!(missing.prepare("owner/repo", "main").await.is_err());
    for repo in ["owner", "owner/..", "owner//repo"] {
        assert!(f.workspace("one").prepare(repo, "main").await.is_err());
    }
}
#[cfg(unix)]
#[tokio::test]
async fn clone_programs_and_redirections_execute_only_in_the_guest_without_credentials() {
    use std::os::unix::fs::PermissionsExt;
    for plant in [
        "hook",
        "filter",
        "fsmonitor",
        "remote",
        "gitfile",
        "worktree",
        "symlink",
    ] {
        let f = Fixture::new();
        let path = f.prepare("one", "topic").await;
        let trap = f.temp.path().join("trap");
        git(
            f.temp.path(),
            &[
                "clone",
                "-q",
                f.remote.to_str().unwrap(),
                trap.to_str().unwrap(),
            ],
        );
        let marker = f.temp.path().join("host-executed");
        let script = format!(
            "#!/bin/sh\n[ -n \"$GUILDBOTICS_TEST_GUEST\" ] || touch '{}'\n[ -z \"$GIT_PASSWORD\" ] || touch '{}'\ncat\n",
            marker.display(),
            marker.display()
        );
        let executable = |p: &Path| {
            std::fs::write(p, &script).unwrap();
            std::fs::set_permissions(p, std::fs::Permissions::from_mode(0o700)).unwrap();
        };
        executable(&path.join(".git/hooks/pre-commit"));
        executable(&trap.join(".git/hooks/pre-commit"));
        match plant {
            "filter" => {
                let hook = path.join("filter");
                executable(&hook);
                git(
                    &path,
                    &["config", "filter.planted.clean", hook.to_str().unwrap()],
                );
                git(
                    &path,
                    &["config", "filter.planted.smudge", hook.to_str().unwrap()],
                );
                std::fs::write(path.join(".gitattributes"), "* filter=planted\n").unwrap();
            }
            "fsmonitor" => {
                let hook = path.join("monitor");
                executable(&hook);
                git(&path, &["config", "core.fsmonitor", hook.to_str().unwrap()]);
            }
            "remote" => {
                git(
                    &path,
                    &["remote", "set-url", "origin", trap.to_str().unwrap()],
                );
                git(
                    &path,
                    &[
                        "config",
                        &format!("url.{}.insteadOf", trap.display()),
                        f.remote.to_str().unwrap(),
                    ],
                );
            }
            "gitfile" => {
                std::fs::remove_dir_all(path.join(".git")).unwrap();
                std::fs::write(
                    path.join(".git"),
                    format!("gitdir: {}/.git\n", trap.display()),
                )
                .unwrap();
            }
            "worktree" => {
                git(&path, &["config", "core.worktree", trap.to_str().unwrap()]);
            }
            "symlink" => {
                let moved = path.with_file_name("moved");
                std::fs::rename(&path, moved).unwrap();
                std::os::unix::fs::symlink(&trap, &path).unwrap();
            }
            _ => {}
        }
        // Staging also goes through the test guest, so planted programs are never run by the fixture's host Git.
        let _ = f
            .guest
            .run(
                &path,
                vec!["git".into(), "add".into(), ".".into()],
                BTreeMap::new(),
                vec![],
                1 << 24,
            )
            .await;
        let _ = f.workspace("one").commit(&path, "planted").await;
        let _ = f.workspace("one").push(&path).await;
        let _ = f.workspace("one").publish(&path, "planted").await;
        let _ = f.workspace("one").prepare("owner/repo", "topic").await;
        assert!(!marker.exists(), "{plant}");
        for (_, _, env) in f.guest.calls.lock().unwrap().iter() {
            assert!(env.keys().all(|k| {
                [
                    "GIT_TERMINAL_PROMPT",
                    "GIT_AUTHOR_NAME",
                    "GIT_AUTHOR_EMAIL",
                    "GIT_COMMITTER_NAME",
                    "GIT_COMMITTER_EMAIL",
                ]
                .contains(&k.as_str())
            }));
        }
    }
}
#[tokio::test]
async fn commit_finishes_conflicted_merge_even_when_the_result_is_ours() {
    for ours in [true, false] {
        let f = Fixture::new();
        let path = f.prepare("one", "topic").await;
        let base = git(&path, &["rev-parse", "HEAD"]);
        git(&path, &["checkout", "-qb", "other"]);
        f.stage(&path, "theirs\n");
        git(&path, &["commit", "-qm", "theirs"]);
        let theirs = git(&path, &["rev-parse", "HEAD"]);
        git(&path, &["checkout", "-q", "topic"]);
        f.stage(&path, "ours\n");
        git(&path, &["commit", "-qm", "ours"]);
        let before = git(&path, &["rev-parse", "HEAD"]);
        let merged = std::process::Command::new("git")
            .args(["merge", "other"])
            .current_dir(&path)
            .output()
            .unwrap();
        assert!(!merged.status.success());
        f.stage(&path, if ours { "ours\n" } else { "resolved\n" });
        let result = f.workspace("one").commit(&path, "resolved").await.unwrap();
        assert!(result.has_changes);
        assert!(!path.join(".git/MERGE_HEAD").exists());
        assert_eq!(
            git(&path, &["show", "-s", "--format=%P", "HEAD"]),
            format!("{before} {theirs}")
        );
        assert_ne!(result.commit_sha.unwrap(), base);
    }
}
#[tokio::test]
async fn commits_consume_cherry_pick_and_revert_state_and_report_a_guest_hook_failure() {
    for operation in ["cherry-pick", "revert"] {
        let f = Fixture::new();
        let path = f.prepare("one", "topic").await;
        git(&path, &["checkout", "-qb", "other"]);
        f.stage(&path, "theirs\n");
        git(&path, &["commit", "-qm", "theirs"]);
        let theirs = git(&path, &["rev-parse", "HEAD"]);
        git(&path, &["checkout", "-q", "topic"]);
        f.stage(&path, "ours\n");
        git(&path, &["commit", "-qm", "ours"]);
        let ours = git(&path, &["rev-parse", "HEAD"]);
        let result = std::process::Command::new("git")
            .args([operation, &theirs])
            .current_dir(&path)
            .output()
            .unwrap();
        assert!(!result.status.success());
        f.stage(&path, "resolved\n");
        f.workspace("one").commit(&path, "finish").await.unwrap();
        assert_eq!(git(&path, &["show", "-s", "--format=%P", "HEAD"]), ours);
        for marker in ["CHERRY_PICK_HEAD", "REVERT_HEAD", "sequencer"] {
            assert!(!path.join(".git").join(marker).exists());
        }
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let f = Fixture::new();
        let path = f.prepare("one", "topic").await;
        let hook = path.join(".git/hooks/pre-commit");
        let marker = path.join("hook-ran");
        std::fs::write(&hook,format!("#!/bin/sh\nprintf '%s' \"$GUILDBOTICS_TEST_GUEST\" > '{}'\necho 'lint failed' >&2\nexit 1\n",marker.display())).unwrap();
        std::fs::set_permissions(&hook, std::fs::Permissions::from_mode(0o700)).unwrap();
        f.stage(&path, "staged\n");
        let error = f
            .workspace("one")
            .commit(&path, "refused")
            .await
            .err()
            .unwrap();
        if let gb_member::MemberError::CapabilityArgs { args, .. } = error {
            assert!(args["reason"].contains("lint failed"));
        } else {
            panic!("expected diagnostic");
        }
        assert_eq!(std::fs::read_to_string(marker).unwrap(), "1");
    }
}
#[tokio::test]
async fn public_tracking_refs_do_not_keep_idle_work_and_shallow_history_is_not_advertised() {
    let f = Fixture::new();
    let path = f.prepare("one", "main").await;
    let baseline = git(&path, &["rev-parse", "HEAD"]);
    f.stage(&path, "discarded\n");
    git(&path, &["commit", "-qm", "discarded"]);
    let discarded = git(&path, &["rev-parse", "HEAD"]);
    git(&path, &["reset", "--hard", &baseline]);
    git(&path, &["update-ref", "refs/remotes/other/old", &discarded]);
    assert_eq!(f.prepare("two", "topic").await, path);
    // A shallow guest must receive all ancestors even when it already has the tip object.
    let shallow = f.temp.path().join("clones/shallow");
    git(
        f.temp.path(),
        &[
            "clone",
            "-q",
            "--depth=1",
            &format!("file://{}", f.remote.display()),
            shallow.to_str().unwrap(),
        ],
    );
    let w = f.workspace("two");
    let origin = gb_member::git::Origin::open(
        &w.host_root,
        "owner/repo",
        f.remote.to_string_lossy().into(),
        w.deadline,
    )
    .await
    .unwrap();
    origin.fetch(None).await.unwrap();
    assert!(shallow.join(".git/shallow").exists());
    assert!(w.known(&shallow).await.unwrap().is_empty());
}
#[tokio::test]
async fn show_omits_oversized_tree_contents_and_keeps_metadata_within_budget() {
    let f = Fixture::new();
    let blob = git(&f.seed, &["hash-object", "-w", "readme"]);
    let input = (0..5000)
        .map(|n| format!("100644 blob {blob}\t{}-{n}\n", "x".repeat(80)))
        .collect::<String>();
    let result = f
        .guest
        .run(
            &f.seed,
            vec!["git".into(), "mktree".into()],
            BTreeMap::new(),
            input.into_bytes(),
            1 << 24,
        )
        .await
        .unwrap();
    assert_eq!(result.code, 0);
    let tree = String::from_utf8(result.stdout).unwrap();
    let commit = f
        .guest
        .run(
            &f.seed,
            vec![
                "git".into(),
                "-c".into(),
                "user.name=Fixture".into(),
                "-c".into(),
                "user.email=fixture@example.invalid".into(),
                "commit-tree".into(),
                tree.trim().into(),
            ],
            BTreeMap::new(),
            b"large tree\n".to_vec(),
            1 << 24,
        )
        .await
        .unwrap();
    assert_eq!(commit.code, 0);
    let sha = String::from_utf8(commit.stdout).unwrap();
    git(
        &f.remote,
        &["fetch", "-q", f.seed.to_str().unwrap(), sha.trim()],
    );
    git(&f.remote, &["update-ref", "refs/heads/main", sha.trim()]);
    let result = gb_member::git::show(
        &f.temp.path().join("host"),
        &f.services,
        "owner/repo",
        &[".".into()],
        Instant::now() + Duration::from_secs(30),
    )
    .await
    .unwrap();
    assert_eq!(result["items"][0]["status"], "too_large");
    assert!(result["items"][0].get("entries").is_none());
}
struct NoRecord;
impl gb_member::session::Control for NoRecord {
    fn command<'a>(
        &'a self,
        _: &'a gb_member::command::Parsed,
        _: &'a str,
    ) -> Pending<'a, serde_json::Value> {
        panic!("not a completion")
    }
    fn record(&self, _: &str, _: bool, _: &serde_json::Value) -> gb_member::Result<()> {
        Ok(())
    }
}
#[tokio::test]
async fn prepare_dispatch_resolves_ticket_and_fork_heads_and_rejects_invalid_anchors() {
    let mut f = Fixture::new();
    for (url, repository) in [("ticket", Some("owner/repo")), ("loose", None)] {
        let ticket = json!({"kind":"ticket","url":url,"title":"Ticket","label":"T-1","key":"T-1","tracker":"owner/repo","repository":repository,"body":"","state":"open","assignees":[],"labels":[],"lane":null,"parent":null,"children":[],"blocked_by":[],"blocks":[],"interactive":false,"interactive_ancestor":null});
        f.services.pages.insert(
            format!("tickets|{url}|"),
            Ok(ReadPage::of(Resource::Tickets, vec![ticket], None, None).unwrap()),
        );
    }
    f.services.git_head = Some(PullRequestHead {
        repository: "fork/repo".into(),
        branch: "review".into(),
    });
    git(&f.remote, &["branch", "review", "main"]);
    let roster = BTreeMap::new();
    let w = f.workspace("ticket");
    let session = gb_member::session::Session {
        person_id: "aiko",
        person_type: "agent",
        delegated_to: Some("aiko"),
        workflow: true,
        repository: gb_member::repository::Repository {
            services: &f.services,
            guard: &Current,
            roster: &roster,
        },
        chat: None,
        memory: None,
        git: Some(w),
        context: json!({}),
        control: &NoRecord,
        git_root: f.temp.path(),
    };
    let args = |parts: &[&str]| parts.iter().map(|v| (*v).into()).collect::<Vec<String>>();
    for tail in [
        vec![],
        vec!["--repo", "owner/repo"],
        vec!["--branch", "branch"],
        vec!["--issue-url", "ticket", "--repo", "fork/repo"],
        vec!["--pr-url", "pr", "--repo", "owner/repo"],
        vec!["--issue-url", "loose"],
    ] {
        let mut input = vec!["git", "prepare", "--person", "aiko"];
        input.extend(tail);
        assert!(session.execute(&args(&input), "").await.is_err());
    }
    let ticket = session
        .execute(
            &args(&[
                "git",
                "prepare",
                "--person",
                "aiko",
                "--issue-url",
                "ticket",
                "--repo",
                "owner/repo",
            ]),
            "",
        )
        .await
        .unwrap();
    assert_eq!(ticket.payload["branch"], "ticket/T-1");
    let path = PathBuf::from(ticket.payload["repo_path"].as_str().unwrap());
    f.stage(&path, "staged command body");
    let relative = path.file_name().unwrap().to_str().unwrap();
    let committed = session
        .execute(
            &args(&[
                "git",
                "commit",
                "--person",
                "aiko",
                "--repo-path",
                relative,
                "--content-stdin",
            ]),
            "entire\nmessage\n",
        )
        .await
        .unwrap();
    assert_eq!(committed.payload["status"], "committed");
    assert_eq!(git(&path, &["log", "-1", "--format=%B"]), "entire\nmessage");
    let loose = session
        .execute(
            &args(&[
                "git",
                "prepare",
                "--person",
                "aiko",
                "--issue-url",
                "loose",
                "--repo",
                "owner/repo",
            ]),
            "",
        )
        .await
        .unwrap();
    assert_eq!(loose.payload["repository"], "owner/repo");
    let pr = session
        .execute(
            &args(&[
                "git",
                "prepare",
                "--person",
                "aiko",
                "--issue-url",
                "ticket",
                "--pr-url",
                "pr",
            ]),
            "",
        )
        .await
        .unwrap();
    assert_eq!(pr.payload["repository"], "fork/repo");
    assert_eq!(pr.payload["branch"], "review");
    assert_eq!(pr.payload["mode"], "pull_request_review");
}

#[tokio::test]
async fn push_reports_readiness_for_open_prs_without_waiting_for_checks() {
    let mut f = Fixture::new();
    f.services.open = vec!["pr".into()];
    f.services.checks.insert("pr".into(),serde_json::from_value(json!({"target":{"kind":"pull_request","url":"pr","title":"T","label":"!1"},"base_sha":"base","head_sha":"head","behind_by":0,"out_of_date":false,"current_base_sha":"base","current_head_sha":"head","rollup":"pending","checks":[],"checks_expected":true,"readiness":"blocked","completion_blockers":[{"code":"checks_pending","message":"pending","next_action":"wait"}]})).unwrap());
    let path = f.prepare("one", "topic").await;
    f.stage(&path, "ours");
    let result = f.workspace("one").publish(&path, "ours").await.unwrap();
    assert_eq!(result["pushed"], true);
    assert_eq!(result["pull_requests"][0]["url"], "pr");
    assert_eq!(
        result["pull_requests"][0]["completion_blockers"][0]["code"],
        "checks_pending"
    );
}

#[tokio::test]
async fn dirty_selected_checkout_keeps_one_warm_idle_slot() {
    let f = Fixture::new();
    let first = f.prepare("one", "ticket/1").await;
    f.stage(&first, "dirty one\n");
    let second = f.prepare("two", "ticket/2").await;
    f.stage(&second, "dirty two\n");
    let third = f.prepare("three", "ticket/3").await;
    git(&second, &["reset", "--hard"]);
    assert_eq!(f.prepare("one", "ticket/1").await, first);
    assert_eq!(
        usize::from(second.exists()) + usize::from(third.exists()),
        1
    );
    let warm = if second.exists() { second } else { third };
    assert_eq!(f.prepare("four", "ticket/4").await, warm);
}
#[tokio::test]
async fn partial_cleanup_retries_without_reading_missing_git_metadata() {
    let f = Fixture::new();
    let first = f.prepare("one", "ticket/1").await;
    f.stage(&first, "dirty\n");
    let second = f.prepare("two", "ticket/2").await;
    git(&first, &["reset", "--hard"]);
    *f.guest.fail.lock().unwrap() = Some("partial-rm".into());
    assert_eq!(f.prepare("two", "ticket/2").await, second);
    assert!(first.exists());
    assert!(!first.join(".git").exists());
    *f.guest.fail.lock().unwrap() = None;
    assert_eq!(f.prepare("two", "ticket/2").await, second);
    assert!(!first.exists());
}
#[tokio::test]
async fn detached_and_unprepared_pushes_have_actionable_diagnostics() {
    let f = Fixture::new();
    assert_eq!(
        f.workspace("one")
            .push(&f.seed)
            .await
            .unwrap_err()
            .to_string(),
        "capabilities.member_git.invalid_path"
    );
    let path = f.prepare("one", "ticket/1").await;
    git(&path, &["checkout", "--detach"]);
    assert_eq!(
        f.workspace("one")
            .push(&path)
            .await
            .unwrap_err()
            .to_string(),
        "capabilities.member_git.detached"
    );
}

#[tokio::test]
async fn cleanup_nonzero_status_retries_but_success_does_not() {
    let f = Fixture::new();
    let first = f.prepare("one", "ticket/1").await;
    f.stage(&first, "dirty\n");
    f.prepare("two", "ticket/2").await;
    git(&first, &["reset", "--hard"]);
    *f.guest.fail.lock().unwrap() = Some("rm-status".into());
    f.prepare("two", "ticket/2").await;
    assert!(first.exists());
    *f.guest.fail.lock().unwrap() = None;
    f.prepare("two", "ticket/2").await;
    assert!(!first.exists());
    f.guest.calls.lock().unwrap().clear();
    f.prepare("two", "ticket/2").await;
    assert!(
        !f.guest
            .calls
            .lock()
            .unwrap()
            .iter()
            .any(|(_, argv, _)| argv.first().is_some_and(|a| a == "rm"))
    );
}

fn lifecycle_page(f: &mut Fixture, kind: &str, url: &str, state: &str, done: bool, merged: bool) {
    let resource = if kind == "ticket" {
        Resource::Tickets
    } else {
        Resource::PullRequests
    };
    let value = if kind == "ticket" {
        json!({"kind":"ticket","url":url,"title":"Ticket","label":"T","key":"1","tracker":"owner/repo","repository":"owner/repo","body":"","state":state,"assignees":[],"labels":[],"lane":if done {json!("done")} else {json!("in_progress")},"parent":null,"children":[],"blocked_by":[],"blocks":[],"interactive":false,"interactive_ancestor":null})
    } else {
        json!({"kind":"pull_request","url":url,"title":"PR","label":"P","repository":"owner/repo","body":"","state":state,"assignees":[],"author":"member","requested_reviewers":[],"labels":[],"author_type":"User","head":"feature","head_repository":"owner/repo","head_sha":"a".repeat(40),"base":"main","merged":merged,"draft":false,"changed_files":null})
    };
    f.services.pages.insert(
        format!("{}|{}|", resource.name(), url),
        Ok(ReadPage::of(resource, [value], None, None).unwrap()),
    );
}
#[tokio::test]
async fn active_work_keeps_clean_clones_and_terminal_work_releases_them() {
    for (kind, state, done, merged) in [
        ("ticket", "closed", false, false),
        ("ticket", "open", true, false),
        ("pull_request", "closed", false, true),
        ("pull_request", "closed", false, false),
    ] {
        let mut f = Fixture::new();
        lifecycle_page(&mut f, kind, "one", "open", false, false);
        lifecycle_page(&mut f, kind, "two", "open", false, false);
        let prepare = |work, branch| {
            let mut w = f.workspace(work);
            w.work = Some((kind, work));
            async move { w.prepare("owner/repo", branch).await.unwrap() }
        };
        let first = prepare("one", "ticket/1").await;
        let second = prepare("two", "ticket/2").await;
        assert_ne!(first["repo_path"], second["repo_path"]);
        for _ in 0..2 {
            assert_eq!(
                prepare("one", "ticket/1").await["repo_path"],
                first["repo_path"]
            );
            assert_eq!(
                prepare("two", "ticket/2").await["repo_path"],
                second["repo_path"]
            );
        }
        assert_eq!(
            git(
                Path::new(first["repo_path"].as_str().unwrap()),
                &["branch", "--show-current"]
            ),
            "ticket/1"
        );
        lifecycle_page(&mut f, kind, "one", state, done, merged);
        let next = f.prepare("three", "ticket/3").await;
        assert_eq!(next.to_str().unwrap(), first["repo_path"].as_str().unwrap());
        assert!(Path::new(second["repo_path"].as_str().unwrap()).exists());
    }
}

#[tokio::test]
async fn excluded_work_retains_git_based_reuse() {
    for kind in [
        "pull_request_review",
        "ticket_mention",
        "chat",
        "manual",
        "routine",
    ] {
        let f = Fixture::new();
        let mut w = f.workspace("one");
        w.work = Some((kind, "one"));
        let first = w.prepare("owner/repo", "first").await.unwrap();
        assert_eq!(
            f.prepare("two", "second").await.to_str().unwrap(),
            first["repo_path"]
        );
    }
}
#[tokio::test]
async fn active_clones_survive_cleanup_with_one_idle_clone_per_repository() {
    let mut f = Fixture::new();
    for repo in ["owner/repo", "fork/repo"] {
        lifecycle_page(&mut f, "ticket", repo, "open", false, false);
    }
    let mut active = vec![];
    for repo in ["owner/repo", "fork/repo"] {
        let mut w = f.workspace(repo);
        w.work = Some(("ticket", repo));
        active.push(w.prepare(repo, "active").await.unwrap());
    }
    let mut idle = vec![];
    for repo in ["owner/repo", "fork/repo"] {
        let mut paths = vec![];
        for work in ["one", "two", "three"] {
            let value = f.workspace(work).prepare(repo, work).await.unwrap();
            let path = PathBuf::from(value["repo_path"].as_str().unwrap());
            std::fs::write(path.join("dirty"), "keep").unwrap();
            paths.push(path);
        }
        for path in &paths {
            std::fs::remove_file(path.join("dirty")).unwrap();
        }
        let selected = f.workspace("four").prepare(repo, "four").await.unwrap();
        let remaining: Vec<_> = paths.into_iter().filter(|p| p.exists()).collect();
        assert_eq!(remaining.len(), 1);
        assert_eq!(remaining[0].to_str().unwrap(), selected["repo_path"]);
        idle.push(remaining[0].clone());
    }
    assert!(
        active
            .iter()
            .all(|v| Path::new(v["repo_path"].as_str().unwrap()).exists())
    );
    assert!(idle.iter().all(|p| p.exists()));
}

#[tokio::test]
async fn author_feedback_keeps_the_ticket_clone_after_the_agent_chooses_a_branch() {
    let mut f = Fixture::new();
    lifecycle_page(&mut f, "ticket", "ticket", "open", false, false);
    lifecycle_page(&mut f, "pull_request", "pr", "closed", false, false);
    let mut w = f.workspace("ticket");
    w.work = Some(("ticket", "ticket"));
    let first = w.prepare("owner/repo", "ticket/1").await.unwrap();
    let path = Path::new(first["repo_path"].as_str().unwrap());
    git(path, &["switch", "-c", "agent/chosen"]);
    let feedback = w.prepare("owner/repo", "agent/chosen").await.unwrap();
    assert_eq!(feedback["repo_path"], first["repo_path"]);
    assert_eq!(feedback["branch"], "agent/chosen");
    assert_ne!(f.prepare("other", "other").await, path);
}
