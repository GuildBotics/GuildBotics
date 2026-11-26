//! Releases: the schedule, the release points and the tags.
use super::*;
use crate::ci::tests;
use chrono::TimeZone;

struct Fixture {
    temp: tempfile::TempDir,
    app: crate::app::Shared,
    repo: Repo,
}

const TOKYO_RULE: &str = "[release]\nschedule=['12:00']\ntime_zone='Asia/Tokyo'\n";
const JOB: &str = "[[jobs]]\nname='test'\nimage='alpine'\ncommands=['true']\n";

/// `hour:minute` on 2026-11-`day` in Tokyo.
fn tokyo(day: u32, hour: u32, minute: u32) -> DateTime<Utc> {
    chrono_tz::Asia::Tokyo
        .with_ymd_and_hms(2026, 11, day, hour, minute, 0)
        .unwrap()
        .with_timezone(&Utc)
}

impl Fixture {
    async fn new(pipeline: &str) -> Self {
        let (temp, ci, repo) = tests::setup();
        let app = tests::app(&temp, &ci);
        app.store
            .write_sync(|w| {
                w.put(store::REPOS, &Repo::key(&repo.id), &repo)?;
                crate::model::rebuild_indexes(w)
            })
            .unwrap();
        let dir = app.git.repo_dir(&repo.path);
        app.git.init_bare(&dir, "main").await.unwrap();
        let work = temp.path().join("work");
        app.git
            .run_ok(None, &["init", "-q", "-b", "main", work.to_str().unwrap()])
            .await
            .unwrap();
        let f = Self { temp, app, repo };
        f.commit(pipeline).await;
        f
    }

    /// Commits `pipeline` as the pipeline and pushes it to main.
    async fn commit(&self, pipeline: &str) -> String {
        let work = self.temp.path().join("work");
        std::fs::write(work.join(".guildbotics-ci.toml"), pipeline).unwrap();
        let sha = tests::commit_all(&self.app.git, &work, "change").await;
        let dir = self.app.git.repo_dir(&self.repo.path);
        self.app
            .git
            .run_ok(
                None,
                &[
                    "-C",
                    work.to_str().unwrap(),
                    "push",
                    "-q",
                    dir.to_str().unwrap(),
                    "HEAD:refs/heads/main",
                ],
            )
            .await
            .unwrap();
        sha
    }

    async fn verified(&self, pipeline: &str) -> String {
        let sha = self.commit(pipeline).await;
        let dir = self.app.git.repo_dir(&self.repo.path);
        let name = format!("refs/branvil/verified/{sha}");
        assert!(
            self.app
                .git
                .update_ref(&dir, &name, &sha, ZERO_SHA, "test")
                .await
                .unwrap()
        );
        sha
    }

    /// A verified commit whose verification printed a release point.
    async fn point(&self, pipeline: &str) -> String {
        let sha = self.verified(pipeline).await;
        let (id, note) = (
            self.repo.id.clone(),
            Note {
                job: "schema".into(),
                attempt_id: "att_1".into(),
                message: format!("point {pipeline}"),
            },
        );
        let at = sha.clone();
        self.app
            .store
            .write_sync(move |w| put_points(w, &id, &at, &[note]))
            .unwrap();
        sha
    }

    async fn advance(&self, now: DateTime<Utc>) {
        advance_at(&self.app, &self.repo, now).await.unwrap();
    }

    async fn start(&self, sha: &str, name: Option<&str>, now: DateTime<Utc>) -> AppResult<View> {
        start(
            &self.app,
            self.repo.clone(),
            sha.into(),
            name.map(Into::into),
            now,
        )
        .await
    }

    async fn view(&self, since: Option<String>, now: DateTime<Utc>) -> View {
        let main = Main::read(&self.app, &self.repo).await.unwrap();
        view_of(&self.app, &self.repo, main, since, now)
            .await
            .unwrap()
    }

    fn last_slot(&self) -> Option<String> {
        let id = self.repo.id.clone();
        self.app
            .store
            .read_sync(|r| r.get::<State>(store::CI_RELEASES, &key(&id)))
            .unwrap()
            .and_then(|s| s.last_slot)
    }

    async fn tags(&self) -> BTreeMap<String, String> {
        let dir = self.app.git.repo_dir(&self.repo.path);
        self.app
            .git
            .all_refs(&dir)
            .await
            .unwrap()
            .into_iter()
            .filter_map(|(r, sha)| Some((r.strip_prefix("refs/tags/")?.to_owned(), sha)))
            .collect()
    }
}

