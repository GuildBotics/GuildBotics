use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
    sync::Mutex,
};

pub struct OutputMarker {
    marker: &'static [u8],
    pending: Mutex<Option<Vec<u8>>>,
}

impl OutputMarker {
    pub fn new(marker: &'static [u8]) -> Self {
        Self {
            marker,
            pending: Mutex::new(Some(Vec::new())),
        }
    }

    pub fn observed(&self, chunk: &[u8]) -> bool {
        let mut state = self.pending.lock().unwrap();
        let Some(pending) = &mut *state else {
            return false;
        };
        pending.extend_from_slice(chunk);
        if pending
            .windows(self.marker.len())
            .any(|part| part == self.marker)
        {
            *state = None;
            true
        } else {
            let discard = pending.len().saturating_sub(self.marker.len() - 1);
            pending.drain(..discard);
            false
        }
    }
}

pub fn sandboxes(home: &Path) -> Vec<PathBuf> {
    match fs::read_dir(home.join("sandboxes")) {
        Ok(entries) => entries
            .map(|entry| entry.unwrap().path())
            .filter(|path| {
                path.file_name()
                    .unwrap()
                    .to_string_lossy()
                    .starts_with("guildbotics-")
            })
            .collect(),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => vec![],
        Err(error) => panic!("cannot inspect runtime VM directories: {error}"),
    }
}

pub fn disk_matches(root: &Path, mark: &str) -> Vec<PathBuf> {
    let output = Command::new("grep")
        .args(["-D", "skip", "-rlaF", "--", mark])
        .arg(root)
        .output()
        .unwrap();
    assert!(
        matches!(output.status.code(), Some(0 | 1)),
        "disk scan failed under {}: {}",
        root.display(),
        String::from_utf8_lossy(&output.stderr),
    );
    String::from_utf8(output.stdout)
        .unwrap()
        .lines()
        .map(PathBuf::from)
        .collect()
}

pub fn assert_private_disk(home: &Path, canary: &str, login: &str) {
    let live = sandboxes(home);
    assert_eq!(live.len(), 1, "expected one VM while scanning");
    assert_eq!(disk_matches(&live[0], canary), [live[0].join("upper.ext4")]);
    assert!(
        disk_matches(home, login).is_empty(),
        "a login reached the runtime disk"
    );
}
