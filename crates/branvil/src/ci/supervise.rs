//! The Hub supervises one runner child. Its CI switch is enforced by the
//! claim endpoint, so it keeps reporting and finishing work when disabled.

use std::time::Duration;

use crate::app::Shared;
use crate::ci::queue;
use crate::domain::members::{NewMember, insert_member};
use crate::error::{AppError, AppResult};
use crate::model::{Member, MemberKind};
use crate::runner::executor::Executor;
use crate::store::{self, Rd};
use crate::util;

pub const AUTO_RUNNER: &str = "hub-runner";
pub const AUTO_RUNNER_DISPLAY_NAME: &str = "GuildBotics runner (automatic)";

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
                            display_name: Some(AUTO_RUNNER_DISPLAY_NAME.into()),
                            kind: MemberKind::Runner,
                        },
                    )?;
                }
            }
            let changed = queue::interrupt_running(
                w,
                Some(AUTO_RUNNER),
                "the automatic runner stopped before it finished (GuildBotics stopped, or the runner exited)",
                &now,
            )?;
            Ok((
                crate::auth::replace_token(w, AUTO_RUNNER, crate::auth::AUTO_LABEL)?.1,
                changed,
            ))
        })
        .await?;
    app.ci.publish(&changed);
    Ok(token)
}

/// Restarts the runner if its process exits, including while CI is disabled.
pub fn start(app: Shared, url: String) {
    tokio::spawn(async move {
        let mut backoff = 2;
        loop {
            let exe = match mint_token(&app).await.map_err(|e| e.message).and_then(|t| {
                std::env::current_exe()
                    .map(|exe| (t, exe))
                    .map_err(|e| format!("cannot find the running executable: {e}"))
            }) {
                Ok(v) => v,
                Err(e) => {
                    tracing::error!("CI: cannot start the automatic runner: {e}");
                    tokio::time::sleep(Duration::from_secs(backoff)).await;
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
                .env("GUILDBOTICS_RUNNER_TOKEN", token)
                .env_remove("GUILDBOTICS_FAILPOINT")
                .stdin(std::process::Stdio::null())
                // The service's stdout carries only its own notices.
                .stdout(std::io::stderr())
                .kill_on_drop(true);
            let started = std::time::Instant::now();
            match cmd.spawn() {
                Ok(mut child) => {
                    tracing::info!("CI: started the automatic runner (pid {:?})", child.id());
                    let status = child.wait().await;
                    tracing::warn!("CI: the automatic runner exited ({status:?}); restarting");
                    stopped(&app).await;
                }
                Err(e) => tracing::error!("CI: cannot start the automatic runner: {e}"),
            }
            if started.elapsed() > Duration::from_secs(300) {
                backoff = 2;
            }
            tokio::time::sleep(Duration::from_secs(backoff)).await;
            backoff = (backoff * 2).min(120);
        }
    });
}

/// A dead child could not remove its microVMs.
async fn stopped(app: &Shared) {
    app.ci.runner_stopped(AUTO_RUNNER);
    app.ci
        .settings()
        .microsandbox(AUTO_RUNNER)
        .await
        .sweep()
        .await;
}