#[test]
fn release_times_follow_the_rule_s_zone_day_and_hour() {
    let rule = |schedule: &[ReleaseTime], zone| ReleaseRule {
        tag_prefix: "release/".into(),
        schedule: schedule.to_vec(),
        time_zone: zone,
    };
    let noon = rule(
        &[ReleaseTime::Daily {
            hour: 12,
            minute: 0,
        }],
        chrono_tz::Asia::Tokyo,
    );
    assert_eq!(
        slots(&noon, tokyo(26, 11, 59)),
        (Some(tokyo(25, 12, 0)), Some(tokyo(26, 12, 0)))
    );
    assert_eq!(
        slots(&noon, tokyo(26, 12, 0)),
        (Some(tokyo(26, 12, 0)), Some(tokyo(27, 12, 0)))
    );
    let hourly = rule(
        &[ReleaseTime::Hourly { minute: 30 }],
        chrono_tz::Asia::Kolkata,
    );
    let at = Utc.with_ymd_and_hms(2026, 11, 26, 3, 10, 0).unwrap(); // 08:40 in Kolkata
    assert_eq!(
        slots(&hourly, at),
        (
            Some(Utc.with_ymd_and_hms(2026, 11, 26, 3, 0, 0).unwrap()),
            Some(Utc.with_ymd_and_hms(2026, 11, 26, 4, 0, 0).unwrap())
        )
    );
    // 02:30 does not occur in New York on 2026-03-08: that day has no release.
    let skipped = rule(
        &[ReleaseTime::Daily {
            hour: 2,
            minute: 30,
        }],
        chrono_tz::America::New_York,
    );
    let after = Utc.with_ymd_and_hms(2026, 3, 8, 12, 0, 0).unwrap();
    assert_eq!(
        slots(&skipped, after).0,
        Some(Utc.with_ymd_and_hms(2026, 3, 7, 7, 30, 0).unwrap())
    );
    assert_eq!(slots(&rule(&[], chrono_tz::UTC), after), (None, None));
}

#[tokio::test]
async fn an_owner_releases_a_newer_verified_commit_under_the_default_or_given_name() {
    let f = Fixture::new(JOB).await;
    let first = f.verified(&format!("{JOB}# 1\n")).await;
    let unverified = f.commit(&format!("{JOB}# 2\n")).await;
    let now = tokyo(26, 9, 0);
    let error = f.start(&unverified, None, now).await.unwrap_err();
    assert_eq!(error.code, "release_not_verified");
    let view = f.start(&first, None, now).await.unwrap();
    assert_eq!(
        f.tags().await,
        [("release/2026-11-26-1".into(), first.clone())].into()
    );
    assert_eq!(view.releases.len(), 1);
    assert_eq!(view.next_name, "release/2026-11-26-2");
    // Only a commit newer than the latest release.
    let error = f.start(&first, None, now).await.unwrap_err();
    assert_eq!(error.code, "release_not_newer");
    let second = f.verified(&format!("{JOB}# 3\n")).await;
    for (name, code) in [
        ("", "invalid"),
        ("a b", "invalid"),
        ("x/", "invalid"),
        ("2026-11-26-1", "release_tag_exists"),
    ] {
        let error = f.start(&second, Some(name), now).await.unwrap_err();
        assert_eq!(error.code, code, "{name:?}");
    }
    f.start(&second, Some("v2"), now).await.unwrap();
    assert_eq!(f.tags().await["release/v2"], second);
    // A name past the largest number still leaves a default name.
    let third = f.verified(&format!("{JOB}# 4\n")).await;
    let view = f
        .start(&third, Some("2026-11-26-18446744073709551615"), now)
        .await
        .unwrap();
    assert_eq!(view.next_name, "release/2026-11-26-18446744073709551615");
    // A tag under the prefix on a commit that was not verified (pushed
    // before the prefix took effect) is no release.
    let pushed = f.commit(&format!("{JOB}# 5\n")).await;
    let dir = f.app.git.repo_dir(&f.repo.path);
    f.app
        .git
        .update_ref(&dir, "refs/tags/release/pushed", &pushed, ZERO_SHA, "test")
        .await
        .unwrap();
    let names: Vec<_> = f
        .view(None, now)
        .await
        .releases
        .into_iter()
        .map(|r| r.name)
        .collect();
    assert_eq!(
        names,
        [
            "release/2026-11-26-18446744073709551615",
            "release/v2",
            "release/2026-11-26-1"
        ]
    );
}

