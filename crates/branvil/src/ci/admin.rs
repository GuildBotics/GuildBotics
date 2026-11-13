//! The owner's CI settings (`/api/v1/admin/ci`, Settings → CI): the runner,
//! the job resources, the runner's image cache, and which repositories let jobs
//! use the network. See `settings.rs` for what is stored where, and
//! `docs/api.md` ("CI settings").

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use axum::Router;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde::{Deserialize, Serialize};

use super::config::RunnerMode;
use super::pipeline::valid_image;
use super::settings::{Limits, Loaded};
use super::supervise::AUTO_RUNNER;
use crate::app::Shared;
use crate::auth::Caller;
use crate::error::{AppError, AppResult};
use crate::http::J;
use crate::model::Repo;
use crate::runner::executor::{ImageInfo, Microsandbox, Msb};
use crate::store::{self, Rd};

#[derive(Serialize)]
struct View {
    runner: serde_json::Value,
    executor: ExecutorView,
    limits: Limits,
    images: ImagesView,
    operation: Option<super::settings::Operation>,
    repositories: Vec<RepoNetwork>,
}

#[derive(Serialize)]
struct ExecutorView {
    msb: Option<PathBuf>,
    problem: Option<String>,
    home: PathBuf,
}

#[derive(Serialize)]
struct ImagesView {
    error: Option<String>,
    cached: Vec<Cached>,
    missing: Vec<Missing>,
}

#[derive(Serialize)]
struct Cached {
    /// What a pipeline writes as a job's `image`.
    reference: String,
    /// The configuration digest: which version of the tag it is.
    digest: String,
    size_bytes: u64,
    created_at: String,
    /// The repositories whose default-branch pipelines run it.
    used_by: Vec<UsedBy>,
}

/// An image pipelines name that the cache does not hold.
#[derive(Serialize)]
struct Missing {
    image: String,
    repositories: Vec<UsedBy>,
}

#[derive(Serialize, Clone)]
struct UsedBy {
    repository: String,
    jobs: Vec<String>,
}

#[derive(Serialize)]
struct RepoNetwork {
    path: String,
    ci_network: bool,
}

fn cached(i: &ImageInfo) -> Cached {
    Cached {
        reference: i.reference.clone(),
        digest: i.config_digest.clone(),
        size_bytes: i.size_bytes,
        created_at: i.created_at.clone(),
        used_by: Vec::new(),
    }
}

async fn view(app: &Shared) -> AppResult<View> {
    let settings = app.ci.settings();
    let ms = settings.microsandbox(AUTO_RUNNER).await;
    let (msb, problem) = match ms.msb() {
        Msb::Found(p) => (Some(p.clone()), None),
        Msb::Missing(m) => (None, Some(m.clone())),
    };
    let repos: Vec<Repo> = app.store.read(|r| r.prefix_all(store::REPOS, &[])).await?;
    // Which repositories' default-branch pipelines name each image.
    let mut uses: BTreeMap<String, Vec<UsedBy>> = BTreeMap::new();
    for repo in &repos {
        let mut by_image: BTreeMap<String, Vec<String>> = BTreeMap::new();
        for job in app.ci.default_jobs(repo).await {
            by_image.entry(job.image).or_default().push(job.name);
        }
        for (image, jobs) in by_image {
            uses.entry(image).or_default().push(UsedBy {
                repository: repo.path.clone(),
                jobs,
            });
        }
    }
    let mut images = ImagesView {
        error: None,
        cached: Vec::new(),
        missing: Vec::new(),
    };
    if msb.is_some() {
        match ms.images().await {
            Ok(list) => images.cached = list.iter().map(cached).collect(),
            Err(e) => images.error = Some(e),
        }
    }
    for (image, repositories) in uses {
        if msb.is_some() && images.error.is_none() {
            // Without the lock: a load in progress holds it for minutes.
            match ms.inspect(&image).await {
                Ok(Some(held)) => {
                    if let Some(c) = images
                        .cached
                        .iter_mut()
                        .find(|c| c.reference == held.reference)
                    {
                        c.used_by.extend(repositories);
                        continue;
                    }
                }
                Ok(None) => {}
                Err(e) => images.error = Some(e),
            }
        }
        images.missing.push(Missing {
            image,
            repositories,
        });
    }
    Ok(View {
        runner: app.ci.diagnostics().await,
        executor: ExecutorView {
            msb,
            problem,
            home: ms.home().to_path_buf(),
        },
        limits: settings.limits(),
        images,
        operation: settings.operation(),
        repositories: repos
            .into_iter()
            .map(|r| RepoNetwork {
                path: r.path,
                ci_network: r.ci_network,
            })
            .collect(),
    })
}

