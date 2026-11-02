//! `[ci] runner = "auto"`: `branvil serve` starts one `branvil runner` child
//! (the same binary, a separate process) and restarts it when it exits. The
//! mode and the limits the child reads can change while the service runs
//! (Settings → CI, `settings.rs`): the child reads the limits again before
//! every claim, so only the mode stops it. The child authenticates as the runner
//! member `branvil-runner` with a token minted at each start (the previous
//! automatic token is revoked), passed through the environment, never the
//! command line.

use std::time::Duration;

use crate::app::Shared;
use crate::ci::config::RunnerMode;
use crate::ci::queue;
use crate::domain::members::{NewMember, insert_member};
use crate::error::{AppError, AppResult};
use crate::model::{Member, MemberKind};
use crate::runner::executor::Executor;
use crate::store::{self, Rd};
use crate::util;

pub const AUTO_RUNNER: &str = "branvil-runner";

/// Mints the token of the next child. The previous child is gone (it ends with
/// its service, or before the next start): the jobs it held are interrupted
/// now, and queued again in their place ahead of the others, rather than when
/// their leases run out, after the next child took others.
async fn mint_token(app: &Shared) -> AppResult<String> {
    let now = util::now();
    let (token, changed) = app
        .store
        .write(move |w| {
            match w.get::<Member>(store::MEMBERS, &Member::key(AUTO_RUNNER))? {
                Some(m) if m.kind != MemberKind::Runner => {
                    return Err(AppError::conflict(format!(
                        "The member {AUTO_RUNNER} exists and is not a runner; the automatic runner cannot use it."
                    )));
                }
                Some(_) => {}
                None => {
                    insert_member(
                        w,
                        &NewMember {
                            name: AUTO_RUNNER.into(),
                            display_name: Some("Branvil runner (automatic)".into()),
                            kind: MemberKind::Runner,
                        },
                    )?;
                }
            }
            let changed = queue::interrupt_running(
                w,
                Some(AUTO_RUNNER),
                "the automatic runner stopped before it finished (Branvil stopped, or the runner exited)",
                &now,
            )?;
            Ok((
                crate::auth::replace_token(w, AUTO_RUNNER, crate::auth::AUTO_LABEL)?.1,
                changed,
            ))
        })
        .await?;
    for (path, sha) in changed {
        app.events.publish("check", &path, sha);
    }
    Ok(token)
}

/// Supervises the runner child for the lifetime of the service: runs one
/// while the mode is `auto`, and stops it when the owner switches the mode
/// off.
pub fn start(app: Shared, url: String) {
    tokio::spawn(async move {
        let mut desired = app.ci.settings().subscribe();
        let mut backoff = 2;
        loop {
            if *desired.borrow_and_update() != RunnerMode::Auto {
                if desired.changed().await.is_err() {
                    return;
                }
                continue;
            }
            let exe = match mint_token(&app).await.map_err(|e| e.message).and_then(|t| {
                std::env::current_exe()
                    .map(|exe| (t, exe))
                    .map_err(|e| format!("cannot find the branvil binary: {e}"))
            }) {
                Ok(v) => v,
                Err(e) => {
                    // Tried again after the backoff, or at once when the
                    // owner changes the settings.
                    tracing::error!("CI: cannot start the automatic runner: {e}");
                    if !wait(&mut desired, backoff).await {
                        return;
                    }
                    backoff = (backoff * 2).min(120);
                    continue;
                }
            };
            let (token, exe) = exe;
            let mut cmd = tokio::process::Command::new(exe);
            cmd.arg("runner")
                .arg("--url")
                .arg(&url)
                .arg("--data-dir")
                .arg(&app.config.data_dir)
                .arg("--config")
                .arg(&app.config.file)
                .env("BRANVIL_TOKEN", token)
                .env_remove("BRANVIL_FAILPOINT")
                .stdin(std::process::Stdio::null())
                .kill_on_drop(true);
            let started = std::time::Instant::now();
            match cmd.spawn() {
                Ok(mut child) => {
                    tracing::info!("CI: started the automatic runner (pid {:?})", child.id());
                    tokio::select! {
                        status = child.wait() => {
                            tracing::warn!("CI: the automatic runner exited ({status:?}); restarting");
                        }
                        changed = desired.changed() => {
                            let _ = child.kill().await;
                            stopped(&app).await;
                            if changed.is_err() {
                                return;
                            }
                            tracing::info!("CI: the runner mode changed; the automatic runner stopped");
                            backoff = 2;
                            continue;
                        }
                    }
                }
                Err(e) => tracing::error!("CI: cannot start the automatic runner: {e}"),
            }
            if started.elapsed() > Duration::from_secs(300) {
                backoff = 2;
            }
            if !wait(&mut desired, backoff).await {
                return;
            }
            backoff = (backoff * 2).min(120);
        }
    });
}

/// Waits `secs`, or less when the mode changes; false when it never can
/// any more.
async fn wait(desired: &mut tokio::sync::watch::Receiver<RunnerMode>, secs: u64) -> bool {
    tokio::select! {
        _ = tokio::time::sleep(Duration::from_secs(secs)) => true,
        changed = desired.changed() => changed.is_ok(),
    }
}

/// After the child was killed: the microVMs of the jobs it ran are removed
/// (the child could not), and the diagnostics stop counting it.
async fn stopped(app: &Shared) {
    app.ci.forget_runner(AUTO_RUNNER);
    app.ci
        .settings()
        .microsandbox(AUTO_RUNNER)
        .await
        .sweep()
        .await;
}