#[tokio::test]
async fn no_commit_newer_than_an_unreleased_release_point_is_released_first() {
    let f = Fixture::new(JOB).await;
    let first = f.point(&format!("{JOB}# 1\n")).await;
    let second = f.point(&format!("{JOB}# 2\n")).await;
    let newest = f.verified(&format!("{JOB}# 3\n")).await;
    let now = tokyo(26, 9, 0);
    let view = f.view(None, now).await;
    let points: Vec<_> = view.points.iter().map(|p| p.sha.as_str()).collect();
    assert_eq!(points, [second.as_str(), first.as_str()], "newest first");
    assert_eq!(view.points[1].notes[0].message, format!("point {JOB}# 1\n"));
    for sha in [&second, &newest] {
        let error = f.start(sha, None, now).await.unwrap_err();
        assert_eq!(error.code, "release_point_first");
    }
    let view = f.start(&first, None, now).await.unwrap();
    assert_eq!(view.points.len(), 1, "a released point is no longer listed");
    let error = f.start(&newest, None, now).await.unwrap_err();
    assert_eq!(error.code, "release_point_first");
    f.start(&second, None, now).await.unwrap();
    f.start(&newest, None, now).await.unwrap();
    assert!(f.view(None, now).await.points.is_empty());
}

#[tokio::test]
async fn the_schedule_releases_the_points_in_order_then_the_newest_verified_commit() {
    let rule = format!("{TOKYO_RULE}{JOB}");
    let f = Fixture::new(&rule).await;
    // A schedule seen for the first time waits for its next time.
    f.verified(&format!("{rule}# seen\n")).await;
    f.advance(tokyo(26, 12, 30)).await;
    assert!(f.tags().await.is_empty());
    assert_eq!(f.last_slot().as_deref(), Some("2026-11-26T03:00:00.000Z"));
    let first = f.point(&format!("{rule}# 1\n")).await;
    let middle = f.verified(&format!("{rule}# 2\n")).await;
    let second = f.point(&format!("{rule}# 3\n")).await;
    let newest = f.verified(&format!("{rule}# 4\n")).await;
    f.commit(&format!("{rule}# unverified\n")).await;
    f.advance(tokyo(27, 11, 59)).await;
    assert!(f.tags().await.is_empty(), "before the time");
    f.advance(tokyo(27, 12, 0)).await;
    let made: BTreeMap<_, _> = [
        ("release/2026-11-27-1".to_string(), first),
        ("release/2026-11-27-2".to_string(), second),
        ("release/2026-11-27-3".to_string(), newest.clone()),
    ]
    .into();
    assert_eq!(f.tags().await, made);
    assert!(!made.values().any(|sha| *sha == middle));
    // The same time does not release again; the next time has nothing newer.
    f.verified(&format!("{rule}# 5\n")).await;
    f.advance(tokyo(27, 18, 0)).await;
    assert_eq!(f.tags().await, made);
    let latest = f.point(&format!("{rule}# 6\n")).await;
    f.advance(tokyo(28, 12, 0)).await;
    let tags = f.tags().await;
    assert_eq!(tags.len(), 4, "a point that is the newest is tagged once");
    assert_eq!(tags["release/2026-11-28-1"], latest);
}

#[tokio::test]
async fn the_view_names_the_releases_after_a_commit_and_the_next_time() {
    let f = Fixture::new(&format!("{TOKYO_RULE}{JOB}")).await;
    let base = f.commit(&format!("{TOKYO_RULE}{JOB}# base\n")).await;
    let first = f.verified(&format!("{TOKYO_RULE}{JOB}# 1\n")).await;
    f.start(&first, None, tokyo(26, 9, 0)).await.unwrap();
    let second = f.verified(&format!("{TOKYO_RULE}{JOB}# 2\n")).await;
    f.start(&second, None, tokyo(26, 10, 0)).await.unwrap();
    let now = tokyo(26, 11, 0);
    let all = f.view(Some(base), now).await;
    assert_eq!(all.since_on_main, Some(true));
    let shas: Vec<_> = all.releases.iter().map(|r| r.sha.as_str()).collect();
    assert_eq!(shas, [second.as_str(), first.as_str()]);
    let newer = f.view(Some(first.clone()), now).await;
    assert_eq!(newer.releases.len(), 1);
    assert_eq!(newer.releases[0].sha, second);
    let elsewhere = f.view(Some("f".repeat(40)), now).await;
    assert_eq!(elsewhere.since_on_main, Some(false));
    assert_eq!(all.next_at.as_deref(), Some("2026-11-26T03:00:00.000Z"));
    assert_eq!(all.schedule, ["12:00"]);
    assert_eq!(all.time_zone, "Asia/Tokyo");
    assert_eq!(all.problem, None);
    f.commit("[release]\nschedule=['noon']\n").await;
    let broken = f.view(None, now).await;
    assert!(broken.problem.is_some());
    assert_eq!(
        (broken.next_at, broken.tag_prefix.as_str()),
        (None, "release/")
    );
    let invalid = f.verified("[release]\nschedule=['noon']\n# v\n").await;
    let error = f.start(&invalid, None, now).await.unwrap_err();
    assert_eq!(error.code, "release_pipeline_invalid");
}
