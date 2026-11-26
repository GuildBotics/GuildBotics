//! Releases: tags with the pipeline's release prefix on verified commits of
//! the default branch, made on request or at the pipeline's release times
//! (`docs/ci.md`, "Releases"). Branvil records each release it makes; only
//! those are releases. A verified commit whose verification jobs printed a
//! release point is released before any newer commit.
//! Git reads happen outside short store writes; the repository lock
//! serializes both.
use std::collections::{BTreeMap, BTreeSet};

use chrono::{DateTime, Duration, TimeZone, Utc};
use serde::{Deserialize, Serialize};

use super::pipeline::{self, ReleaseRule, ReleaseTime};
use crate::app::App;
use crate::auth::Caller;
use crate::error::{AppError, AppResult};
use crate::git::ZERO_SHA;
use crate::model::Repo;
use crate::store::{self, Key, Rd, Wtx};
use crate::util;

/// The longest name a release request gives after the prefix.
const MAX_NAME: usize = 64;
/// A repository's releases and the release time its schedule handled.
#[derive(Serialize, Deserialize, Clone, Debug, Default, PartialEq)]
struct State {
    /// The latest release time handled (RFC 3339), under `schedule`.
    last_slot: Option<String>,
    /// The release times and time zone `last_slot` was handled under; none
    /// without a schedule.
    schedule: Option<String>,
    /// The releases made: tag name (after `refs/tags/`) → commit.
    released: BTreeMap<String, String>,
}

fn get(r: &impl Rd, repo: &str) -> AppResult<State> {
    Ok(r.get(store::CI_RELEASES, &key(repo))?.unwrap_or_default())
}

/// One release point a verification job printed.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Note {
    pub job: String,
    pub attempt_id: String,
    pub message: String,
}

/// An unreleased commit that is a release point.
#[derive(Serialize, Debug)]
pub struct Point {
    pub sha: String,
    pub notes: Vec<Note>,
}

fn key(repo: &str) -> Vec<u8> {
    Key::new().s(repo).bytes()
}

fn point_key(repo: &str, sha: &str) -> Vec<u8> {
    Key::new().s(repo).s(sha).bytes()
}

/// Decodes every release record, for the data check.
pub(crate) fn check(r: &impl Rd) -> AppResult<()> {
    let _: Vec<State> = r.prefix_all(store::CI_RELEASES, &[])?;
    let _: Vec<Vec<Note>> = r.prefix_all(store::CI_RELEASE_POINTS, &[])?;
    Ok(())
}

/// Records the release points of verified commit `sha`.
pub(super) fn put_points(w: &Wtx, repo: &str, sha: &str, notes: &[Note]) -> AppResult<()> {
    if notes.is_empty() {
        w.del(store::CI_RELEASE_POINTS, &point_key(repo, sha))?;
        Ok(())
    } else {
        w.put(store::CI_RELEASE_POINTS, &point_key(repo, sha), &notes)
    }
}

/// The latest release time at or before `now`, and the next one after it.
pub fn slots(
    rule: &ReleaseRule,
    now: DateTime<Utc>,
) -> (Option<DateTime<Utc>>, Option<DateTime<Utc>>) {
    let today = now.with_timezone(&rule.time_zone).date_naive();
    let mut times = Vec::new();
    for day in [today - Duration::days(1), today, today + Duration::days(1)] {
        for time in &rule.schedule {
            let hours: Vec<(u32, u32)> = match *time {
                ReleaseTime::Daily { hour, minute } => vec![(hour, minute)],
                ReleaseTime::Hourly { minute } => (0..24).map(|hour| (hour, minute)).collect(),
            };
            for (hour, minute) in hours {
                // A local time a clock change skips does not occur that day.
                if let Some(at) = day
                    .and_hms_opt(hour, minute, 0)
                    .and_then(|local| rule.time_zone.from_local_datetime(&local).earliest())
                {
                    times.push(at.with_timezone(&Utc));
                }
            }
        }
    }
    (
        times.iter().filter(|t| **t <= now).max().copied(),
        times.iter().filter(|t| **t > now).min().copied(),
    )
}

