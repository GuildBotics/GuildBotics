//! Host-owned requests to execute Git inside the command's existing VM.
//! The channel never accepts a host repository path or credentials.
use crate::{Failure, Sentence};
use std::sync::Arc;
use tokio::sync::{Mutex, mpsc, oneshot};

pub struct Process {
    pub argv: Vec<String>,
    pub cwd: String,
    pub env: Vec<(String, String)>,
    pub stdin: Vec<u8>,
    pub limit: usize,
}
pub struct Output {
    pub code: i32,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}
pub(crate) struct Job {
    pub process: Process,
    pub deadline: std::time::Instant,
    pub reply: oneshot::Sender<Result<Output, Failure>>,
}
#[derive(Clone)]
pub struct Channel {
    sender: mpsc::Sender<Job>,
    receiver: Arc<Mutex<Option<mpsc::Receiver<Job>>>>,
}
impl Default for Channel {
    fn default() -> Self {
        Self::new()
    }
}
impl Channel {
    pub fn new() -> Self {
        let (sender, receiver) = mpsc::channel(1);
        Self {
            sender,
            receiver: Arc::new(Mutex::new(Some(receiver))),
        }
    }
    pub async fn execute(&self, process: Process) -> Result<Output, Failure> {
        self.execute_until(
            process,
            std::time::Instant::now() + std::time::Duration::from_secs(300),
        )
        .await
    }
    pub async fn execute_until(
        &self,
        process: Process,
        deadline: std::time::Instant,
    ) -> Result<Output, Failure> {
        if process.argv.is_empty()
            || process.argv.iter().any(|v| v.contains('\0'))
            || process.cwd.contains('\0')
        {
            return Err(Failure::Command(Sentence::key(
                "capabilities.member_git.invalid_path",
            )));
        }
        let (reply, result) = oneshot::channel();
        tokio::time::timeout_at(
            deadline.into(),
            self.sender.send(Job {
                process,
                reply,
                deadline,
            }),
        )
        .await
        .map_err(|_| closed())?
        .map_err(|_| closed())?;
        result.await.map_err(|_| closed())?
    }
    pub(crate) async fn take(&self) -> Result<mpsc::Receiver<Job>, Failure> {
        self.receiver.lock().await.take().ok_or_else(closed)
    }
}
fn closed() -> Failure {
    Failure::Command(Sentence::key("capabilities.member_git.guest_closed"))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn process() -> Process {
        Process {
            argv: vec!["git".into(), "status".into()],
            cwd: "/clones/work".into(),
            env: vec![("GIT_TERMINAL_PROMPT".into(), "0".into())],
            stdin: b"input\0".to_vec(),
            limit: 1024,
        }
    }
    #[tokio::test]
    async fn only_the_vm_owner_takes_the_receiver_and_raw_results_cross_unchanged() {
        let channel = Channel::new();
        let mut jobs = channel.take().await.unwrap();
        assert!(channel.take().await.is_err());
        let client = channel.clone();
        let request = tokio::spawn(async move { client.execute(process()).await });
        let job = jobs.recv().await.unwrap();
        assert_eq!(job.process.cwd, "/clones/work");
        assert_eq!(job.process.stdin, b"input\0");
        assert!(
            job.reply
                .send(Ok(Output {
                    code: 7,
                    stdout: b"raw\xff".to_vec(),
                    stderr: b"reason".to_vec()
                }))
                .is_ok()
        );
        let result = request.await.unwrap().unwrap();
        assert_eq!(result.code, 7);
        assert_eq!(result.stdout, b"raw\xff");
        assert_eq!(result.stderr, b"reason");
        drop(jobs);
        assert!(channel.execute(process()).await.is_err());
    }
    #[tokio::test]
    async fn invalid_input_fails_without_enqueuing_and_disconnection_keeps_an_accepted_job() {
        let channel = Channel::new();
        let mut jobs = channel.take().await.unwrap();
        let mut invalid = process();
        invalid.argv.clear();
        assert!(channel.execute(invalid).await.is_err());
        assert!(jobs.try_recv().is_err());
        let client = channel.clone();
        let request = tokio::spawn(async move { client.execute(process()).await });
        let job = jobs.recv().await.unwrap();
        request.abort();
        let _ = request.await;
        assert_eq!(job.process.argv, ["git", "status"]);
        assert!(
            job.reply
                .send(Ok(Output {
                    code: 0,
                    stdout: vec![],
                    stderr: vec![]
                }))
                .is_err()
        );
    }
}
