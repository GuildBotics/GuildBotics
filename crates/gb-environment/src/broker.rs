//! Trusted member execution. The guest never chooses its person or workspace.
use serde::{Deserialize, Serialize};
use std::{path::PathBuf, sync::Arc, time::Duration};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    process::Command,
    sync::Mutex,
};

#[derive(Clone, Serialize)]
pub struct Invocation {
    pub person_id: String,
    pub run_id: String,
    pub trace_id: String,
    pub work_kind: String,
    pub work_identity: String,
    pub cwd: PathBuf,
    pub access: gb_wire::CommandAccess,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub arguments: Vec<String>,
    #[serde(default)]
    pub stdin: String,
    pub turn_grant: String,
}
#[derive(Debug, Serialize)]
pub struct Result {
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
}
impl Result {
    fn refused(message: &str) -> Self {
        Self {
            exit_code: 2,
            stdout: String::new(),
            stderr: message.into(),
        }
    }
}

pub fn rejection(arguments: &[String], person: &str) -> Option<&'static str> {
    if arguments.is_empty() {
        return Some("Member command arguments must not be empty.");
    }
    if arguments.len() > 128 || arguments.iter().map(|v| v.len()).sum::<usize>() > 64 * 1024 {
        return Some("Member command arguments are too large.");
    }
    if arguments.iter().any(|a| a.contains('\0')) {
        return Some("Member command arguments must not contain NUL bytes.");
    }
    if arguments
        .iter()
        .any(|a| a == "--workspace" || a.starts_with("--workspace="))
    {
        return Some("The member capability workspace cannot be overridden.");
    }
    let mut people = vec![];
    let mut index = 0;
    while index < arguments.len() {
        let value = &arguments[index];
        if value == "--person" {
            index += 1;
            if index == arguments.len() {
                return Some("--person requires the active member ID.");
            };
            people.push(arguments[index].as_str());
        } else if let Some(value) = value.strip_prefix("--person=") {
            people.push(value);
        }
        index += 1;
    }
    if people.iter().any(|p| *p != person) {
        return Some("Member capabilities cannot act as another person.");
    }
    if people.is_empty() && arguments != ["help"] {
        return Some("Member commands must name the active person with --person.");
    }
    None
}