/// The default branch and the releases made on it, as releases see them.
struct Main {
    tip: String,
    /// Its first-parent history, newest first.
    first_parent: Vec<String>,
    refs: BTreeMap<String, String>,
    /// The releases made (`State::released`).
    released: BTreeMap<String, String>,
}

impl Main {
    async fn read(
        app: &App,
        repo: &Repo,
        released: BTreeMap<String, String>,
    ) -> AppResult<Option<Self>> {
        let dir = app.git.repo_dir(&repo.path);
        let refs = app.git.all_refs(&dir).await?;
        let Some(tip) = refs
            .get(&format!("refs/heads/{}", repo.default_branch))
            .cloned()
        else {
            return Ok(None);
        };
        let history = app
            .git
            .run_ok(Some(&dir), &["rev-list", "--first-parent", &tip, "--"])
            .await?;
        Ok(Some(Self {
            tip,
            first_parent: history.lines().map(str::to_owned).collect(),
            refs,
            released,
        }))
    }

    /// The released commits of the first-parent history with the names of
    /// the releases made on them, newest first. Any other tag, under the
    /// prefix or not, is no release.
    fn releases(&self) -> Vec<(&str, Vec<String>)> {
        let mut tags: BTreeMap<&str, Vec<String>> = BTreeMap::new();
        for (name, sha) in &self.released {
            if self.refs.get(&format!("refs/tags/{name}")) == Some(sha) {
                tags.entry(sha.as_str()).or_default().push(name.clone());
            }
        }
        self.first_parent
            .iter()
            .filter_map(|sha| tags.remove(sha.as_str()).map(|names| (sha.as_str(), names)))
            .collect()
    }

    /// The first-parent commits newer than the latest release, newest first.
    fn unreleased(&self) -> &[String] {
        let latest = self.releases().first().map(|(sha, _)| (*sha).to_owned());
        let end = latest
            .and_then(|sha| self.first_parent.iter().position(|c| *c == sha))
            .unwrap_or(self.first_parent.len());
        &self.first_parent[..end]
    }

    fn verified(&self, sha: &str) -> bool {
        self.refs
            .get(&format!("refs/branvil/verified/{sha}"))
            .is_some_and(|v| v == sha)
    }

    /// `<prefix><date>-<n>`: the next number of the day in the rule's zone.
    fn default_name(&self, rule: &ReleaseRule, now: DateTime<Utc>) -> String {
        let date = now.with_timezone(&rule.time_zone).format("%Y-%m-%d");
        let stem = format!("{}{date}-", rule.tag_prefix);
        let last = self
            .refs
            .keys()
            .filter_map(|r| {
                r.strip_prefix("refs/tags/")?
                    .strip_prefix(&stem)?
                    .parse::<u64>()
                    .ok()
            })
            .max()
            .unwrap_or(0);
        format!("{stem}{}", last.saturating_add(1))
    }

    /// The unreleased commits that are release points, newest first.
    async fn points(&self, app: &App, repo: &Repo) -> AppResult<Vec<Point>> {
        let (id, shas) = (repo.id.clone(), self.unreleased().to_vec());
        app.store
            .read(move |r| {
                let mut points = Vec::new();
                for sha in shas {
                    if let Some(notes) = r.get(store::CI_RELEASE_POINTS, &point_key(&id, &sha))? {
                        points.push(Point { sha, notes });
                    }
                }
                Ok(points)
            })
            .await
    }

    /// Records the release, then makes its tag. A record without its tag (a
    /// stop between the two) is no release, and keeps pushes off the name.
    async fn release(
        &mut self,
        app: &App,
        repo: &Repo,
        state: &mut State,
        name: &str,
        sha: &str,
    ) -> AppResult<()> {
        let refname = format!("refs/tags/{name}");
        let exists =
            || AppError::conflict_code("release_tag_exists", format!("The tag {name} exists."));
        if self.refs.contains_key(&refname) {
            return Err(exists());
        }
        state.released.insert(name.to_owned(), sha.to_owned());
        put(app, repo, state).await?;
        let dir = app.git.repo_dir(&repo.path);
        if !app
            .git
            .update_ref(&dir, &refname, sha, ZERO_SHA, "release")
            .await?
        {
            state.released.remove(name);
            put(app, repo, state).await?;
            return Err(exists());
        }
        self.refs.insert(refname, sha.to_owned());
        self.released = state.released.clone();
        Ok(())
    }
}

