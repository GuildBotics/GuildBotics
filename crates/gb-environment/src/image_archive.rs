//! Read the archive's CPU before the runtime labels it with the host's CPU.

use std::fs::File;
use std::io::{BufRead, BufReader, Read};
use std::path::Path;

use serde_json::Value;

type Error = Box<dyn std::error::Error + Send + Sync>;

/// The SDK reads seekable, uncompressed tar. Keep decompression temporary,
/// including when loading fails; archive paths are never extracted.
pub fn uncompressed(path: &Path) -> Result<Option<tempfile::NamedTempFile>, Error> {
    let mut reader = BufReader::new(File::open(path)?);
    if !reader.fill_buf()?.starts_with(&[0x1f, 0x8b]) {
        return Ok(None);
    }
    let mut output = tempfile::NamedTempFile::new()?;
    std::io::copy(&mut flate2::read::GzDecoder::new(reader), &mut output)?;
    Ok(Some(output))
}

/// Match the frozen host's first-image policy for Docker and OCI archives.
/// Metadata is read in place; no archive member is extracted to the host.
pub fn architecture(path: &Path) -> Result<String, Error> {
    if let Some(manifest) = read(path, "manifest.json")? {
        let entries = manifest.as_array().ok_or("invalid Docker manifest")?;
        if let Some(first) = entries.first() {
            let config = first["Config"].as_str().ok_or("missing Docker config")?;
            return cpu(&required(path, config)?);
        }
    }
    let mut descriptor = required(path, "index.json")?;
    for _ in 0..2 {
        let Some(first) = descriptor["manifests"].as_array().and_then(|m| m.first()) else {
            return Ok(String::new());
        };
        if let Some(architecture) = first["platform"]["architecture"].as_str() {
            return Ok(architecture.into());
        }
        descriptor = required(path, &blob(&first["digest"])?)?;
        if descriptor.get("config").is_some() {
            return cpu(&required(path, &blob(&descriptor["config"]["digest"])?)?);
        }
    }
    Ok(String::new())
}

pub fn validate(path: &Path, device: &str) -> Result<(), Error> {
    let architecture = architecture(path)
        .map_err(|error| format!("Could not read image archive {}: {error}", path.display()))?;
    if !architecture.is_empty() && architecture != device {
        return Err(format!(
            "The archive {} holds an image built for {architecture}, which this device ({device}) cannot run.",
            path.display()
        ).into());
    }
    Ok(())
}

fn cpu(config: &Value) -> Result<String, Error> {
    match config.get("architecture") {
        None => Ok(String::new()),
        Some(Value::String(value)) => Ok(value.clone()),
        _ => Err("invalid image architecture".into()),
    }
}

fn blob(digest: &Value) -> Result<String, Error> {
    let (algorithm, hex) = digest
        .as_str()
        .and_then(|s| s.split_once(':'))
        .ok_or("invalid OCI digest")?;
    Ok(format!("blobs/{algorithm}/{hex}"))
}

fn required(path: &Path, name: &str) -> Result<Value, Error> {
    read(path, name)?.ok_or_else(|| format!("missing archive member {name}").into())
}

fn read(path: &Path, name: &str) -> Result<Option<Value>, Error> {
    let mut file = BufReader::new(File::open(path)?);
    let compressed = file.fill_buf()?.starts_with(&[0x1f, 0x8b]);
    let reader: Box<dyn Read> = if compressed {
        Box::new(flate2::read::GzDecoder::new(file))
    } else {
        Box::new(file)
    };
    for entry in tar::Archive::new(reader).entries()? {
        let entry = entry?;
        if entry.path()?.as_ref() == Path::new(name) {
            if !entry.header().entry_type().is_file() {
                return Err("image metadata must be a regular file".into());
            }
            return Ok(Some(serde_json::from_reader(entry)?));
        }
    }
    Ok(None)
}
