use super::*;

#[derive(Default)]
struct Input {
    chunks: std::sync::Mutex<Vec<Vec<u8>>>,
    fail_at: Option<usize>,
}
impl ChunkInput for Input {
    async fn write_chunk<'a>(&'a self, bytes: &'a [u8]) -> Result<(), String> {
        let mut chunks = self.chunks.lock().unwrap();
        if self.fail_at == Some(chunks.len()) {
            return Err("synthetic write failure".into());
        }
        assert!(bytes.len() < 4 * 1024 * 1024);
        chunks.push(bytes.to_vec());
        Ok(())
    }
}

#[tokio::test]
async fn stdin_preserves_every_byte_in_bounded_ordered_writes() {
    for size in [
        0,
        1,
        STDIN_CHUNK_BYTES - 1,
        STDIN_CHUNK_BYTES,
        STDIN_CHUNK_BYTES + 1,
        5 * 1024 * 1024 + 73,
    ] {
        let bytes: Vec<u8> = (0..size)
            .map(|i| ((i * 31 + i / 251) % 256) as u8)
            .collect();
        let input = Input::default();
        write_stdin(&input, &bytes).await.unwrap();
        let chunks = input.chunks.lock().unwrap();
        assert_eq!(chunks.len(), size.div_ceil(STDIN_CHUNK_BYTES));
        assert!(
            chunks
                .iter()
                .all(|c| !c.is_empty() && c.len() <= STDIN_CHUNK_BYTES)
        );
        assert_eq!(chunks.concat(), bytes);
    }
}

#[tokio::test]
async fn stdin_stops_at_the_first_write_failure() {
    let input = Input {
        fail_at: Some(1),
        ..Default::default()
    };
    assert_eq!(
        write_stdin(&input, &vec![42; 3 * STDIN_CHUNK_BYTES])
            .await
            .unwrap_err(),
        "synthetic write failure"
    );
    assert_eq!(input.chunks.lock().unwrap().len(), 1);
}

struct Events(std::collections::VecDeque<ExecEvent>);
impl CommandEvents for Events {
    async fn next(&mut self) -> Option<ExecEvent> {
        self.0.pop_front()
    }
}

#[tokio::test]
async fn guest_output_keeps_exit_status_and_failure_categories() {
    let mut events = Events(
        [
            ExecEvent::Stdout(b"abc".to_vec().into()),
            ExecEvent::Stderr(b"cause".to_vec().into()),
            ExecEvent::Exited { code: 7 },
        ]
        .into(),
    );
    let output = read_guest_output(&mut events, 3).await.unwrap();
    assert_eq!(output.code, 7);
    assert_eq!(output.stdout, b"abc");
    assert_eq!(output.stderr, b"cause");
    for (event, limit, key) in [
        (
            Some(ExecEvent::Stdout(vec![0; 4].into())),
            3,
            "capabilities.member_git.guest_stdout_limit",
        ),
        (
            Some(ExecEvent::Stderr(vec![0; (1 << 24) + 1].into())),
            3,
            "capabilities.member_git.guest_stderr_limit",
        ),
        (None, 3, "capabilities.member_git.guest_stream_closed"),
    ] {
        let mut events = Events(event.into_iter().collect());
        assert!(
            matches!(read_guest_output(&mut events, limit).await, Err(Failure::Command(Sentence::Key(actual, _))) if actual == key)
        );
    }
}