async fn put(app: &App, repo: &Repo, state: &State) -> AppResult<()> {
    let (id, state) = (repo.id.clone(), state.clone());
    app.store
        .write(move |w| w.put(store::CI_RELEASES, &key(&id), &state))
        .await
}

/// The release rule of the default branch's tip, and why it is the default
/// one when that pipeline is invalid.
async fn rule_at(app: &App, repo: &Repo, tip: &str) -> AppResult<(ReleaseRule, Option<String>)> {
    let dir = app.git.repo_dir(&repo.path);
    Ok(match pipeline::read_release(&app.git, &dir, tip).await? {
        Ok(rule) => (rule, None),
        Err(message) => (ReleaseRule::default(), Some(message)),
    })
}

/// The tags pushes may not touch: those under the default branch's prefix
/// (the default one when it cannot be read) and every release made.
pub struct Reserved {
    pub prefix: String,
    pub released: BTreeSet<String>,
}

impl Reserved {
    pub fn covers(&self, tag: &str) -> bool {
        tag.starts_with(&self.prefix) || self.released.contains(tag)
    }
}

pub async fn reserved(
    app: &App,
    repo: &Repo,
    refs: &BTreeMap<String, String>,
) -> AppResult<Reserved> {
    let prefix = match refs.get(&format!("refs/heads/{}", repo.default_branch)) {
        Some(tip) => rule_at(app, repo, tip).await?.0.tag_prefix,
        None => pipeline::DEFAULT_TAG_PREFIX.into(),
    };
    let id = repo.id.clone();
    let state = app.store.read(move |r| get(r, &id)).await?;
    Ok(Reserved {
        prefix,
        released: state.released.into_keys().collect(),
    })
}

/// Releases at the release time that came: each unreleased release point,
/// oldest first, then the newest verified commit. Called by the landing sweep.
pub async fn advance(app: &App, repo: &Repo) -> AppResult<()> {
    advance_at(app, repo, Utc::now()).await
}

pub(crate) async fn advance_at(app: &App, repo: &Repo, now: DateTime<Utc>) -> AppResult<()> {
    let id = repo.id.clone();
    app.locked(&repo.id, move |app| async move {
        let repo = app
            .store
            .read(move |r| crate::domain::repo_by_id(r, &id))
            .await?;
        let dir = app.git.repo_dir(&repo.path);
        let tips = app.git.branch_tips(&dir).await?;
        let Some(tip) = tips.get(&repo.default_branch) else {
            return Ok(());
        };
        let (rule, problem) = rule_at(&app, &repo, tip).await?;
        let id = repo.id.clone();
        let old = app.store.read(move |r| get(r, &id)).await?;
        let mut state = old.clone();
        let times: Vec<String> = rule.schedule.iter().map(ToString::to_string).collect();
        let schedule = (problem.is_none() && !times.is_empty())
            .then(|| format!("{} {}", times.join(","), rule.time_zone.name()));
        let slot = slots(&rule, now).0;
        let due = match (&schedule, slot) {
            (Some(schedule), Some(slot)) => {
                let last = state
                    .last_slot
                    .as_deref()
                    .and_then(|s| DateTime::parse_from_rfc3339(s).ok());
                // A schedule seen for the first time, or changed, waits for
                // its next time; each time releases once.
                if state.schedule.as_ref() == Some(schedule) {
                    last.is_some_and(|last| last < slot).then_some(slot)
                } else {
                    state.schedule = Some(schedule.clone());
                    state.last_slot = Some(util::format_time(slot));
                    None
                }
            }
            _ => {
                state.schedule = None;
                state.last_slot = None;
                None
            }
        };
        if let Some(slot) = due
            && let Some(mut main) = Main::read(&app, &repo, state.released.clone()).await?
        {
            // The release points oldest first, then the newest verified
            // commit. A point not verified yet holds itself and every later
            // release: the time is handled once it is.
            let mut shas = Vec::new();
            let mut held = false;
            for point in main.points(&app, &repo).await?.into_iter().rev() {
                if !main.verified(&point.sha) {
                    held = true;
                    break;
                }
                shas.push(point.sha);
            }
            let newest = main.unreleased().iter().find(|sha| main.verified(sha));
            if !held
                && let Some(newest) = newest.cloned()
                && shas.last() != Some(&newest)
            {
                shas.push(newest);
            }
            // A failure leaves the time unhandled: the next sweep releases
            // the rest, as the releases made are recorded one by one.
            for sha in &shas {
                let name = main.default_name(&rule, now);
                main.release(&app, &repo, &mut state, &name, sha).await?;
            }
            if !shas.is_empty() {
                app.events.publish("release", &repo.path, "");
            }
            if !held {
                state.last_slot = Some(util::format_time(slot));
            }
        }
        if state != old {
            put(&app, &repo, &state).await?;
        }
        Ok(())
    })
    .await
}

