//! PR-head job selection. Git owns pathspec matching; the queue receives a
//! snapshot, so admission never needs Git or mutable PR state.

use super::Ci;
use super::queue::{self, JobSelection, PipelineState, SelectionBase};
use crate::error::{AppError, AppResult};
use crate::model::Repo;

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

    async fn paths_changed(
        &self,
        repo: &Repo,
        base: &str,
        head: &str,
        paths: &[String],
    ) -> AppResult<bool> {
        let specs: Vec<String> = paths.iter().map(|p| format!(":(glob){p}")).collect();
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

#[cfg(test)]
mod tests {
    use super::*;

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