/// The member CLI implementation is injected from the layer above the
/// broker (#89). No service CLI or shell is discovered through PATH.
#[derive(Clone)]
pub struct Broker {
    program: PathBuf,
    invocation: Invocation,
    active: Arc<Mutex<Option<String>>>,
    serial: Arc<Mutex<()>>,
    closed: Arc<std::sync::atomic::AtomicBool>,
}
impl Broker {
    pub fn new(program: PathBuf, invocation: Invocation) -> Self {
        Self {
            program,
            invocation,
            active: Arc::new(Mutex::new(None)),
            serial: Arc::new(Mutex::new(())),
            closed: Arc::new(std::sync::atomic::AtomicBool::new(false)),
        }
    }
    pub fn close(&self) {
        self.closed.store(true, std::sync::atomic::Ordering::SeqCst);
    }
    pub async fn activate(&self) -> String {
        let grant = uuid::Uuid::new_v4().simple().to_string();
        *self.active.lock().await = Some(grant.clone());
        grant
    }
    pub async fn deactivate(&self) {
        *self.active.lock().await = None;
    }
    pub async fn execute(&self, request: Request) -> Result {
        let _serial = self.serial.lock().await;
        if self.closed.load(std::sync::atomic::Ordering::SeqCst) {
            return Result::refused("The command broker is closed.");
        }
        if self.active.lock().await.as_deref() != Some(&request.turn_grant) {
            return Result::refused("The GuildBotics turn grant is invalid or expired.");
        }
        if request.stdin.len() > 2 * 1024 * 1024 {
            return Result::refused("Member command stdin is too large.");
        }
        if let Some(reason) = rejection(&request.arguments, &self.invocation.person_id) {
            return Result::refused(reason);
        }
        match tokio::time::timeout(Duration::from_secs(300), self.run(request)).await {
            Ok(Ok(result)) => result,
            Ok(Err(_)) => Result::refused("The member CLI could not execute."),
            Err(_) => Result::refused("The member CLI timed out."),
        }
    }
    async fn run(&self, request: Request) -> std::io::Result<Result> {
        let mut child = Command::new(&self.program)
            .arg("member")
            .args(request.arguments)
            .current_dir(&self.invocation.cwd)
            .env("GUILDBOTICS_RUN_ID", &self.invocation.run_id)
            .env("GUILDBOTICS_TRACE_ID", &self.invocation.trace_id)
            .env("GUILDBOTICS_WORK_KIND", &self.invocation.work_kind)
            .env("GUILDBOTICS_WORK_IDENTITY", &self.invocation.work_identity)
            .env(
                "GUILDBOTICS_MEMBER_INVOCATION",
                serde_json::to_string(&self.invocation).expect("host invocation JSON"),
            )
            .kill_on_drop(true)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()?;
        let mut stdin = child.stdin.take().expect("piped stdin");
        let input = request.stdin.into_bytes();
        let write = async move {
            stdin.write_all(&input).await?;
            stdin.shutdown().await
        };
        let mut stdout = child
            .stdout
            .take()
            .expect("piped stdout")
            .take(10 * 1024 * 1024 + 1);
        let mut stderr = child
            .stderr
            .take()
            .expect("piped stderr")
            .take(10 * 1024 * 1024 + 1);
        let mut out = vec![];
        let mut err = vec![];
        let read =
            async { tokio::try_join!(stdout.read_to_end(&mut out), stderr.read_to_end(&mut err)) };
        let (_, _) = tokio::try_join!(write, read)?;
        if out.len() > 10 * 1024 * 1024 || err.len() > 10 * 1024 * 1024 {
            return Ok(Result::refused("Member CLI output is too large."));
        }
        let code = child.wait().await?.code().unwrap_or(-1);
        Ok(Result {
            exit_code: code,
            stdout: String::from_utf8_lossy(&out).into(),
            stderr: String::from_utf8_lossy(&err).into(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn all_person_spellings_and_workspace_overrides_are_checked() {
        for args in [
            vec!["context", "--person", "other"],
            vec!["context", "--person=other"],
            vec!["context", "--person", "aiko", "--person=other"],
            vec!["context", "--person=aiko", "--workspace=/elsewhere"],
            vec!["context"],
            vec!["context", "--person"],
        ] {
            assert!(
                rejection(
                    &args.into_iter().map(String::from).collect::<Vec<_>>(),
                    "aiko"
                )
                .is_some()
            );
        }
        assert!(rejection(&["help".into()], "aiko").is_none());
        assert!(rejection(&["context".into(), "--person=aiko".into()], "aiko").is_none());
    }
    #[tokio::test]
    async fn expired_grant_never_starts_the_program() {
        let dir = tempfile::tempdir().unwrap();
        let broker = Broker::new(
            dir.path().join("not-a-cli"),
            Invocation {
                person_id: "aiko".into(),
                run_id: "run".into(),
                trace_id: "trace".into(),
                work_kind: "ticket".into(),
                work_identity: "87".into(),
                cwd: dir.path().into(),
                access: gb_wire::CommandAccess {
                    read_only: true,
                    inspects: vec![],
                },
            },
        );
        let grant = broker.activate().await;
        broker.deactivate().await;
        let reply = broker
            .execute(Request {
                arguments: vec!["help".into()],
                stdin: String::new(),
                turn_grant: grant,
            })
            .await;
        assert_eq!(reply.exit_code, 2);
        assert!(reply.stderr.contains("expired"));
    }
}

#[cfg(all(test, unix))]
mod external_tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    #[tokio::test]
    async fn injected_cli_receives_literal_arguments_stdin_and_host_invocation() {
        let dir = tempfile::tempdir().unwrap();
        let program = dir.path().join("fake-cli");
        std::fs::write(&program,"#!/bin/sh\nprintf '%s|%s|%s|%s|' \"$1\" \"$2\" \"$GUILDBOTICS_RUN_ID\" \"$PWD\"\ncat\n").unwrap();
        std::fs::set_permissions(&program, std::fs::Permissions::from_mode(0o700)).unwrap();
        let broker = Broker::new(
            program,
            Invocation {
                person_id: "aiko".into(),
                run_id: "run-id".into(),
                trace_id: "trace".into(),
                work_kind: "ticket".into(),
                work_identity: "87".into(),
                cwd: dir.path().into(),
                access: gb_wire::CommandAccess {
                    read_only: true,
                    inspects: vec![],
                },
            },
        );
        let grant = broker.activate().await;
        let result = broker
            .execute(Request {
                arguments: vec!["context".into(), "--person=aiko".into()],
                stdin: "literal $(false)".into(),
                turn_grant: grant,
            })
            .await;
        assert_eq!(result.exit_code, 0);
        assert!(result.stdout.starts_with("member|context|run-id|"));
        assert!(result.stdout.ends_with("literal $(false)"));
    }
}