async fn get_settings(State(app): State<Shared>, c: Caller) -> AppResult<Response> {
    c.require_owner()?;
    Ok(axum::Json(view(&app).await?).into_response())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RunnerInput {
    mode: RunnerMode,
}

async fn put_runner(
    State(app): State<Shared>,
    c: Caller,
    J(input): J<RunnerInput>,
) -> AppResult<Response> {
    c.require_owner()?;
    app.ci.settings().set_mode(input.mode).await?;
    Ok(axum::Json(view(&app).await?).into_response())
}

async fn put_limits(
    State(app): State<Shared>,
    c: Caller,
    J(input): J<Limits>,
) -> AppResult<Response> {
    c.require_owner()?;
    app.ci.settings().set_limits(input).await?;
    Ok(axum::Json(view(&app).await?).into_response())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PullInput {
    reference: String,
}

async fn pull(State(app): State<Shared>, c: Caller, J(input): J<PullInput>) -> AppResult<Response> {
    c.require_owner()?;
    let reference = input.reference.trim().to_string();
    if !valid_image(&reference) {
        return Err(AppError::invalid(format!(
            "'{reference}' is not an image reference by tag ([registry/]name[:tag]): pipelines name an image by its tag."
        )));
    }
    app.ci.settings().begin("pull", &reference)?;
    let worker = app.clone();
    tokio::spawn(async move {
        let settings = worker.ci.settings();
        let ms = settings.microsandbox(AUTO_RUNNER).await;
        settings.end(transfer(&ms, vec![reference.clone()], None, ms.pull(&reference)).await);
    });
    Ok((StatusCode::ACCEPTED, axum::Json(view(&app).await?)).into_response())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct LoadInput {
    path: String,
}

async fn load(State(app): State<Shared>, c: Caller, J(input): J<LoadInput>) -> AppResult<Response> {
    c.require_owner()?;
    let path = PathBuf::from(input.path.trim());
    if !path.is_absolute() {
        return Err(AppError::invalid(
            "Give the absolute path of the archive on the Branvil machine.",
        ));
    }
    match std::fs::symlink_metadata(&path) {
        Ok(m) if m.is_file() => {}
        Ok(_) => {
            return Err(AppError::invalid(format!(
                "{} is not a regular file.",
                path.display()
            )));
        }
        Err(e) => {
            return Err(AppError::invalid(format!(
                "{} cannot be read: {e}",
                path.display()
            )));
        }
    }
    app.ci.settings().begin("load", &path.to_string_lossy())?;
    let worker = app.clone();
    tokio::spawn(async move {
        let settings = worker.ci.settings();
        let ms = settings.microsandbox(AUTO_RUNNER).await;
        settings.end(load_archive(&ms, path).await);
    });
    Ok((StatusCode::ACCEPTED, axum::Json(view(&app).await?)).into_response())
}

/// Loads the image archive at `path` into the cache (see [`transfer`]).
async fn load_archive(ms: &Microsandbox, path: PathBuf) -> Result<Vec<Loaded>, String> {
    let archive = path.clone();
    let names = tokio::task::spawn_blocking(move || archive_references(&archive))
        .await
        .ok()
        .and_then(Result::ok)
        .unwrap_or_default();
    transfer(ms, names, Some(&path), ms.load(&path)).await
}

/// Runs a pull or a load and tells what it brought: the images it names
/// (the pulled reference, the archive's own references) as the cache holds
/// them afterwards, new or written again. When it names none the cache
/// knows, what changed in the cache instead.
///
/// A load from `archive` pins the version msb registered for it: the one
/// reference the load changed. The archive's own references cannot tell
/// which: msb registers a `docker save` archive under its `RepoTags`, while
/// its `index.json` names the image with the registry (`docker.io/...`).
async fn transfer(
    ms: &Microsandbox,
    names: Vec<String>,
    archive: Option<&Path>,
    operation: impl std::future::Future<Output = Result<(), String>>,
) -> Result<Vec<Loaded>, String> {
    // Held through the report, so that it names what this operation brought.
    let _lock = ms.lock_images(true, || {}).await?;
    let held = |i: &ImageInfo| (i.reference.clone(), i.digest.clone(), i.created_at.clone());
    let before: std::collections::HashSet<_> = ms.images().await?.iter().map(held).collect();
    operation.await?;
    let changed: Vec<ImageInfo> = ms
        .images()
        .await?
        .into_iter()
        .filter(|i| !before.contains(&held(i)))
        .collect();
    if let Some(archive) = archive {
        match changed.as_slice() {
            [] => {}
            [image] => ms.pin(&image.reference, Some(archive)).await,
            _ => tracing::warn!(
                "the load of {} changed {} references, so its version is not pinned and stays when replaced",
                archive.display(),
                changed.len()
            ),
        }
    }
    ms.collect().await;
    let loaded = |i: &ImageInfo| Loaded {
        reference: i.reference.clone(),
    };
    let mut named = Vec::new();
    for name in &names {
        if let Some(i) = ms.inspect(name).await? {
            named.push(loaded(&i));
        }
    }
    if !named.is_empty() {
        return Ok(named);
    }
    Ok(changed.iter().map(loaded).collect())
}

/// The references an image archive carries: `RepoTags` of a `docker save`
/// manifest, or the image names an OCI index records. Only the archive's
/// small metadata entries are read.
fn archive_references(path: &Path) -> std::io::Result<Vec<String>> {
    let mut archive = tar::Archive::new(std::fs::File::open(path)?);
    let mut found = Vec::new();
    for entry in archive.entries_with_seek()? {
        let mut entry = entry?;
        let name = entry
            .path()?
            .to_string_lossy()
            .trim_start_matches("./")
            .to_string();
        if name != "manifest.json" && name != "index.json" {
            continue;
        }
        let mut text = String::new();
        std::io::Read::read_to_string(&mut entry, &mut text)?;
        let Ok(v) = serde_json::from_str::<serde_json::Value>(&text) else {
            continue;
        };
        if name == "manifest.json" {
            for image in v.as_array().into_iter().flatten() {
                for tag in image["RepoTags"].as_array().into_iter().flatten() {
                    found.extend(tag.as_str().map(str::to_string));
                }
            }
        } else {
            for m in v["manifests"].as_array().into_iter().flatten() {
                let a = &m["annotations"];
                let named = a["io.containerd.image.name"].as_str().or_else(|| {
                    a["org.opencontainers.image.ref.name"]
                        .as_str()
                        .filter(|r| r.contains(':') || r.contains('/'))
                });
                found.extend(named.map(str::to_string));
            }
        }
    }
    found.dedup();
    Ok(found)
}

loopback_auth::classified_routes!(pub fn routes() -> Router<Shared>, ROUTES,
    ("/admin/ci", (get, get_settings, OwnerSession)),
    ("/admin/ci/runner", (put, put_runner, OwnerSession)),
    ("/admin/ci/limits", (put, put_limits, OwnerSession)),
    ("/admin/ci/images/pull", (post, pull, OwnerSession)),
    ("/admin/ci/images/load", (post, load, OwnerSession)),
);

#[cfg(test)]
mod tests {
    use super::{archive_references, load_archive};
    use crate::runner::executor::{Microsandbox, Msb, tests::write_script};

    fn archive(entries: &[(&str, &str)]) -> tempfile::NamedTempFile {
        let file = tempfile::NamedTempFile::new().unwrap();
        let mut b = tar::Builder::new(file.reopen().unwrap());
        for (name, body) in entries {
            let mut h = tar::Header::new_gnu();
            h.set_size(body.len() as u64);
            h.set_mode(0o644);
            h.set_cksum();
            b.append_data(&mut h, name, body.as_bytes()).unwrap();
        }
        b.finish().unwrap();
        file
    }

    /// A `docker save` archive (Docker 29): its `index.json`, which comes
    /// first, names the image with the registry; its `manifest.json` names
    /// it as `RepoTags`, the name msb registers.
    fn docker_save() -> tempfile::NamedTempFile {
        archive(&[
            ("blobs/sha256/aa", "layer"),
            (
                "index.json",
                r#"{"manifests":[{"annotations":{"io.containerd.image.name":"docker.io/guildbotics/agent-environment:dev","org.opencontainers.image.ref.name":"dev"}}]}"#,
            ),
            (
                "manifest.json",
                r#"[{"Config":"c","RepoTags":["guildbotics/agent-environment:dev"],"Layers":[]}]"#,
            ),
            ("oci-layout", "{}"),
        ])
    }

    /// An msb that keeps references (`fake/refs`, "reference digest" lines)
    /// and manifests (`fake/manifests`): a load registers the version in
    /// `fake/next` under the archive's `RepoTags` name, or under `--tag`; a
    /// manifest goes with its last reference.
    #[cfg(unix)]
    const FAKE_MSB: &str = r#"#!/bin/sh
state="$MSB_HOME/fake"
touch "$state/refs" "$state/manifests"
drop() { awk -v r="$1" '$1 != r' "$state/refs" > "$state/r"; mv "$state/r" "$state/refs"; }
digest() { awk -v r="$1" '$1 == r { print $2 }' "$state/refs"; }
case "$1 $2" in
"load --quiet")
  next=$(cat "$state/next")
  ref=guildbotics/agent-environment:dev
  [ "$5" = --tag ] && ref="$6"
  drop "$ref"
  echo "$ref $next" >> "$state/refs"
  grep -qx "$next" "$state/manifests" || echo "$next" >> "$state/manifests" ;;
"image list")
  sep=; printf '['
  while read -r r d; do printf '%s{"reference":"%s","digest":"%s"}' "$sep" "$r" "$d"; sep=,; done < "$state/refs"
  printf ']' ;;
"image inspect")
  d=$(digest "$5"); [ -n "$d" ] || exit 1
  printf '{"reference":"%s","digest":"%s","config":{"digest":"%s"},"created_at":"%s"}' "$5" "$d" "$d" "$d" ;;
"image remove")
  d=$(digest "$4"); drop "$4"
  awk '{ print $2 }' "$state/refs" | grep -qx "$d" || { grep -vx "$d" "$state/manifests" > "$state/m"; mv "$state/m" "$state/manifests"; } ;;
