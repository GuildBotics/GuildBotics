//! Which workspace the server opens, chosen as the Python version chooses it
//! (`guildbotics/app_api/server.py`, `guildbotics/utils/workspace_state.py`):
//! the one named explicitly, else the one last selected on this machine. Every
//! workspace opened is remembered in the machine's registry, which protects
//! the `.guildbotics` of the ones not selected too.

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::time::Duration;

use gb_base::files::{self, FileLock};
use gb_base::host_path;

/// Names the workspace explicitly, over the one selected on the machine.
pub const WORKSPACE_ROOT_ENV: &str = "GUILDBOTICS_WORKSPACE_ROOT";
/// Names the workspace by its `<workspace>/.guildbotics/config`.
pub const CONFIG_DIR_ENV: &str = "GUILDBOTICS_CONFIG_DIR";

pub const ACTIVE_FILE: &str = "active-workspace.json";
const REGISTRY_FILE: &str = "workspaces.json";
const REGISTRY_LOCK: &str = "workspaces.lock";
const REGISTRY_TIMEOUT: Duration = Duration::from_secs(30);

/// The workspace the server opens on this machine (`data` is the machine's
/// `<machine root>/data`), if any, as `variable` (`std::env::var_os`) names it.
pub fn restore(
    data: &Path,
    variable: impl Fn(&str) -> Option<OsString>,
) -> anyhow::Result<Option<PathBuf>> {
    let Some(workspace) = resolve(data, variable)? else {
        return Ok(None);
    };
    register(data, &workspace)?;
    Ok(Some(workspace))
}

/// Read the explicit or persisted selection without modifying the registry.
pub fn resolve(
    data: &Path,
    variable: impl Fn(&str) -> Option<OsString>,
) -> anyhow::Result<Option<PathBuf>> {
    match specified(variable(WORKSPACE_ROOT_ENV), variable(CONFIG_DIR_ENV))? {
        Some(explicit) => Ok(Some(explicit)),
        None => read_active(data),
    }
}

/// What the variables name, the root over the config directory, which names
/// a workspace only when it is one's `.guildbotics/config`.
pub fn specified(
    root: Option<OsString>,
    config_dir: Option<OsString>,
) -> anyhow::Result<Option<PathBuf>> {
    let given = |value: Option<OsString>| {
        value
            // Trimmed as the Python version trims it; a name that is not
            // text is taken as it is.
            .map(|v| match v.to_str() {
                Some(text) => PathBuf::from(text.trim()),
                None => PathBuf::from(v),
            })
            .filter(|v| !v.as_os_str().is_empty())
    };
    if let Some(root) = given(root) {
        return Ok(Some(host_path::directory(&root, true)?));
    }
    let Some(config_dir) = given(config_dir) else {
        return Ok(None);
    };
    let config_dir = host_path::directory(&config_dir, true)?;
    let shaped = config_dir.file_name() == Some("config".as_ref())
        && config_dir.parent().and_then(Path::file_name) == Some(".guildbotics".as_ref());
    Ok(shaped
        .then(|| {
            config_dir
                .parent()
                .and_then(Path::parent)
                .map(Path::to_path_buf)
        })
        .flatten())
}

/// The workspace last selected on this machine, when it still is a directory.
pub fn read_active(data: &Path) -> anyhow::Result<Option<PathBuf>> {
    let Ok(bytes) = std::fs::read(data.join(ACTIVE_FILE)) else {
        return Ok(None);
    };
    let Ok(serde_json::Value::Object(state)) = serde_json::from_slice(&bytes) else {
        return Ok(None);
    };
    let Some(named) = state.get("workspace").and_then(|v| v.as_str()) else {
        return Ok(None);
    };
    if named.trim().is_empty() {
        return Ok(None);
    }
    let workspace = host_path::directory(Path::new(named), true)?;
    Ok(workspace.is_dir().then_some(workspace))
}

