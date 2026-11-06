use std::{
    io::{self, IsTerminal, Write},
    path::PathBuf,
};

use clap::{Args, Subcommand};
use gb_server::client::secrets;

#[derive(Args)]
pub struct Secrets {
    /// Use this workspace instead of the active workspace
    #[arg(long, global = true, conflicts_with = "identities")]
    workspace: Option<PathBuf>,
    /// Manage the identity store shared by all workspaces
    #[arg(long, global = true)]
    identities: bool,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Show the OS secret-store status and number of keys
    Status,
    /// List the names of stored secrets
    List,
    /// Store a secret value from a file or prompt
    Set {
        key: String,
        value: Option<String>,
        /// Read the secret value from a UTF-8 file
        #[arg(long, conflicts_with = "value")]
        from_file: Option<PathBuf>,
    },
    /// Delete a stored secret
    Delete { key: String },
    /// Export secrets in dotenv format
    Export {
        /// Write to an owner-only file instead of standard output
        #[arg(long)]
        file: Option<PathBuf>,
    },
    /// Import secrets from a dotenv file
    Import { file: PathBuf },
}

pub async fn run(args: Secrets) -> anyhow::Result<()> {
    let command = match args.command {
        Command::Status => secrets::Command::Status,
        Command::List => secrets::Command::List,
        Command::Set {
            key,
            value,
            from_file,
        } => secrets::Command::Set {
            key,
            value,
            from_file,
        },
        Command::Delete { key } => secrets::Command::Delete { key },
        Command::Export { file } => secrets::Command::Export { file },
        Command::Import { file } => secrets::Command::Import { file },
    };
    secrets::run(command, args.workspace, args.identities, prompt).await
}

fn prompt(label: &str) -> anyhow::Result<String> {
    use crossterm::{
        event::{self, Event, KeyCode, KeyEventKind, KeyModifiers},
        terminal,
    };
    let mut stderr = io::stderr().lock();
    write!(stderr, "{label} ")?;
    stderr.flush()?;
    if !io::stdin().is_terminal() {
        let mut value = String::new();
        io::stdin().read_line(&mut value)?;
        return Ok(value.trim_end_matches(['\n', '\r']).to_owned());
    }
    terminal::enable_raw_mode()?;
    let input = (|| -> io::Result<String> {
        let mut value = String::new();
        loop {
            if let Event::Key(key) = event::read()?
                && key.kind != KeyEventKind::Release
            {
                match key.code {
                    KeyCode::Enter => return Ok(value),
                    KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                        return Err(io::Error::from(io::ErrorKind::Interrupted));
                    }
                    KeyCode::Char(character) => value.push(character),
                    KeyCode::Backspace => {
                        value.pop();
                    }
                    _ => (),
                }
            }
        }
    })();
    let restored = terminal::disable_raw_mode();
    writeln!(stderr)?;
    restored?;
    Ok(input?)
}

#[cfg(test)]
mod tests {
    #[test]
    fn secrets_status_is_registered_on_the_main_cli() {
        use clap::Parser as _;
        let parsed = crate::Cli::try_parse_from(["guildbotics", "secrets", "status"]).unwrap();
        assert!(matches!(
            parsed.command,
            crate::Command::Secrets(super::Secrets {
                command: super::Command::Status,
                ..
            })
        ));
    }
}
