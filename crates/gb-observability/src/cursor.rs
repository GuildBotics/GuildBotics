//! Incremental complete-row reader, including relocation after retained-tail rotation.
use crate::Diagnostics;
use base64::{Engine, engine::general_purpose::STANDARD};
use serde_json::{Value, json};
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom};

impl Diagnostics {
    pub(crate) fn records_after(&self, cursor: Option<&Value>) -> (Vec<Value>, Value) {
        let empty = || {
            (
                Vec::new(),
                json!({"offset":0,"device":0,"inode":0,"anchor":""}),
            )
        };
        let Ok(file) = std::fs::File::open(&self.index) else {
            return empty();
        };
        let Ok(meta) = file.metadata() else {
            return empty();
        };
        #[cfg(unix)]
        let (device, inode) = {
            use std::os::unix::fs::MetadataExt;
            (meta.dev(), meta.ino())
        };
        #[cfg(not(unix))]
        let (device, inode) = (0_u64, 0_u64);
        let mut reader = BufReader::new(file);
        let mut offset = 0;
        if let Some(cursor) = cursor {
            let anchor = STANDARD
                .decode(cursor["anchor"].as_str().unwrap_or_default())
                .unwrap_or_default();
            let at = cursor["offset"].as_u64().unwrap_or_default();
            if !anchor.is_empty() {
                let mut actual = vec![0; anchor.len()];
                if cursor["device"] == device
                    && cursor["inode"] == inode
                    && at <= meta.len()
                    && at >= anchor.len() as u64
                    && reader
                        .seek(SeekFrom::Start(at - anchor.len() as u64))
                        .is_ok()
                    && reader.read_exact(&mut actual).is_ok()
                    && actual == anchor
                {
                    offset = at;
                } else {
                    let mut all = Vec::new();
                    if reader.seek(SeekFrom::Start(0)).is_ok()
                        && reader.read_to_end(&mut all).is_ok()
                        && let Some(at) = all.windows(anchor.len()).rposition(|s| s == anchor)
                    {
                        offset = (at + anchor.len()) as u64;
                    }
                }
            }
        }
        if reader.seek(SeekFrom::Start(offset)).is_err() {
            return empty();
        }
        let mut records = Vec::new();
        let mut line = Vec::new();
        loop {
            line.clear();
            if reader.read_until(b'\n', &mut line).is_err() || line.last() != Some(&b'\n') {
                break;
            }
            offset += line.len() as u64;
            if let Ok(value) = serde_json::from_slice::<Value>(&line)
                && value.is_object()
            {
                records.push(value);
            }
        }
        let start = offset.saturating_sub(256);
        let mut anchor = vec![0; (offset - start) as usize];
        let _ = reader.seek(SeekFrom::Start(start));
        let _ = reader.read_exact(&mut anchor);
        if anchor.last() == Some(&b'\n')
            && let Some(i) = anchor[..anchor.len() - 1].iter().rposition(|b| *b == b'\n')
        {
            anchor.drain(..=i);
        }
        (
            records,
            json!({"offset":offset,"device":device,"inode":inode,"anchor":STANDARD.encode(anchor)}),
        )
    }
}
