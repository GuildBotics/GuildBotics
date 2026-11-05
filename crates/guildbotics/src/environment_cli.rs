//! The environment commands share the server's device reading.
use clap::{Subcommand, ValueEnum};
use std::{collections::BTreeMap, path::PathBuf};

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
        archive: PathBuf,
        #[arg(long)]
        tag: Option<String>,
    },
    /// Load an archive into the workspace code host's CI runner
    LoadCi { archive: PathBuf },
    /// List eligible local base images
    List {
        /// Select the output format
        #[arg(long, value_enum, default_value = "markdown")]
        format: OutputFormat,
    },
    /// Declare the image's digest per architecture in the selected workspace
    Declare {
        reference: Option<String>,
        #[arg(long = "default")]
        use_default: bool,
        #[arg(long = "digest")]
        digests: Vec<String>,
    },
}
pub async fn run(command: EnvironmentCommand, workspace: Option<PathBuf>) -> anyhow::Result<()> {
    let data = gb_base::home::data()?;
    let runtime = data.join("msb");
    let language = gb_server::client::language().await;
    let usage = matches!(&command, EnvironmentCommand::Usage { .. });
    match command {
        EnvironmentCommand::Remove => {
            gb_environment::runtime::remove_snapshots(
                &runtime,
                &gb_server::workspace::resolve(&data, workspace, language)?,
            )
            .await
            .map_err(anyhow::Error::msg)?;
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
            println!(
                "The environment {} is ready.",
                built.file_name().unwrap_or_default().to_string_lossy()
            );
        }
        EnvironmentCommand::Status { format } => {
            let home = std::env::var_os("HOME")
                .map(PathBuf::from)
                .ok_or_else(|| anyhow::anyhow!("HOME is not set"))?;
            let mut view = gb_server::environment::status(
                gb_server::workspace::resolve(&data, workspace, language)?,
                data,
                home,
                language,
            )
            .await?;
            if matches!(format, OutputFormat::Markdown) {
                for field in ["problem", "warning"] {
                    if let Some(text) = view[field].as_str().filter(|text| !text.is_empty()) {
                        println!("{text}");
                    }
                }
            }
            if matches!(format, OutputFormat::Json)
                && let Some(view) = view.as_object_mut()
            {
                for (source, target) in [("problem", "refusal"), ("problem_setting", "setting")] {
                    if let Some(value) = view.remove(source) {
                        view.insert(target.into(), value);
                    }
                }
            }
            println!("{}", serde_json::to_string_pretty(&view)?);
        }
        EnvironmentCommand::Image { command } => match command {
            ImageCommand::LoadCi { archive } => {
                let loaded = gb_server::environment::load_ci(
                    &gb_server::workspace::resolve(&data, workspace, language)?,
                    &archive,
                )
                .await?;
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
                let mut list = gb_server::environment::list_images(&data, Some(&workspace)).await?;
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
                anyhow::ensure!(
                    use_default != reference.is_some(),
                    "Give REFERENCE or --default"
                );
                let mut declared = BTreeMap::new();
                for value in digests {
                    let (arch, digest) = value
                        .split_once('=')
                        .ok_or_else(|| anyhow::anyhow!("Expected ARCH=DIGEST"))?;
                    declared.insert(arch.into(), digest.into());
                }
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
                            .map_err(|e| anyhow::anyhow!("{e:?}"))?
                            .ok_or_else(|| {
                                anyhow::anyhow!("The image is not loaded on this device")
                            })?;
                    declared.insert(gb_environment::image::device_architecture().into(), digest);
                }
                gb_server::environment::declare(
                    &gb_server::workspace::resolve(&data, workspace, language)?,
                    reference,
                    declared,
                )?;
            }
        },
    }
    Ok(())
}