/// Releases `sha` as the owner asked.
pub async fn request(
    app: &App,
    caller: &Caller,
    path: &str,
    sha: String,
    name: Option<String>,
) -> AppResult<View> {
    caller.require_owner()?;
    if !util::is_sha(&sha) {
        return Err(AppError::invalid("sha must be a full commit SHA."));
    }
    let repo = crate::domain::repos::visible(app, caller, path).await?;
    start(app, repo, sha, name, Utc::now()).await
}

pub(crate) async fn start(
    app: &App,
    repo: Repo,
    sha: String,
    name: Option<String>,
    now: DateTime<Utc>,
) -> AppResult<View> {
    let id = repo.id.clone();
    app.locked(&id, move |app| async move {
        let id = repo.id.clone();
        let mut state = app.store.read(move |r| get(r, &id)).await?;
        let mut main = Main::read(&app, &repo, state.released.clone())
            .await?
            .ok_or_else(|| AppError::not_found("The default branch does not exist."))?;
        let (rule, problem) = rule_at(&app, &repo, &main.tip).await?;
        if let Some(problem) = problem {
            return Err(AppError::conflict_code(
                "release_pipeline_invalid",
                format!("The release rule is unknown: {problem}"),
            ));
        }
        let name = match name {
            None => main.default_name(&rule, now),
            Some(name) => {
                let full = format!("{}{name}", rule.tag_prefix);
                if name.is_empty()
                    || name.len() > MAX_NAME
                    || full.ends_with(['/', '.'])
                    || !pipeline::valid_tag_text(&full)
                {
                    return Err(AppError::invalid(format!(
                        "'{name}' is not a release name ([A-Za-z0-9_./-], at most {MAX_NAME})."
                    )));
                }
                full
            }
        };
        let unreleased = main.unreleased();
        let Some(at) = unreleased.iter().position(|c| *c == sha) else {
            return Err(AppError::conflict_code(
                "release_not_newer",
                format!(
                    "{sha} is not a commit of {} newer than its latest release.",
                    repo.default_branch
                ),
            ));
        };
        if !main.verified(&sha) {
            return Err(AppError::conflict_code(
                "release_not_verified",
                format!(
                    "{sha} has not passed the verification of {}.",
                    repo.default_branch
                ),
            ));
        }
        let points = main.points(&app, &repo).await?;
        if let Some(oldest) = points.last()
            && unreleased.iter().position(|c| *c == oldest.sha) > Some(at)
        {
            return Err(AppError::conflict_code(
                "release_point_first",
                format!(
                    "{} is a release point: release it before {sha}.",
                    oldest.sha
                ),
            ));
        }
        main.release(&app, &repo, &mut state, &name, &sha).await?;
        app.events.publish("release", &repo.path, "");
        view_of(&app, &repo, Some(main), None, now).await
    })
    .await
}

/// One release: its tag and commit.
#[derive(Serialize, Debug)]
pub struct Released {
    pub name: String,
    pub sha: String,
    pub title: String,
    pub authored_at: String,
}

