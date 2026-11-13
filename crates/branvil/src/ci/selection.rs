//! PR-head job selection. Git owns pathspec matching; the queue receives a
//! snapshot, so admission never needs Git or mutable PR state.

use std::collections::BTreeMap;

use super::Ci;
use super::queue::{self, JobSelection, PipelineState, SelectionBase};
use crate::error::{AppError, AppResult};
use crate::model::Repo;
use crate::util;

impl Ci {
    pub(super) async fn select_jobs(
        &self,
        repo: &Repo,
        sha: &str,
        pipeline: &PipelineState,
        all_jobs: bool,
    ) -> AppResult<Option<JobSelection>> {
        if all_jobs {
            return Ok(None);
        }
        let PipelineState::Jobs { jobs } = pipeline else {
            return Ok(None);
        };
        let (repo_copy, commit) = (repo.clone(), sha.to_string());
        let (pulls, previous) = self
            .inner
            .store
            .read(move |r| {
                Ok((
                    crate::domain::pulls::open_pulls(r, &repo_copy)?,
                    queue::get_run(r, &repo_copy.id, &commit)?,
                ))
            })
            .await?;
        // A protected registration permanently widens this run to all jobs.
        if previous
            .as_ref()
            .is_some_and(|r| r.cache_scope == "protected")
        {
            return Ok(None);
        }
        let dir = self.inner.git.repo_dir(&repo.path);
        let tips = self.inner.git.branch_tips(&dir).await?;
        let mut bases: Vec<SelectionBase> = pulls
            .into_iter()
            .filter(|p| tips.get(&p.head).is_some_and(|tip| tip == sha))
            .map(|p| SelectionBase {
                pull: p.number,
                base_sha: tips.get(&p.base).cloned().unwrap_or_default(),
                base: p.base,
                merge_base: None,
            })
            .collect();
        bases.sort_by_key(|b| b.pull);
        if bases.is_empty() {
            return Ok(None);
        }
        if let Some(run) = &previous
            && run.pipeline == *pipeline
            && let Some(selection) = &run.selection
            && selection.bases.len() == bases.len()
            && selection
                .bases
                .iter()
                .zip(&bases)
                .all(|(a, b)| a.pull == b.pull && a.base == b.base && a.base_sha == b.base_sha)
        {
            return Ok(Some(selection.clone()));
        }
        Ok(Some(self.select_bases(repo, sha, jobs, bases).await?))
    }

    async fn select_bases(
        &self,
        repo: &Repo,
        sha: &str,
        jobs: &[super::pipeline::JobDef],
        mut bases: Vec<SelectionBase>,
    ) -> AppResult<JobSelection> {
        let dir = self.inner.git.repo_dir(&repo.path);
        let mut all = false;
        for base in &mut bases {
            if base.base_sha.is_empty() {
                all = true;
                continue;
            }
            let out = self
                .inner
                .git
                .run(Some(&dir), &["merge-base", &base.base_sha, sha])
                .await?;
            match out.status {
                0 => base.merge_base = Some(out.text().trim().to_string()),
                1 => {
                    all = true;
                    continue;
                }
                _ => {
                    return Err(AppError::internal(format!(
                        "selecting CI merge-base: {}",
                        out.stderr.trim()
                    )));
                }
            }
            if self
                .paths_changed(
                    repo,
                    base.merge_base.as_deref().unwrap(),
                    sha,
                    &[super::pipeline::PIPELINE_PATH.to_string()],
                )
                .await?
            {
                all = true;
            }
        }
        let mut skipped_jobs = Vec::new();
        if !all {
            for job in jobs.iter().filter(|j| !j.paths.is_empty()) {
                let mut changed = false;
                for base in &bases {
                    if self
                        .paths_changed(repo, base.merge_base.as_deref().unwrap(), sha, &job.paths)
                        .await?
                    {
                        changed = true;
                        break;
                    }
                }
                if !changed {
                    skipped_jobs.push(job.name.clone());
                }
            }
        }
        Ok(JobSelection {
            bases,
            skipped_jobs,
        })
    }

