use std::path::PathBuf;

use clap::{Args, Subcommand};
use gb_server::{client, i18n};
use serde_json::json;

use crate::environment_cli::OutputFormat;

#[derive(Args)]
pub struct Format {
    /// Select the output format
    #[arg(long, value_enum, default_value = "markdown")]
    format: OutputFormat,
}

#[derive(Subcommand)]
pub enum WorkspaceCommand {
    /// Persist the active workspace for the server and AI CLI tools
    Use {
        workspace_dir: PathBuf,
        #[command(flatten)]
        output: Format,
    },
    /// Show the persisted active workspace
    Current(Format),
    /// Show the active workspace status, even when no workspace is configured
    Status(Format),
}

pub async fn run(command: WorkspaceCommand) -> anyhow::Result<()> {
    let language = client::language().await;
    let data = gb_base::home::data()?;
    let (workspace, output) = match command {
        WorkspaceCommand::Use {
            workspace_dir,
            output,
        } => (
            Some(gb_server::workspace::select(
                &data,
                &workspace_dir,
                language,
            )?),
            output,
        ),
        WorkspaceCommand::Current(output) => {
            let workspace = gb_server::workspace::active(&data)?.ok_or_else(|| {
                anyhow::anyhow!(i18n::t(language, "cli.workspace.not_configured", &[]))
            })?;
            (Some(workspace), output)
        }
        WorkspaceCommand::Status(output) => (gb_server::workspace::active(&data)?, output),
    };
    let state_file = gb_server::workspace::state_file(&data);
    let config_dir = workspace
        .as_ref()
        .map(|workspace| workspace.join(".guildbotics/config"));
    let config_exists = config_dir.as_ref().is_some_and(|dir| dir.is_dir());
    match (output.format, workspace, config_dir) {
        (OutputFormat::Json, Some(workspace), Some(config_dir)) => println!(
            "{}",
            json!({
                "configured": true,
                "state_file": state_file,
                "workspace": workspace,
                "workspace_exists": true,
                "config_dir": config_dir,
                "config_dir_exists": config_exists,
            })
        ),
        (OutputFormat::Json, _, _) => println!(
            "{}",
            json!({
                "configured": false,
                "state_file": state_file,
            })
        ),
        (OutputFormat::Markdown, Some(workspace), Some(config_dir)) => println!(
            "{}",
            i18n::t(
                language,
                "cli.workspace.selected",
                &[
                    ("workspace", &workspace.to_string_lossy()),
                    ("config_dir", &config_dir.to_string_lossy()),
                    ("exists", &config_exists.to_string()),
                    ("state_file", &state_file.to_string_lossy()),
                ],
            )
        ),
        (OutputFormat::Markdown, _, _) => println!(
            "{}",
            i18n::t(
                language,
                "cli.workspace.missing",
                &[("state_file", &state_file.to_string_lossy())],
            )
        ),
    }
    Ok(())
}
