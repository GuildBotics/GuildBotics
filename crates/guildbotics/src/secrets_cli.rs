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
        return read_noninteractive(io::stdin().lock());
    }
    terminal::enable_raw_mode()?;
    let input = (|| -> io::Result<String> {
        let mut value = String::new();
        loop {
            if let Event::Key(key) = event::read()?
                && key.kind != KeyEventKind::Release
            {
                match key.code {
                    KeyCode::Enter if !value.is_empty() => return Ok(value),
                    KeyCode::Enter => {
                        write!(stderr, "\r\n{label} ")?;
                        stderr.flush()?;
                    }
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

fn read_noninteractive(mut input: impl io::BufRead) -> anyhow::Result<String> {
    let mut line = String::new();
    while input.read_line(&mut line)? != 0 {
        let value = line.trim_end_matches(['\n', '\r']);
        if !value.is_empty() {
            return Ok(value.to_owned());
        }
        line.clear();
    }
    Err(io::Error::from(io::ErrorKind::UnexpectedEof).into())
}

#[cfg(test)]
mod tests {
    #[test]
    fn secret_prompt_requires_a_value_before_eof() {
        assert!(super::read_noninteractive(&b""[..]).is_err());
        assert!(super::read_noninteractive(&b"\r\n\n"[..]).is_err());
        assert_eq!(
            super::read_noninteractive(&b"\n fixture \r\n"[..]).unwrap(),
            " fixture "
        );
    }

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