/// The release rule, the unreleased release points and the releases of the
/// default branch, newest first.
#[derive(Serialize, Debug)]
pub struct View {
    pub tag_prefix: String,
    pub schedule: Vec<String>,
    pub time_zone: String,
    pub next_at: Option<String>,
    /// Why the rule is the default one: the default branch's pipeline is
    /// invalid.
    pub problem: Option<String>,
    /// The name a release made now takes by default.
    pub next_name: String,
    /// The unreleased release points: no newer commit is released before
    /// the oldest of them.
    pub points: Vec<Point>,
    pub releases: Vec<Released>,
    /// With `since`: whether that commit is in the first-parent history
    /// (the releases are then only the newer ones).
    pub since_on_main: Option<bool>,
}

pub async fn view(
    app: &App,
    caller: &Caller,
    path: &str,
    since: Option<String>,
) -> AppResult<View> {
    if since.as_deref().is_some_and(|s| !util::is_sha(s)) {
        return Err(AppError::invalid("since must be a full commit SHA."));
    }
    let repo = crate::domain::repos::visible(app, caller, path).await?;
    let id = repo.id.clone();
    app.locked(&id, move |app| async move {
        let id = repo.id.clone();
        let released = app.store.read(move |r| get(r, &id)).await?.released;
        let main = Main::read(&app, &repo, released).await?;
        view_of(&app, &repo, main, since, Utc::now()).await
    })
    .await
}

async fn view_of(
    app: &App,
    repo: &Repo,
    main: Option<Main>,
    since: Option<String>,
    now: DateTime<Utc>,
) -> AppResult<View> {
    let Some(main) = main else {
        let rule = ReleaseRule::default();
        return Ok(View {
            tag_prefix: rule.tag_prefix.clone(),
            schedule: Vec::new(),
            time_zone: rule.time_zone.name().into(),
            next_at: None,
            problem: None,
            next_name: format!("{}{}-1", rule.tag_prefix, now.format("%Y-%m-%d")),
            points: Vec::new(),
            releases: Vec::new(),
            since_on_main: since.map(|_| false),
        });
    };
    let (rule, problem) = rule_at(app, repo, &main.tip).await?;
    let mut released = main.releases();
    let since_on_main = since.map(|since| {
        let found = main.first_parent.iter().position(|c| *c == since);
        if let Some(end) = found {
            released.retain(|(sha, _)| {
                main.first_parent
                    .iter()
                    .position(|c| c == sha)
                    .is_some_and(|i| i < end)
            });
        }
        found.is_some()
    });
    let mut releases = Vec::new();
    if !released.is_empty() {
        let dir = app.git.repo_dir(&repo.path);
        let mut args = vec!["show", "-s", "--no-color", "--format=%H%x00%s%x00%aI"];
        args.extend(released.iter().map(|(sha, _)| *sha));
        args.push("--");
        let text = app.git.run_ok(Some(&dir), &args).await?;
        let mut info = BTreeMap::new();
        for line in text.lines() {
            let fields: Vec<_> = line.splitn(3, '\0').collect();
            if let [sha, title, at] = fields.as_slice() {
                info.insert(*sha, (*title, util::parse_time(at)?));
            }
        }
        for (sha, names) in &released {
            let (title, at) = info
                .get(sha)
                .cloned()
                .ok_or_else(|| AppError::internal("Invalid Git log record"))?;
            for name in names.iter().rev() {
                releases.push(Released {
                    name: name.clone(),
                    sha: (*sha).to_owned(),
                    title: title.to_owned(),
                    authored_at: at.clone(),
                });
            }
        }
    }
    Ok(View {
        tag_prefix: rule.tag_prefix.clone(),
        schedule: rule.schedule.iter().map(ToString::to_string).collect(),
        time_zone: rule.time_zone.name().into(),
        next_at: if problem.is_none() {
            slots(&rule, now).1.map(util::format_time)
        } else {
            None
        },
        next_name: main.default_name(&rule, now),
        problem,
        points: main.points(app, repo).await?,
        releases,
        since_on_main,
    })
}

#[cfg(test)]
#[path = "release_tests.rs"]
mod tests;
