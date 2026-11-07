//! Device-local, non-secret debug flags shared by the host and its commands.
use serde::Serialize;
use std::path::Path;

#[derive(Serialize)]
pub struct Settings {
    pub enabled: bool,
    pub log_level: String,
    pub agno_debug: bool,
    #[serde(skip)]
    pub configured: bool,
}

pub fn read(workspace: Option<&Path>) -> Settings {
    read_with(workspace, |key| std::env::var(key).ok())
}

fn read_with(workspace: Option<&Path>, inherited: impl Fn(&str) -> Option<String>) -> Settings {
    let text = workspace
        .and_then(|w| std::fs::read_to_string(w.join(".guildbotics/local/debug.env")).ok())
        .unwrap_or_default();
    let values = text
        .lines()
        .filter_map(|line| line.trim().trim_start_matches("export ").split_once('='))
        .map(|(key, value)| {
            let value = value.trim();
            let value = if value.starts_with(['\'', '"']) {
                value.trim_matches(['\'', '"']).to_owned()
            } else {
                value.split(" #").next().unwrap_or("").trim().to_owned()
            };
            (key.trim().to_owned(), value)
        })
        .collect::<std::collections::BTreeMap<_, _>>();
    let get = |key: &str| {
        values
            .get(key)
            .filter(|v| !v.is_empty())
            .cloned()
            .or_else(|| inherited(key))
            .unwrap_or_default()
    };
    let level = get("LOG_LEVEL").trim().to_uppercase();
    let configured = !level.is_empty();
    let log_level = if level.is_empty() {
        "INFO".into()
    } else {
        level
    };
    let agno_debug = matches!(
        get("AGNO_DEBUG").trim().to_ascii_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    );
    Settings {
        configured,
        enabled: log_level == "DEBUG" || agno_debug,
        log_level,
        agno_debug,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn debug_file_wins_over_inherited_values_and_ignores_other_keys() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join(".guildbotics/local/debug.env");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        for (level, agno) in [("DEBUG", false), ("INFO", true), ("WARNING", false)] {
            std::fs::write(&path,format!("export LOG_LEVEL=' {level} '\nAGNO_DEBUG={} # comment\nPRIVATE_KEY=never-read\n",if agno{"YES"}else{"false"})).unwrap();
            let value = read_with(Some(root.path()), |_| Some("DEBUG".into()));
            assert_eq!(value.log_level, level);
            assert_eq!(value.agno_debug, agno);
            assert_eq!(value.enabled, level == "DEBUG" || agno);
        }
        std::fs::remove_file(path).unwrap();
        let value = read_with(Some(root.path()), |key| match key {
            "LOG_LEVEL" => Some("debug".into()),
            "AGNO_DEBUG" => Some("on".into()),
            _ => panic!("only allowlisted keys"),
        });
        assert_eq!(value.log_level, "DEBUG");
        assert!(value.agno_debug);
        let value = read_with(None, |_| None);
        assert_eq!(value.log_level, "INFO");
        assert!(!value.enabled);
        assert!(!value.configured);
    }
}
