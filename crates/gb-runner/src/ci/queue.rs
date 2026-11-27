use serde::{Deserialize, Serialize};
/// A successful execution the runner may reuse after checking its image.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Reuse {
    pub attempt_id: String,
    pub sha: String,
    pub image_digest: String,
    pub basis: Basis,
}

/// What makes a result reusable: the same tree, or the same input digest
/// of the job's paths (`Ci::job_inputs`). Never stored: a reused attempt's
/// basis follows from its run's tree and its source's (`reused_basis`).
#[derive(Serialize, Deserialize, Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub enum Basis {
    Tree,
    Paths,
}

/// Observations from the three intervals of one execution.
#[derive(Serialize, Deserialize, Clone, Copy, Debug, Default, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AttemptResources {
    pub restore: PhaseResources,
    pub commands: PhaseResources,
    pub save: PhaseResources,
    pub guest_peak_memory_mib: Option<u64>,
}

/// Sampled process usage and system-wide CPU load, in basis points.
#[derive(Serialize, Deserialize, Clone, Copy, Debug, Default, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PhaseResources {
    pub disk_write_bytes: Option<u64>,
    pub cpu_time_ms: Option<u64>,
    pub host_cpu_average_bps: Option<u32>,
    pub host_cpu_peak_bps: Option<u32>,
    #[serde(skip)]
    pub(crate) host_cpu_busy_ticks: u64,
    #[serde(skip)]
    pub(crate) host_cpu_total_ticks: u64,
}

impl PhaseResources {
    pub(crate) fn add_usage(&mut self, writes: Option<u64>, cpu: Option<u64>) {
        for (total, observed) in [
            (&mut self.disk_write_bytes, writes),
            (&mut self.cpu_time_ms, cpu),
        ] {
            if let Some(observed) = observed {
                *total = Some(total.unwrap_or_default().saturating_add(observed));
            }
        }
    }
}

/// The execution metadata reported by the runner at finish.
#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct ResultInfo {
    pub image_digest: Option<String>,
    pub peak_memory_mib: Option<u64>,
    pub resources: Option<AttemptResources>,
    pub reused_from: Option<String>,
    pub cache_key: Option<String>,
}

/// What a line of a job's output starts with to make the commit a release
/// point when the job is part of a passing default-branch verification; the
/// rest of the line says why (Branvil's `ci::release`).
pub const RELEASE_POINT_MARKER: &str = "::release-point::";
/// The most release points a job reports, and the longest message kept.
pub const MAX_RELEASE_POINTS: usize = 10;
pub const MAX_RELEASE_POINT_CHARS: usize = 200;
/// The longest line looked at; longer lines are cut there.
const MAX_LINE: usize = 4096;

/// A release point's message as reported and kept.
pub fn release_point_message(text: &str) -> String {
    text.trim().chars().take(MAX_RELEASE_POINT_CHARS).collect()
}

/// The release points of a job's whole output, read as it arrives in pieces
/// of any size, including what its log does not keep.
#[derive(Default)]
pub struct ReleasePoints {
    line: Vec<u8>,
    found: Vec<String>,
}

impl ReleasePoints {
    pub fn read(&mut self, bytes: &[u8]) {
        for &b in bytes {
            if b == b'\n' {
                self.end_line();
            } else if self.line.len() < MAX_LINE {
                self.line.push(b);
            }
        }
    }

    fn end_line(&mut self) {
        let line = String::from_utf8_lossy(&self.line);
        if let Some(message) = line
            .trim_end_matches('\r')
            .strip_prefix(RELEASE_POINT_MARKER)
            && self.found.len() < MAX_RELEASE_POINTS
        {
            self.found.push(release_point_message(message));
        }
        self.line.clear();
    }

    /// The messages found, the output's last line included.
    pub fn finish(mut self) -> Vec<String> {
        if !self.line.is_empty() {
            self.end_line();
        }
        self.found
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn release_points_are_whole_lines_of_the_output_however_it_arrives() {
        let mut points = ReleasePoints::default();
        for piece in [
            &b"build\n::release-"[..],
            b"point:: storage 47 \xe2\x86",
            b"\x92 48 \r\n  ::release-point::indented\n",
            b"say ::release-point::inside\n",
        ] {
            points.read(piece);
        }
        let long = format!("{RELEASE_POINT_MARKER}{}", "x".repeat(MAX_LINE * 2));
        points.read(long.as_bytes());
        points.read(b"\n::release-point::last line");
        let found = points.finish();
        assert_eq!(found[0], "storage 47 → 48");
        assert_eq!(found[1].chars().count(), MAX_RELEASE_POINT_CHARS);
        assert_eq!(found[2], "last line");
        assert_eq!(found.len(), 3);
        let mut many = ReleasePoints::default();
        many.read(
            format!("{RELEASE_POINT_MARKER}x\n")
                .repeat(MAX_RELEASE_POINTS + 5)
                .as_bytes(),
        );
        assert_eq!(many.finish().len(), MAX_RELEASE_POINTS);
    }
}
