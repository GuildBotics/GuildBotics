//! A minimal zip writer (stored entries, no compression, no zip64): enough for
//! CI artifacts, which are capped far below 4 GiB.

use std::io::{self, Read, Seek, Write};

struct Entry {
    name: String,
    crc: u32,
    size: u32,
    offset: u32,
}

pub struct ZipWriter<W: Write + Seek> {
    out: W,
    entries: Vec<Entry>,
}

const DOS_TIME: u16 = 0;
/// 1980-01-01.
const DOS_DATE: u16 = (1 << 5) | 1;
/// General purpose flag bit 11: names are UTF-8.
const UTF8: u16 = 1 << 11;

fn too_big() -> io::Error {
    io::Error::other("the archive would exceed 4 GiB")
}

impl<W: Write + Seek> ZipWriter<W> {
    pub fn new(out: W) -> Self {
        Self {
            out,
            entries: Vec::new(),
        }
    }

    /// Adds a file. `name` is a `/`-separated relative path.
    pub fn add(&mut self, name: &str, mut data: impl Read) -> io::Result<()> {
        let offset = u32::try_from(self.out.stream_position()?).map_err(|_| too_big())?;
        let mut content = Vec::new();
        data.read_to_end(&mut content)?;
        let size = u32::try_from(content.len()).map_err(|_| too_big())?;
        let crc = crc32fast::hash(&content);
        let n = name.as_bytes();
        let mut h = Vec::with_capacity(30 + n.len());
        h.extend_from_slice(&0x0403_4b50u32.to_le_bytes());
        h.extend_from_slice(&20u16.to_le_bytes()); // version needed
        h.extend_from_slice(&UTF8.to_le_bytes());
        h.extend_from_slice(&0u16.to_le_bytes()); // stored
        h.extend_from_slice(&DOS_TIME.to_le_bytes());
        h.extend_from_slice(&DOS_DATE.to_le_bytes());
        h.extend_from_slice(&crc.to_le_bytes());
        h.extend_from_slice(&size.to_le_bytes());
        h.extend_from_slice(&size.to_le_bytes());
        h.extend_from_slice(&(n.len() as u16).to_le_bytes());
        h.extend_from_slice(&0u16.to_le_bytes()); // extra
        h.extend_from_slice(n);
        self.out.write_all(&h)?;
        self.out.write_all(&content)?;
        self.entries.push(Entry {
            name: name.to_string(),
            crc,
            size,
            offset,
        });
        Ok(())
    }

    pub fn finish(mut self) -> io::Result<W> {
        let start = u32::try_from(self.out.stream_position()?).map_err(|_| too_big())?;
        for e in &self.entries {
            let n = e.name.as_bytes();
            let mut h = Vec::with_capacity(46 + n.len());
            h.extend_from_slice(&0x0201_4b50u32.to_le_bytes());
            h.extend_from_slice(&((3u16 << 8) | 20).to_le_bytes()); // made by: Unix, 2.0
            h.extend_from_slice(&20u16.to_le_bytes());
            h.extend_from_slice(&UTF8.to_le_bytes());
            h.extend_from_slice(&0u16.to_le_bytes());
            h.extend_from_slice(&DOS_TIME.to_le_bytes());
            h.extend_from_slice(&DOS_DATE.to_le_bytes());
            h.extend_from_slice(&e.crc.to_le_bytes());
            h.extend_from_slice(&e.size.to_le_bytes());
            h.extend_from_slice(&e.size.to_le_bytes());
            h.extend_from_slice(&(n.len() as u16).to_le_bytes());
            h.extend_from_slice(&0u16.to_le_bytes()); // extra
            h.extend_from_slice(&0u16.to_le_bytes()); // comment
            h.extend_from_slice(&0u16.to_le_bytes()); // disk
            h.extend_from_slice(&0u16.to_le_bytes()); // internal attributes
            h.extend_from_slice(&((0o100644u32) << 16).to_le_bytes()); // external: regular file
            h.extend_from_slice(&e.offset.to_le_bytes());
            h.extend_from_slice(n);
            self.out.write_all(&h)?;
        }
        let end = u32::try_from(self.out.stream_position()?).map_err(|_| too_big())?;
        let count = u16::try_from(self.entries.len()).map_err(|_| too_big())?;
        let mut eocd = Vec::with_capacity(22);
        eocd.extend_from_slice(&0x0605_4b50u32.to_le_bytes());
        eocd.extend_from_slice(&0u16.to_le_bytes());
        eocd.extend_from_slice(&0u16.to_le_bytes());
        eocd.extend_from_slice(&count.to_le_bytes());
        eocd.extend_from_slice(&count.to_le_bytes());
        eocd.extend_from_slice(&(end - start).to_le_bytes());
        eocd.extend_from_slice(&start.to_le_bytes());
        eocd.extend_from_slice(&0u16.to_le_bytes());
        self.out.write_all(&eocd)?;
        self.out.flush()?;
        Ok(self.out)
    }
}

/// The file names of a zip made by `ZipWriter` (for tests and checks).
#[cfg(test)]
pub fn names(data: &[u8]) -> Vec<String> {
    let eocd = data.len() - 22;
    let count = u16::from_le_bytes([data[eocd + 10], data[eocd + 11]]) as usize;
    let mut pos = u32::from_le_bytes(data[eocd + 16..eocd + 20].try_into().unwrap()) as usize;
    let mut out = Vec::new();
    for _ in 0..count {
        let n = u16::from_le_bytes([data[pos + 28], data[pos + 29]]) as usize;
        out.push(String::from_utf8_lossy(&data[pos + 46..pos + 46 + n]).into_owned());
        pos += 46 + n;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn writes_a_zip_unzip_reads() {
        let mut z = ZipWriter::new(io::Cursor::new(Vec::new()));
        z.add("a.txt", &b"hello\n"[..]).unwrap();
        z.add("dir/b.txt", &b"world\n"[..]).unwrap();
        let data = z.finish().unwrap().into_inner();
        assert_eq!(&data[..4], b"PK\x03\x04");
        assert_eq!(names(&data), vec!["a.txt", "dir/b.txt"]);
        // `unzip -t` verifies CRCs and structure when it is installed.
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("t.zip");
        std::fs::write(&path, &data).unwrap();
        if let Ok(out) = std::process::Command::new("unzip")
            .arg("-t")
            .arg(&path)
            .output()
        {
            assert!(
                out.status.success(),
                "{}",
                String::from_utf8_lossy(&out.stdout)
            );
        }
    }
}
