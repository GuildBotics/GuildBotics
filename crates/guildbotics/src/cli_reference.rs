use std::{collections::BTreeSet, fmt::Write};

use clap::{Command, CommandFactory};
use serde_json::Value;

use crate::Cli;

const REFERENCE: &str = "docs/cli_reference.md";
const CATALOG: &str = include_str!("../../../guildbotics/templates/member/catalog.json");

pub fn run(check: bool) -> anyhow::Result<()> {
    verify_catalog()?;
    let reference = render();
    if check {
        anyhow::ensure!(
            std::fs::read_to_string(REFERENCE)? == reference,
            "Regenerate {REFERENCE} with `cargo run --locked -p guildbotics -- generate-cli-reference`."
        );
    } else {
        std::fs::write(REFERENCE, reference)?;
    }
    Ok(())
}

fn verify_catalog() -> anyhow::Result<()> {
    let grammar = gb_server::member::grammar();
    let commands = grammar
        .keys()
        .filter_map(|name| name.split_whitespace().next())
        .collect::<BTreeSet<_>>();
    let mut options = grammar
        .values()
        .flat_map(|entry| {
            entry
                .options
                .iter()
                .flat_map(|option| option.spelling.split('/'))
        })
        .collect::<BTreeSet<_>>();
    let cli = Cli::command();
    let run = cli.find_subcommand("run").expect("run CLI");
    // Agent instructions also use the run command's working directory option.
    if run.get_arguments().any(|arg| arg.get_long() == Some("cwd")) {
        options.insert("--cwd");
    }
    options.extend(["--help", "--workspace"]);
    let catalog: Value = serde_json::from_str(CATALOG)?;
    for (field, actual) in [("member_commands", commands), ("options", options)] {
        let expected: BTreeSet<String> = serde_json::from_value(catalog[field].clone())?;
        let expected = expected.iter().map(String::as_str).collect::<BTreeSet<_>>();
        anyhow::ensure!(
            expected == actual,
            "member/catalog.json {field} differs from the Rust CLI. Missing: {:?}; obsolete: {:?}",
            actual.difference(&expected).collect::<Vec<_>>(),
            expected.difference(&actual).collect::<Vec<_>>(),
        );
    }
    Ok(())
}

fn render() -> String {
    let mut output = String::from(
        "# GuildBotics CLI Reference\n\n\
        <!-- AUTO-GENERATED FILE. DO NOT EDIT BY HAND. -->\n\n\
        Regenerate this reference from the Rust CLI.\n\n\
        ```sh\ncargo run --locked -p guildbotics -- generate-cli-reference\n```\n\n\
        For workspace setup and examples, see the [README](../README.md).\n\n\
        ## Commands\n\n",
    );
    let mut sections = String::new();
    collect(Cli::command(), "guildbotics", &mut output, &mut sections);
    output.push_str(&sections);
    output
}

fn collect(command: Command, path: &str, index: &mut String, sections: &mut String) {
    writeln!(index, "- [`{path}`](#{})", path.replace(' ', "-")).unwrap();
    writeln!(sections, "\n## {path}\n").unwrap();
    let mut command = if path == "guildbotics branvil" {
        branvil::cli_command()
    } else {
        command
    };
    let env_args = command
        .get_arguments()
        .filter(|arg| arg.get_env().is_some())
        .map(|arg| arg.get_id().clone())
        .collect::<Vec<_>>();
    for id in env_args {
        command = command.mut_arg(id, |arg| arg.hide_env_values(true));
    }
    let help = command
        .clone()
        .bin_name(path)
        .term_width(0)
        .render_long_help();
    sections.push_str("```text\n");
    for line in help.to_string().lines() {
        writeln!(sections, "{}", line.trim_end()).unwrap();
    }
    sections.push_str("```\n");
    if path == "guildbotics member" {
        sections.push_str(
            "\nStart the GuildBotics server before running member commands. Use `member --workspace <path>` to verify the selected workspace.\n\
            See the [member capability reference](../guildbotics/templates/member/reference.md) for examples and option semantics.\n",
        );
        for name in gb_server::member::grammar().keys() {
            let full = format!("{path} {name}");
            writeln!(index, "- [`{full}`](#{})", full.replace(' ', "-")).unwrap();
            writeln!(
                sections,
                "\n### {full}\n\n```text\n{}```",
                gb_server::member::usage(name)
            )
            .unwrap();
        }
    }
    for child in command
        .get_subcommands()
        .filter(|child| !child.is_hide_set())
    {
        collect(
            child.clone(),
            &format!("{path} {}", child.get_name()),
            index,
            sections,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generated_reference_and_member_catalog_match_the_cli() {
        verify_catalog().unwrap();
        assert_eq!(
            render(),
            include_str!("../../../docs/cli_reference.md"),
            "run guildbotics generate-cli-reference"
        );
    }

    #[test]
    fn help_shows_defaults_required_and_repeatable() {
        let recall = gb_server::member::usage("memory recall");
        for expected in [
            "[default: 20; 1<=x<=200]",
            "[default: json]",
            "[required]",
            "(repeatable)",
        ] {
            assert!(recall.contains(expected), "missing {expected} in {recall}");
        }
        let help = Cli::command()
            .find_subcommand("diagnostics")
            .unwrap()
            .find_subcommand("system")
            .unwrap()
            .clone()
            .render_long_help()
            .to_string();
        assert!(help.contains("--limit <LIMIT>") && help.contains("[default: 50]"));
    }
}
