use serde::{Deserialize, Serialize};
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct Image {
    pub reference: String,
    pub architecture: String,
    pub digest: String,
}

pub fn architecture() -> &'static str {
    match std::env::consts::ARCH {
        "aarch64" => "arm64",
        "x86_64" => "amd64",
        a => a,
    }
}

pub fn same_reference(a: &str, b: &str) -> bool {
    match (
        a.parse::<oci_spec::distribution::Reference>(),
        b.parse::<oci_spec::distribution::Reference>(),
    ) {
        (Ok(a), Ok(b)) => a == b,
        _ => false,
    }
}

impl Image {
    pub fn supports(&self, reference: &str) -> bool {
        same_reference(&self.reference, reference)
            && !self.digest.is_empty()
            && matches!(self.architecture.as_str(), "arm64" | "amd64")
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Archive {
    pub images: Vec<Image>,
    pub id: String,
}
