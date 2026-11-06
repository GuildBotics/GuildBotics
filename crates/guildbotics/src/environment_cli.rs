//! The environment commands share the server's device reading.
use clap::{Subcommand, ValueEnum};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

#[derive(Subcommand)]
pub(crate) enum EnvironmentCommand {
    /// Read the device's environment and refusal or warning
    Status {
        /// Select the output format
        #[arg(long, value_enum, default_value = "markdown")]
        format: OutputFormat,
    },
    /// Build the declared environment on this device
    Build {
        /// Rebuild even when the snapshot already matches the declaration
        #[arg(long)]
        force: bool,
    },
    /// Remove device snapshots while preserving provider logins
    Remove,
    /// Log in to a provider in its private environment
    Login { tool: String },
    /// Probe provider usage in its private environment
    Usage { tool: String },
    /// Manage local and CI base images
    Image {
        #[command(subcommand)]
        command: ImageCommand,
    },
}
#[derive(Clone, Copy, ValueEnum)]
pub(crate) enum OutputFormat {
    Markdown,
    Json,
}

#[derive(Subcommand)]
pub(crate) enum ImageCommand {
    /// Load a Docker save or OCI archive built for this device's CPU
    Load {
        #[arg(value_parser = archive_file)]
        archive: PathBuf,
        /// Add a reference to the archive's own tags
        #[arg(long)]
        tag: Option<String>,
    },
    /// Load an archive into the workspace code host's CI runner
    LoadCi {
        #[arg(value_parser = archive_file)]
        archive: PathBuf,
    },
    /// List eligible local base images
    List {
        /// Select the output format
        #[arg(long, value_enum, default_value = "markdown")]
        format: OutputFormat,
    },
    /// Declare the image's digest per architecture in the selected workspace
    Declare {
        #[arg(required_unless_present = "use_default")]
        reference: Option<String>,
        /// Restore the GuildBotics default image
        #[arg(long = "default", conflicts_with_all = ["reference", "digests"])]
        use_default: bool,
        /// Set the image digest for an architecture. Repeatable
        #[arg(long = "digest", value_name = "ARCH=DIGEST", value_parser = architecture_digest)]
        digests: Vec<(String, String)>,
    },
}
pub async fn run(command: EnvironmentCommand, workspace: Option<PathBuf>) -> anyhow::Result<()> {
    let data = gb_base::home::data()?;
    let runtime = data.join("msb");
    let language = gb_server::client::language().await;
    let usage = matches!(&command, EnvironmentCommand::Usage { .. });
    match command {
        EnvironmentCommand::Remove => {
            let removed = gb_environment::runtime::remove_snapshots(
                &runtime,
                &gb_server::workspace::resolve(&data, workspace, language)?,
            )
            .await
            .map_err(|error| anyhow::anyhow!(gb_server::i18n::say(error.sentence(), language)))?;
            if removed.is_empty() {
                println!("This device holds no snapshot of the workspace.");
            }
            for name in removed {
                println!("removed {name}");
            }
        }
        EnvironmentCommand::Login { tool } | EnvironmentCommand::Usage { tool } => {
            let workspace = gb_server::workspace::resolve(&data, workspace, language)?;
            let home = std::env::var_os("HOME")
                .map(PathBuf::from)
                .ok_or_else(|| anyhow::anyhow!("HOME is not set"))?;
            let host = gb_environment::Host {
                data,
                home,
                package: gb_base::home::python_package(),
                identity_root: gb_base::home::identity_root()?,
            };
            let result = gb_server::tool_login::login(&host, &workspace, &tool, usage)
                .await
                .map_err(anyhow::Error::msg)?;
            println!("{}", serde_json::to_string_pretty(&result)?);
        }
        EnvironmentCommand::Build { force } => {
            let workspace = gb_server::workspace::resolve(&data, workspace, language)?;
            let home = std::env::var_os("HOME")
                .map(PathBuf::from)
                .ok_or_else(|| anyhow::anyhow!("HOME is not set"))?;
            let built =
                gb_server::environment::build(&workspace, &data, &home, force, language, |line| {
                    println!("{line}")
                })
                .await?;
            use gb_server::environment::BuildOutcome;
            let (path, state) = match built {
                BuildOutcome::UpToDate(path) => (path, "already up to date"),
                BuildOutcome::Built(path) => (path, "ready"),
            };
            println!(
                "The environment {} is {state}.",
                path.file_name().unwrap_or_default().to_string_lossy()
            );
        }
        EnvironmentCommand::Status { format } => {
            let home = std::env::var_os("HOME")
                .map(PathBuf::from)
                .ok_or_else(|| anyhow::anyhow!("HOME is not set"))?;
            let view = gb_server::environment::status(
                gb_server::workspace::resolve(&data, workspace, language)?,
                data,
                home,
                language,
            )
            .await?;
            match format {
                OutputFormat::Json => {
                    println!("{}", serde_json::to_string_pretty(&status_payload(&view))?)
                }
                OutputFormat::Markdown => print_status(&view, language),
            }
        }
        EnvironmentCommand::Image { command } => match command {
            ImageCommand::LoadCi { archive } => {
                let loaded = gb_server::environment::load_ci(
                    &gb_server::workspace::resolve_with_message(
                        &data,
                        workspace,
                        language,
                        "intelligences.agent_environment.image.workspace_required",
                    )?,
                    &archive,
                    |path| {
                        println!(
                            "{}",
                            gb_server::i18n::t(
                                language,
                                "intelligences.agent_environment.image.loading_ci",
                                &[("archive", &path.to_string_lossy())],
                            )
                        );
                    },
                )
                .await?;
                let Some(loaded) = loaded else {
                    println!(
                        "{}",
                        gb_server::i18n::t(
                            language,
                            "intelligences.agent_environment.image.no_ci_runner",
                            &[],
                        )
                    );
                    return Ok(());
                };
                for reference in loaded {
                    println!("loaded {reference}");
                }
            }
            ImageCommand::Load { archive, tag } => {
                for (reference, digest) in gb_environment::load_image(&runtime, &archive, tag)
                    .await
                    .map_err(|sentence| anyhow::anyhow!(gb_server::i18n::say(sentence, language)))?
                {
                    println!("loaded {reference} {digest}");
                }
            }
            ImageCommand::List { format } => {
                let workspace = gb_server::workspace::resolve(&data, workspace, language)?;
                let mut list =
                    gb_server::environment::list_images(&data, Some(&workspace), language).await?;
                if matches!(format, OutputFormat::Json) {
                    list.as_object_mut().expect("image list").remove("problem");
                    println!("{}", serde_json::to_string_pretty(&list)?);
                } else {
                    println!(
                        "architecture: {}",
                        list["architecture"].as_str().unwrap_or_default()
                    );
                    let images = list["images"].as_array().expect("image rows");
                    if images.is_empty() {
                        println!(
                            "{}",
                            gb_server::i18n::t(
                                language,
                                "intelligences.agent_environment.image.empty",
                                &[]
                            )
                        );
                    }
                    for image in images {
                        let reference = image["reference"].as_str().unwrap_or_default();
                        let digest = image["digest"].as_str().unwrap_or_default();
                        let declared = if image["declared"] == true {
                            gb_server::i18n::t(
                                language,
                                "intelligences.agent_environment.image.declared",
                                &[],
                            )
                        } else {
                            String::new()
                        };
                        println!("{}", format!("{reference} {digest} {declared}").trim_end());
                    }
                }
            }
            ImageCommand::Declare {
                reference,
                use_default,
                digests,
            } => {
                let workspace = gb_server::workspace::resolve(&data, workspace, language)?;
                let mut declared = digests
                    .into_iter()
                    .collect::<std::collections::BTreeMap<_, _>>();
                if let Some(reference) = reference.as_deref()
                    && declared.is_empty()
                {
                    let recipe = gb_environment::recipe::Recipe::load(
                        &gb_base::home::python_package().join("templates"),
                    )
                    .map_err(|e| anyhow::anyhow!("{e}"))?;
                    let digest =
                        gb_environment::runtime::held_image(&runtime, reference, &recipe.image)
                            .await
                            .map_err(|e| {
                                anyhow::anyhow!(gb_server::i18n::say(e.sentence(), language))
                            })?
                            .ok_or_else(|| {
                                anyhow::anyhow!(gb_server::i18n::t(
                                    language,
                                    "intelligences.agent_environment.image.missing",
                                    &[
                                        ("reference", reference),
                                        (
                                            "architecture",
                                            gb_environment::image::device_architecture()
                                        ),
                                        (
                                            "command",
                                            "guildbotics environment image load <archive.tar>"
                                        )
                                    ]
                                ))
                            })?;
                    declared.insert(gb_environment::image::device_architecture().into(), digest);
                }
                let image = gb_server::environment::declare(&workspace, reference, declared)?;
                if let Some(image) = image {
                    for (architecture, digest) in image.digests {
                        println!("{} {architecture} {digest}", image.reference);
                    }
                } else {
                    debug_assert!(use_default);
                    let recipe = gb_environment::recipe::Recipe::load(
                        &gb_base::home::python_package().join("templates"),
                    )
                    .map_err(|error| anyhow::anyhow!("{error}"))?;
                    println!("image: {} (GuildBotics default)", recipe.image);
                }
            }
        },
    }
    Ok(())
}

