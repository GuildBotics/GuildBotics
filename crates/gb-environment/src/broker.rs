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

const OUTPUT_LIMIT: u64 = 10 * 1024 * 1024;
async fn read_output(reader: impl tokio::io::AsyncRead + Unpin) -> std::io::Result<Vec<u8>> {
    let mut bytes = Vec::new();
    reader
        .take(OUTPUT_LIMIT + 1)
        .read_to_end(&mut bytes)
        .await?;
    if bytes.len() as u64 > OUTPUT_LIMIT {
        return Err(std::io::Error::new(
            std::io::ErrorKind::FileTooLarge,
            "Member CLI output is too large.",
        ));
    }
    Ok(bytes)
}

/// The member CLI implementation is injected from the layer above the
/// broker (#89). No service CLI or shell is discovered through PATH.
#[derive(Clone)]
pub struct Broker {
    tracker: Option<tokio_util::task::TaskTracker>,
    handler: Option<Handler>,
    program: PathBuf,
    invocation: Invocation,
    active: Arc<Mutex<Option<String>>>,
    serial: Arc<Mutex<()>>,
    closed: Arc<std::sync::atomic::AtomicBool>,
}
pub type Handler = Arc<
    dyn Fn(
            Vec<String>,
            String,
            Invocation,
        ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result> + Send>>
        + Send
        + Sync,
>;
impl Broker {
    pub fn tracked(mut self, tracker: Option<tokio_util::task::TaskTracker>) -> Self {
        self.tracker = tracker;
        self
    }
    pub fn native(invocation: Invocation, handler: Handler) -> Self {
        let mut broker = Self::new(PathBuf::new(), invocation);
        broker.handler = Some(handler);
        broker
    }
    pub fn new(program: PathBuf, invocation: Invocation) -> Self {
        Self {
            tracker: None,
            handler: None,
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
    pub async fn settle(&self) {
        self.close();
        let _serial = self.serial.lock().await;
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
        self.execute_with_timeout(request, Duration::from_secs(300))
            .await
    }
    async fn execute_with_timeout(&self, request: Request, timeout: Duration) -> Result {
        let serial = self.serial.clone().lock_owned().await;
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
        if let Some(handler) = self.handler.clone() {
            let invocation = self.invocation.clone();
            // Timeout ends only the response wait. The accepted operation owns
            // its serial guard until its write and evidence recording settle.
            let task = async move {
                let _serial = serial;
                handler(request.arguments, request.stdin, invocation).await
            };
            let mut worker = match &self.tracker {
                Some(tracker) => tracker.spawn(task),
                None => tokio::spawn(task),
            };
            return match tokio::time::timeout(timeout, &mut worker).await {
                Ok(Ok(result)) => result,
                Ok(Err(_)) => Result::refused("The member command worker failed."),
                Err(_) => Result {
                    exit_code: 124,
                    stdout: String::new(),
                    stderr: "The member command timed out.".into(),
                },
            };
        }
        let _serial = serial;
        match tokio::time::timeout(timeout, self.run(request)).await {
            Ok(Ok(result)) => result,
            Ok(Err(error)) if error.kind() == std::io::ErrorKind::FileTooLarge => {
                Result::refused("Member CLI output is too large.")
            }
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
        let stdout = child.stdout.take().expect("piped stdout");
        let stderr = child.stderr.take().expect("piped stderr");
        // Either overflow ends the join immediately; dropping the child then
        // closes both pipes and kills the process, even for infinite output.
        let read = async { tokio::try_join!(read_output(stdout), read_output(stderr)) };
        let (_, (out, err)) = tokio::try_join!(write, read)?;
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
    #[tokio::test]
    async fn native_timeout_retains_serialization_and_shutdown_waits_for_the_accepted_write() {
        let tracker = tokio_util::task::TaskTracker::new();
        let started = Arc::new(tokio::sync::Notify::new());
        let release = Arc::new(tokio::sync::Notify::new());
        let writes = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let handler: Handler = {
            let started = started.clone();
            let release = release.clone();
            let writes = writes.clone();
            Arc::new(move |arguments, stdin, invocation| {
                let started = started.clone();
                let release = release.clone();
                let writes = writes.clone();
                Box::pin(async move {
                    assert_eq!(arguments, ["help"]);
                    assert_eq!(stdin, "body");
                    assert_eq!(invocation.person_id, "aiko");
                    started.notify_one();
                    release.notified().await;
                    writes.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                    Result {
                        exit_code: 0,
                        stdout: "finished".into(),
                        stderr: String::new(),
                    }
                })
            })
        };
        let broker = Broker::native(
            Invocation {
                person_id: "aiko".into(),
                run_id: "run".into(),
                trace_id: "trace".into(),
                work_kind: "ticket".into(),
                work_identity: "ticket".into(),
                cwd: "/guest".into(),
                access: gb_wire::CommandAccess {
                    read_only: false,
                    inspects: vec![],
                },
            },
            handler,
        )
        .tracked(Some(tracker.clone()));
        let grant = broker.activate().await;
        let result = broker
            .execute_with_timeout(
                Request {
                    arguments: vec!["help".into()],
                    stdin: "body".into(),
                    turn_grant: grant.clone(),
                },
                Duration::from_millis(1),
            )
            .await;
        assert_eq!(result.exit_code, 124);
        started.notified().await;
        assert_eq!(tracker.len(), 1);
        assert_eq!(writes.load(std::sync::atomic::Ordering::SeqCst), 0);
        broker.deactivate().await;
        broker.close();
        tracker.close();
        assert!(!tracker.is_empty());
        release.notify_one();
        tracker.wait().await;
        broker.settle().await;
        assert_eq!(writes.load(std::sync::atomic::Ordering::SeqCst), 1);
        assert_eq!(
            broker
                .execute(Request {
                    arguments: vec!["help".into()],
                    stdin: String::new(),
                    turn_grant: grant
                })
                .await
                .exit_code,
            2
        );
    }
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
    fn invocation(root: &std::path::Path) -> Invocation {
        Invocation {
            person_id: "aiko".into(),
            run_id: "run".into(),
            trace_id: "trace".into(),
            work_kind: "ticket".into(),
            work_identity: "87".into(),
            cwd: root.into(),
            access: gb_wire::CommandAccess {
                read_only: true,
                inspects: vec![],
            },
        }
    }
    fn script(root: &std::path::Path, text: &str) -> PathBuf {
        let program = root.join("member-cli");
        std::fs::write(&program, format!("#!/bin/sh\n{text}\n")).unwrap();
        std::fs::set_permissions(&program, std::fs::Permissions::from_mode(0o700)).unwrap();
        program
    }
    fn request(grant: &str, command: &str) -> Request {
        Request {
            arguments: vec![command.into(), "--person=aiko".into()],
            stdin: String::new(),
            turn_grant: grant.into(),
        }
    }
    #[tokio::test]
    async fn member_timeout_and_request_cancellation_release_the_serial_slot() {
        let dir = tempfile::tempdir().unwrap();
        let program = script(
            dir.path(),
            r#"case "$2" in context) exec sleep 30;; *) printf done;; esac"#,
        );
        let broker = Broker::new(program, invocation(dir.path()));
        let grant = broker.activate().await;
        let timeout = broker
            .execute_with_timeout(request(&grant, "context"), Duration::from_millis(30))
            .await;
        assert_eq!(timeout.exit_code, 2);
        assert!(timeout.stderr.contains("timed out"));
        assert_eq!(broker.execute(request(&grant, "help")).await.stdout, "done");
        let busy = broker.serial.lock().await;
        let queued_broker = broker.clone();
        let queued_grant = grant.clone();
        let queued = tokio::spawn(async move {
            queued_broker
                .execute(request(&queued_grant, "context"))
                .await
        });
        tokio::task::yield_now().await;
        queued.abort();
        assert!(queued.await.unwrap_err().is_cancelled());
        broker.deactivate().await;
        drop(busy);
        assert_eq!(broker.execute(request(&grant, "help")).await.exit_code, 2);
        let fresh = broker.activate().await;
        assert_eq!(broker.execute(request(&fresh, "help")).await.stdout, "done");
        broker.close();
        assert!(
            broker
                .execute(request(&fresh, "help"))
                .await
                .stderr
                .contains("closed")
        );
    }
    #[tokio::test]
    async fn every_member_request_gets_the_host_invocation_and_never_another_turns_authority() {
        let dir = tempfile::tempdir().unwrap();
        let program = script(
            dir.path(),
            r#"printf '%s\n' "$GUILDBOTICS_MEMBER_INVOCATION" "$GUILDBOTICS_TRACE_ID" "$GUILDBOTICS_WORK_KIND" "$GUILDBOTICS_WORK_IDENTITY"
cat"#,
        );
        let broker = Broker::new(program, invocation(dir.path()));
        let stale = broker.activate().await;
        let grant = broker.activate().await;
        assert!(
            broker
                .execute(request(&stale, "help"))
                .await
                .stderr
                .contains("expired")
        );
        for _ in 0..2 {
            let mut input = request(&grant, "context");
            input.stdin = "literal $(false)".into();
            let output = broker.execute(input).await;
            assert_eq!(output.exit_code, 0);
            let mut lines = output.stdout.lines();
            let metadata: serde_json::Value = serde_json::from_str(lines.next().unwrap()).unwrap();
            assert_eq!(metadata["person_id"], "aiko");
            assert_eq!(metadata["cwd"], dir.path().to_string_lossy().as_ref());
            assert_eq!(metadata["access"]["read_only"], true);
            assert_eq!(
                lines.collect::<Vec<_>>(),
                ["trace", "ticket", "87", "literal $(false)"]
            );
        }
    }
    #[tokio::test]
    async fn oversized_output_is_bounded_without_waiting_for_a_blocked_child() {
        let dir = tempfile::tempdir().unwrap();
        let broker = Broker::new(
            script(dir.path(), "head -c 20971520 /dev/zero"),
            invocation(dir.path()),
        );
        let grant = broker.activate().await;
        let result = broker
            .execute_with_timeout(request(&grant, "help"), Duration::from_secs(2))
            .await;
        assert_eq!(result.exit_code, 2);
        assert!(
            result.stderr.contains("output is too large"),
            "{}",
            result.stderr
        );
        assert!(result.stdout.is_empty());
    }
}