    /// The input digest of every job of `pipeline` with paths at `sha`: the
    /// SHA-256 (hex) of the raw diff from the empty tree to `sha` limited to
    /// the job's paths, i.e. the (mode, blob, path) of every matching file.
    /// The digests of the commit's run are kept while its pipeline is the same.
    pub(super) async fn job_inputs(
        &self,
        repo: &Repo,
        sha: &str,
        pipeline: &PipelineState,
    ) -> AppResult<BTreeMap<String, String>> {
        let PipelineState::Jobs { jobs } = pipeline else {
            return Ok(BTreeMap::new());
        };
        let (id, commit) = (repo.id.clone(), sha.to_string());
        let previous = self
            .inner
            .store
            .read(move |r| queue::get_run(r, &id, &commit))
            .await?;
        let known = previous
            .as_ref()
            .filter(|run| run.pipeline == *pipeline)
            .map(|run| &run.inputs);
        let dir = self.inner.git.repo_dir(&repo.path);
        let mut empty_tree = None;
        let mut out = BTreeMap::new();
        for job in jobs.iter().filter(|j| !j.paths.is_empty()) {
            if let Some(digest) = known.and_then(|k| k.get(&job.name)) {
                out.insert(job.name.clone(), digest.clone());
                continue;
            }
            if empty_tree.is_none() {
                empty_tree = Some(
                    self.inner
                        .git
                        .run_ok(Some(&dir), &["hash-object", "-t", "tree", "/dev/null"])
                        .await?,
                );
            }
            let specs = pathspecs(&job.paths);
            let mut args = vec![
                "diff-tree",
                "-r",
                "-z",
                "--no-renames",
                "--raw",
                empty_tree.as_deref().unwrap(),
                sha,
                "--",
            ];
            args.extend(specs.iter().map(String::as_str));
            let diff = self.inner.git.run(Some(&dir), &args).await?;
            if !diff.ok() || diff.truncated {
                return Err(AppError::internal(format!(
                    "computing CI inputs of {}: {}",
                    job.name,
                    diff.stderr.trim()
                )));
            }
            out.insert(job.name.clone(), util::hex(&util::sha256(&diff.stdout)));
        }
        Ok(out)
    }

    async fn paths_changed(
        &self,
        repo: &Repo,
        base: &str,
        head: &str,
        paths: &[String],
    ) -> AppResult<bool> {
        let specs = pathspecs(paths);
        let mut args = vec![
            "diff-tree",
            "-r",
            "--quiet",
            "--no-renames",
            base,
            head,
            "--",
        ];
        args.extend(specs.iter().map(String::as_str));
        let out = self
            .inner
            .git
            .run(Some(&self.inner.git.repo_dir(&repo.path)), &args)
            .await?;
        match out.status {
            0 => Ok(false),
            1 => Ok(true),
            _ => Err(AppError::internal(format!(
                "selecting CI paths: {}",
                out.stderr.trim()
            ))),
        }
    }
}

/// The Git pathspecs of a job's `paths`, shared by selection and inputs.
fn pathspecs(paths: &[String]) -> Vec<String> {
    paths.iter().map(|p| format!(":(glob){p}")).collect()
}

#[cfg(test)]
mod tests {
    use super::super::tests::{commit_all, inputs_pipeline, raw_digest};
    use super::*;