/// Save the selected workspace after registering it for filesystem protection.
pub fn write_active(data: &Path, workspace: &Path) -> anyhow::Result<()> {
    register(data, workspace)?;
    let record = serde_json::json!({
        "workspace": workspace,
        "config_dir": workspace.join(".guildbotics/config"),
    });
    files::atomic_write(
        &data.join(ACTIVE_FILE),
        files::dump_shared_json(&record).as_bytes(),
    )?;
    Ok(())
}

#[derive(Debug)]
pub struct RegistryError {
    pub path: PathBuf,
    pub reason: String,
}

impl std::fmt::Display for RegistryError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}: {}", self.path.display(), self.reason)
    }
}

impl std::error::Error for RegistryError {}

/// The workspaces this machine has opened.
fn registered(data: &Path) -> anyhow::Result<Vec<PathBuf>> {
    let path = data.join(REGISTRY_FILE);
    let failed = |reason| RegistryError {
        path: path.clone(),
        reason,
    };
    let Some(bytes) =
        host_path::read_file_if_present(&path).map_err(|error| failed(error.to_string()))?
    else {
        return Ok(Vec::new());
    };
    let named: Vec<String> =
        serde_json::from_slice(&bytes).map_err(|error| failed(error.to_string()))?;
    named
        .iter()
        .map(|name| {
            host_path::normalize(Path::new(name)).map_err(|error| failed(error.to_string()).into())
        })
        .collect()
}