*) exit 1 ;;
esac
"#;

    /// A load pins the version under the name msb registered, not the
    /// archive's first name; the next load that replaces the version takes
    /// the old one away, leaving no manifest without a reference.
    #[cfg(unix)]
    #[tokio::test]
    async fn a_docker_save_load_pins_its_version_and_the_replaced_one_goes() {
        let dir = tempfile::tempdir().unwrap();
        let msb = dir.path().join("msb");
        write_script(&msb, FAKE_MSB);
        let home = dir.path().join("home");
        let state = home.join("fake");
        std::fs::create_dir_all(&state).unwrap();
        let ms = Microsandbox::new(Msb::Found(msb), home, "t");
        let saved = docker_save();
        let read = |name: &str| std::fs::read_to_string(state.join(name)).unwrap();

        std::fs::write(state.join("next"), "sha256:v1").unwrap();
        let loaded = load_archive(&ms, saved.path().to_path_buf()).await.unwrap();
        let loaded: Vec<&str> = loaded.iter().map(|l| l.reference.as_str()).collect();
        assert_eq!(loaded, ["guildbotics/agent-environment:dev"]);
        assert_eq!(
            read("refs"),
            "guildbotics/agent-environment:dev sha256:v1\n\
             guildbotics/agent-environment@sha256:v1 sha256:v1\n"
        );

        std::fs::write(state.join("next"), "sha256:v2").unwrap();
        load_archive(&ms, saved.path().to_path_buf()).await.unwrap();
        assert_eq!(
            read("refs"),
            "guildbotics/agent-environment:dev sha256:v2\n\
             guildbotics/agent-environment@sha256:v2 sha256:v2\n"
        );
        assert_eq!(read("manifests"), "sha256:v2\n");
    }

    #[test]
    fn an_archive_names_its_images() {
        let docker = archive(&[
            ("blobs/sha256/aa", "layer"),
            (
                "manifest.json",
                r#"[{"Config":"c","RepoTags":["guildbotics/agent-environment:dev"],"Layers":[]}]"#,
            ),
        ]);
        assert_eq!(
            archive_references(docker.path()).unwrap(),
            vec!["guildbotics/agent-environment:dev".to_string()]
        );
        let oci = archive(&[
            ("oci-layout", "{}"),
            (
                "index.json",
                r#"{"manifests":[{"annotations":{"io.containerd.image.name":"docker.io/library/alpine:3","org.opencontainers.image.ref.name":"3"}},{"annotations":{"org.opencontainers.image.ref.name":"3"}}]}"#,
            ),
        ]);
        assert_eq!(
            archive_references(oci.path()).unwrap(),
            vec!["docker.io/library/alpine:3".to_string()]
        );
        assert_eq!(
            archive_references(docker_save().path()).unwrap(),
            [
                "docker.io/guildbotics/agent-environment:dev",
                "guildbotics/agent-environment:dev"
            ]
        );
        let unnamed = archive(&[("README", "x")]);
        assert!(archive_references(unnamed.path()).unwrap().is_empty());
    }
}
