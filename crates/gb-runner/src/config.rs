use std::path::{Path, PathBuf};
#[derive(Clone, Debug)]
pub struct Config {
    pub data_dir: PathBuf,
    pub file: PathBuf,
    pub ci: crate::ci::config::CiConfig,
}
impl Config {
    pub fn load(path: Option<&Path>, data: &Path) -> anyhow::Result<Self> {
        let file = path
            .map(Path::to_path_buf)
            .unwrap_or_else(|| data.join("config.toml"));
        #[derive(serde::Deserialize, Default)]
        struct File {
            #[serde(default)]
            ci: crate::ci::config::CiConfig,
        }
        let ci = match std::fs::read_to_string(&file) {
            Ok(s) => toml::from_str::<File>(&s)?.ci,
            Err(e) if path.is_none() && e.kind() == std::io::ErrorKind::NotFound => {
                Default::default()
            }
            Err(e) => return Err(e.into()),
        };
        Ok(Self {
            data_dir: data.into(),
            file,
            ci,
        })
    }
}
pub fn normalize_base_url(value: &str) -> anyhow::Result<String> {
    let url = reqwest::Url::parse(value)?;
    anyhow::ensure!(
        matches!(url.scheme(), "http" | "https")
            && url.host_str().is_some()
            && url.username().is_empty()
            && url.password().is_none()
            && url.query().is_none()
            && url.fragment().is_none()
            && url.path() == "/",
        "Invalid server URL"
    );
    Ok(value.trim_end_matches('/').into())
}