/// Remembers `workspace` in the machine's registry.
pub fn register(data: &Path, workspace: &Path) -> anyhow::Result<()> {
    let workspace = host_path::directory(workspace, true)?;
    let data = host_path::directory(data, true)?;
    let data = data.as_path();
    let _lock = FileLock::hold(&data.join(REGISTRY_LOCK), REGISTRY_TIMEOUT)?;
    let mut locations = registered(data)?;
    if locations.contains(&workspace) {
        return Ok(());
    }
    locations.push(workspace);
    // As `json.dumps` writes a list: one space after each comma.
    let names: Vec<String> = locations
        .iter()
        .map(|p| serde_json::to_string(&p.to_string_lossy()).expect("a string is JSON"))
        .collect();
    files::atomic_write(
        &data.join(REGISTRY_FILE),
        format!("[{}]\n", names.join(", ")).as_bytes(),
    )?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Machine {
        _root: tempfile::TempDir,
        root: PathBuf,
        data: PathBuf,
    }

    fn machine() -> Machine {
        let dir = tempfile::tempdir().unwrap();
        let root = host_path::normalize(dir.path()).unwrap();
        let data = root.join("data");
        Machine {
            _root: dir,
            data,
            root,
        }
    }

    fn vars(
        root: Option<OsString>,
        config_dir: Option<OsString>,
    ) -> impl Fn(&str) -> Option<OsString> {
        move |name| match name {
            WORKSPACE_ROOT_ENV => root.clone(),
            CONFIG_DIR_ENV => config_dir.clone(),
            _ => None,
        }
    }

    fn select(machine: &Machine, workspace: &Path) {
        std::fs::create_dir_all(&machine.data).unwrap();
        let state = serde_json::json!({
            "workspace": workspace,
            "config_dir": workspace.join(".guildbotics/config"),
        });
        std::fs::write(
            machine.data.join(ACTIVE_FILE),
            serde_json::to_string_pretty(&state).unwrap() + "\n",
        )
        .unwrap();
    }

    #[test]
    fn the_explicit_workspace_wins_over_the_selected_one_and_is_registered() {
        let m = machine();
        let selected = m.root.join("selected");
        std::fs::create_dir(&selected).unwrap();
        select(&m, &selected);
        let named = m.root.join("named");

        let opened = restore(&m.data, vars(Some(named.clone().into()), None)).unwrap();
        assert_eq!(opened, Some(named.clone()));
        let config = named.join(".guildbotics/config");
        let opened = restore(&m.data, vars(None, Some(config.into()))).unwrap();
        assert_eq!(opened, Some(named.clone()));
        assert_eq!(registered(&m.data).unwrap(), [named]);
    }

    #[test]
    fn a_config_dir_of_another_shape_names_no_workspace() {
        let m = machine();
        let selected = m.root.join("selected");
        std::fs::create_dir(&selected).unwrap();
        select(&m, &selected);
        let other = m.root.join("elsewhere/config");
        let opened = restore(&m.data, vars(Some("  ".into()), Some(other.into()))).unwrap();
        assert_eq!(opened, Some(selected));
    }

    #[test]
    fn the_selected_workspace_opens_while_it_is_a_directory() {
        let m = machine();
        assert_eq!(restore(&m.data, vars(None, None)).unwrap(), None);
        let selected = m.root.join("selected");
        select(&m, &selected);
        assert_eq!(restore(&m.data, vars(None, None)).unwrap(), None);
        std::fs::create_dir(&selected).unwrap();
        assert_eq!(
            restore(&m.data, vars(None, None)).unwrap(),
            Some(selected.clone())
        );
        assert_eq!(registered(&m.data).unwrap(), [selected]);
        for broken in ["", "[]", "{\"workspace\": 1}", "{\"workspace\": \" \"}"] {
            std::fs::write(m.data.join(ACTIVE_FILE), broken).unwrap();
            assert_eq!(
                restore(&m.data, vars(None, None)).unwrap(),
                None,
                "{broken}"
            );
        }
    }

    #[cfg(unix)]
    #[test]
    fn a_machine_whose_records_are_reached_through_a_link_registers_nothing() {
        let m = machine();
        std::fs::create_dir(m.root.join("elsewhere")).unwrap();
        std::os::unix::fs::symlink(m.root.join("elsewhere"), &m.data).unwrap();
        assert!(register(&m.data, &m.root.join("w")).is_err());
        assert!(!m.root.join("elsewhere/workspaces.json").exists());
    }

    #[cfg(unix)]
    #[test]
    fn a_workspace_reached_through_a_link_is_refused() {
        let m = machine();
        std::fs::create_dir(m.root.join("real")).unwrap();
        std::os::unix::fs::symlink(m.root.join("real"), m.root.join("link")).unwrap();
        select(&m, &m.root.join("link"));
        assert!(restore(&m.data, vars(None, None)).is_err());
        assert!(restore(&m.data, vars(Some(m.root.join("link").into()), None)).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn a_registry_reached_through_a_link_is_not_read() {
        let m = machine();
        std::fs::create_dir_all(&m.data).unwrap();
        let target = m.root.join("elsewhere.json");
        std::fs::write(&target, "[]").unwrap();
        let registry = m.data.join(REGISTRY_FILE);
        std::os::unix::fs::symlink(&target, &registry).unwrap();
        let error = register(&m.data, &m.root.join("workspace")).unwrap_err();
        let problem = error.downcast_ref::<RegistryError>().unwrap();
        assert_eq!(problem.path, registry);
        assert!(problem.reason.contains("passes through a link"));
        assert_eq!(std::fs::read_to_string(target).unwrap(), "[]");
    }

    #[test]
    fn the_registry_keeps_what_the_python_version_wrote_and_adds_once() {
        let m = machine();
        std::fs::create_dir_all(&m.data).unwrap();
        let earlier = m.root.join("éarlier");
        std::fs::write(
            m.data.join(REGISTRY_FILE),
            serde_json::to_string(&[&earlier]).unwrap(),
        )
        .unwrap();
        let added = m.root.join("added");
        register(&m.data, &added).unwrap();
        register(&m.data, &added).unwrap();
        assert_eq!(
            std::fs::read_to_string(m.data.join(REGISTRY_FILE)).unwrap(),
            format!("[\"{}\", \"{}\"]\n", earlier.display(), added.display())
        );
        std::fs::write(m.data.join(REGISTRY_FILE), "{\"a\": 1}").unwrap();
        assert!(register(&m.data, &added).is_err());
    }
}