    #[tokio::test]
    async fn inputs_digest_exactly_the_files_matching_paths() {
        let (temp, ci, repo) = super::super::tests::setup();
        let git = &ci.inner.git;
        let dir = git.repo_dir(&repo.path);
        git.init_bare(&dir, "main").await.unwrap();
        let work = temp.path().join("work");
        let w = work.to_str().unwrap();
        git.run_ok(None, &["init", "-q", "-b", "main", w])
            .await
            .unwrap();
        std::fs::create_dir_all(work.join("src")).unwrap();
        for (file, text) in [("src/a.txt", "a"), ("src/b.sh", "b"), ("other.txt", "o")] {
            std::fs::write(work.join(file), text).unwrap();
        }
        let base = commit_all(git, &work, "base").await;
        let mut variants = Vec::new();
        for change in ["other", "content", "mode", "added", "deleted", "renamed"] {
            match change {
                "other" => std::fs::write(work.join("other.txt"), "o2").unwrap(),
                "content" => std::fs::write(work.join("src/a.txt"), "a2").unwrap(),
                "mode" => {
                    use std::os::unix::fs::PermissionsExt;
                    let file = work.join("src/b.sh");
                    let mut mode = std::fs::metadata(&file).unwrap().permissions();
                    mode.set_mode(0o755);
                    std::fs::set_permissions(&file, mode).unwrap();
                }
                "added" => std::fs::write(work.join("src/c.txt"), "c").unwrap(),
                "deleted" => std::fs::remove_file(work.join("src/b.sh")).unwrap(),
                _ => std::fs::rename(work.join("src/a.txt"), work.join("src/d.txt")).unwrap(),
            }
            variants.push((change, commit_all(git, &work, change).await));
            git.run_ok(None, &["-C", w, "reset", "-q", "--hard", &base])
                .await
                .unwrap();
        }
        let mut args = vec!["-C", w, "push", "-q", dir.to_str().unwrap()];
        let refspecs: Vec<String> = std::iter::once(&base)
            .chain(variants.iter().map(|(_, sha)| sha))
            .enumerate()
            .map(|(i, sha)| format!("{sha}:refs/heads/v{i}"))
            .collect();
        args.extend(refspecs.iter().map(String::as_str));
        git.run_ok(None, &args).await.unwrap();
        let pipeline = PipelineState::Jobs {
            jobs: super::super::pipeline::parse(&inputs_pipeline("'src/**'")).unwrap(),
        };
        let inputs = |sha: String| {
            let (ci, repo, pipeline) = (&ci, &repo, &pipeline);
            async move {
                let inputs = ci.job_inputs(repo, &sha, pipeline).await.unwrap();
                assert_eq!(
                    inputs.keys().collect::<Vec<_>>(),
                    ["lint"],
                    "only jobs with paths"
                );
                inputs["lint"].clone()
            }
        };
        let digest = inputs(base.clone()).await;
        assert_eq!(digest, raw_digest(git, &dir, &base, &["src/**"]).await);
        let mut seen = vec![digest.clone()];
        for (change, sha) in &variants {
            let other = inputs(sha.clone()).await;
            if *change == "other" {
                assert_eq!(other, digest, "a file outside paths changed");
            } else {
                assert!(!seen.contains(&other), "{change}");
                seen.push(other);
            }
        }
        // The run's digests are kept while its pipeline is the same, and
        // computed again for another one.
        let reg = queue::Registration {
            inputs: BTreeMap::from([("lint".to_string(), "kept".to_string())]),
            selection: None,
            repo: repo.id.clone(),
            repo_path: repo.path.clone(),
            sha: base.clone(),
            tree: None,
            branch: "main".into(),
            protected: false,
            pipeline: pipeline.clone(),
            network: false,
        };
        ci.inner
            .store
            .write(move |w| queue::register(w, reg, 10, &util::now()))
            .await
            .unwrap();
        assert_eq!(inputs(base.clone()).await, "kept");
        let narrower = PipelineState::Jobs {
            jobs: super::super::pipeline::parse(&inputs_pipeline("'src/a.txt'")).unwrap(),
        };
        assert_eq!(
            ci.job_inputs(&repo, &base, &narrower).await.unwrap()["lint"],
            raw_digest(git, &dir, &base, &["src/a.txt"]).await
        );
    }

    #[tokio::test]
    async fn paths_without_a_merge_base_run_every_job() {
        let (_temp, ci, repo) = super::super::tests::setup();
        let git = &ci.inner.git;
        let dir = git.repo_dir(&repo.path);
        git.init_bare(&dir, "main").await.unwrap();
        let tree = git
            .run_ok(Some(&dir), &["hash-object", "-t", "tree", "-w", "--stdin"])
            .await
            .unwrap();
        let mut commits = Vec::new();
        for message in ["base", "unrelated head"] {
            let out = git
                .run_env(
                    Some(&dir),
                    &["commit-tree", &tree, "-m", message],
                    &[
                        ("GIT_AUTHOR_NAME", "Test"),
                        ("GIT_AUTHOR_EMAIL", "test@example.invalid"),
                        ("GIT_COMMITTER_NAME", "Test"),
                        ("GIT_COMMITTER_EMAIL", "test@example.invalid"),
                    ],
                )
                .await
                .unwrap();
            assert!(out.ok());
            commits.push(out.text().trim().to_string());
        }
        let jobs = super::super::pipeline::parse(
            "[[jobs]]\nname='test'\nimage='alpine'\ncommands=['true']\npaths=['src/**']",
        )
        .unwrap();
        for base_sha in [&commits[0], ""] {
            let selection = ci
                .select_bases(
                    &repo,
                    &commits[1],
                    &jobs,
                    vec![SelectionBase {
                        pull: 1,
                        base: "main".into(),
                        base_sha: base_sha.into(),
                        merge_base: None,
                    }],
                )
                .await
                .unwrap();
            assert!(selection.skipped_jobs.is_empty());
            assert!(selection.bases[0].merge_base.is_none());
        }
    }
}