fn archive_file(value: &str) -> Result<PathBuf, String> {
    let path = Path::new(value);
    if !path.is_file() {
        return Err(format!("The archive {value} is not a regular file"));
    }
    Ok(path.into())
}

fn architecture_digest(value: &str) -> Result<(String, String), String> {
    value
        .split_once('=')
        .map(|(architecture, digest)| (architecture.into(), digest.into()))
        .ok_or_else(|| "Expected ARCH=DIGEST".into())
}

fn fields(value: &Value, names: &[&str]) -> Value {
    Value::Object(
        names
            .iter()
            .map(|name| ((*name).into(), value[*name].clone()))
            .collect(),
    )
}

fn status_payload(view: &Value) -> Value {
    json!({
        "refusal": view["problem"],
        "setting": view["problem_setting"],
        "warning": view["warning"],
        "runtime": view["runtime"],
        "resources": view["resources"],
        "snapshot": fields(&view["snapshot"], &["state", "name", "path", "detail"]),
        "image": fields(&view["image"], &["reference", "architecture", "digest", "digests", "present", "held", "problem", "warning"]),
        "network": view["network"],
        "dns": view["dns"],
        "tools": view["tools"].as_array().into_iter().flatten().map(|tool|
            fields(tool, &["name", "label", "provisioned", "credentials_saved", "authentication_failed", "problem"])
        ).collect::<Vec<_>>(),
    })
}

