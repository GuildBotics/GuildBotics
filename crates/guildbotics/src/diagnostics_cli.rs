use std::path::PathBuf;

use clap::{Args, Subcommand, ValueEnum};
use gb_server::client::diagnostics::{self, TraceSearch};
use serde_json::Value;

use crate::environment_cli::OutputFormat;

#[derive(Args)]
pub struct DiagnosticsCommand {
    /// Read a different workspace
    #[arg(long, global = true)]
    workspace: Option<PathBuf>,
    /// Select the output format
    #[arg(long, global = true, value_enum, default_value = "json")]
    format: OutputFormat,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// List recorded executions, newest first
    Traces {
        /// Only traces from this execution source
        #[arg(long)]
        source: Option<String>,
        /// Only traces recorded for this member
        #[arg(long)]
        person: Option<String>,
        /// Search execution summaries and transcripts
        #[arg(long)]
        query: Option<String>,
        /// Only traces carrying this attribute
        #[arg(long)]
        attr_key: Option<String>,
        /// Match this value of --attr-key
        #[arg(long, requires = "attr_key")]
        attr_value: Option<String>,
        /// Include the screens' AI assistant sessions
        #[arg(long, conflicts_with = "no_include_assistant")]
        include_assistant: bool,
        /// Hide the screens' AI assistant sessions
        #[arg(long)]
        no_include_assistant: bool,
        /// Maximum traces
        #[arg(long, default_value_t = 50, value_parser = clap::value_parser!(u32).range(1..=1000))]
        limit: u32,
    },
    /// Show an execution's summary and records
    Trace {
        trace_id: String,
        /// Filter by record kind. Repeat to combine kinds
        #[arg(long, value_enum)]
        kind: Vec<Kind>,
        /// Filter records by log level
        #[arg(long)]
        level: Option<String>,
        /// Maximum records, keeping the newest
        #[arg(long, default_value_t = 200, value_parser = clap::value_parser!(u32).range(1..=5000))]
        limit: u32,
    },
    /// Show service-wide records outside an execution
    System {
        /// Maximum records, keeping the newest
        #[arg(long, default_value_t = 50, value_parser = clap::value_parser!(u32).range(1..=1000))]
        limit: u32,
    },
}

#[derive(Clone, Copy, ValueEnum)]
enum Kind {
    Event,
    Log,
    Io,
    Memory,
}

pub async fn run(command: DiagnosticsCommand) -> anyhow::Result<()> {
    let language = gb_server::client::language().await;
    let workspace =
        gb_server::workspace::read_selection(&gb_base::home::data()?, command.workspace, language)?;
    let result = match command.command {
        Command::Traces {
            source,
            person,
            query,
            attr_key,
            attr_value,
            include_assistant,
            limit,
            ..
        } => diagnostics::traces(
            &workspace,
            TraceSearch {
                source: source.as_deref(),
                person: person.as_deref(),
                query: query.as_deref(),
                attr_key: attr_key.as_deref(),
                attr_value: attr_value.as_deref(),
                include_assistant,
                limit: limit as usize,
                include_system: true,
                search_transcripts: true,
            },
        )?,
        Command::Trace {
            trace_id,
            kind,
            level,
            limit,
        } => {
            let kinds = kind
                .iter()
                .filter_map(ValueEnum::to_possible_value)
                .collect::<Vec<_>>();
            let names = kinds.iter().map(|kind| kind.get_name()).collect::<Vec<_>>();
            diagnostics::trace(
                &workspace,
                &trace_id,
                &names,
                level.as_deref(),
                limit as usize,
            )?
        }
        Command::System { limit } => diagnostics::system(&workspace, limit as usize)?,
    };
    match command.format {
        OutputFormat::Json => println!("{}", serde_json::to_string(&result)?),
        OutputFormat::Markdown => {
            for (key, value) in result.as_object().expect("diagnostics object") {
                if let Value::Array(values) = value {
                    println!("## {key} ({})", values.len());
                    for value in values {
                        println!("{}", serde_json::to_string(value)?);
                    }
                } else {
                    println!("## {key}\n{}", serde_json::to_string(value)?);
                }
            }
        }
    }
    Ok(())
}