fn text(value: &Value) -> &str {
    value.as_str().unwrap_or_default()
}

fn strings(value: &Value) -> impl Iterator<Item = &str> {
    value
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
}

fn short_digest(digest: &str) -> &str {
    digest.get(..19).unwrap_or(digest)
}

fn print_status(view: &Value, language: &str) {
    let refusal = text(&view["problem"]);
    if !refusal.is_empty() {
        println!("{}: {refusal}", text(&view["problem_setting"]));
    }
    let warning = text(&view["warning"]);
    if !warning.is_empty() {
        println!("warning: {warning}");
    }
    let runtime = &view["runtime"];
    if runtime["available"] == true {
        println!("runtime: available {}", text(&runtime["version"]));
    } else {
        println!("runtime: unavailable: {}", text(&runtime["reason"]));
    }
    let home = text(&runtime["home"]);
    if !home.is_empty() {
        println!("runtime home: {home}");
    }
    let resources = &view["resources"];
    if resources.is_null() {
        println!("resources: unavailable");
    } else {
        println!(
            "resources: {} MiB, {} vCPU",
            resources["memory_mib"], resources["cpus"]
        );
    }
    let image = &view["image"];
    let reference = text(&image["reference"]);
    if reference.is_empty() {
        println!("image: {} (GuildBotics default)", text(&image["default"]));
    } else {
        let architecture = text(&image["architecture"]);
        let held = short_digest(text(&image["held"]));
        let description = if held.is_empty() {
            "not loaded".to_owned()
        } else if image["present"] == true {
            "loaded, declared".to_owned()
        } else if text(&image["digest"]).is_empty() {
            format!("loaded at {held}, not declared for {architecture}")
        } else {
            format!("loaded at {held}, not the declared one")
        };
        let digests = image["digests"]
            .as_object()
            .into_iter()
            .flatten()
            .map(|(arch, digest)| format!("{arch}={}", short_digest(text(digest))))
            .collect::<Vec<_>>()
            .join(" ");
        println!("image: {reference} [{digests}] {architecture}: {description}");
    }
    let snapshot = &view["snapshot"];
    let state = text(&snapshot["state"]);
    let mut detail = text(&snapshot["detail"]);
    if detail.is_empty() && matches!(state, "missing" | "stale") {
        detail = "run `guildbotics environment build`";
    }
    let name = text(&snapshot["name"]);
    if detail.is_empty() {
        println!("snapshot: {state} {name}");
    } else {
        println!("snapshot: {state} {name} ({detail})");
    }
    println!("location: {}", text(&snapshot["path"]));

    let network = &view["network"];
    let mode = text(&network["mode"]);
    let mut domains = strings(&network["allowed_domains"]).collect::<Vec<_>>();
    if network["allow_local_network"] == true {
        domains.push("localhost and LAN");
    }
    if network.is_null() {
        println!("network: unavailable");
    } else if domains.is_empty() {
        println!("network: {mode}");
    } else {
        println!("network: {mode} ({})", domains.join(", "));
    }
    let dns = &view["dns"];
    let nameservers = strings(&dns["nameservers"]).collect::<Vec<_>>().join(", ");
    let resolved = if nameservers.is_empty() {
        text(&dns["problem"])
    } else {
        &nameservers
    };
    println!("dns: {} -> {resolved}", text(&dns["declared"]));
    for tool in view["tools"].as_array().into_iter().flatten() {
        let problem = text(&tool["problem"]);
        let reason = if problem.is_empty() {
            gb_server::i18n::t(
                language,
                "intelligences.agent_environment.tool.credentials_saved",
                &[("tool", text(&tool["label"]))],
            )
        } else {
            problem.to_owned()
        };
        println!("{}: {reason}", text(&tool["name"]));
    }
}
