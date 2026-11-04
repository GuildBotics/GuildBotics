//! The environment commands share the server's device reading.
use clap::Subcommand;
use std::{collections::BTreeMap, path::PathBuf};

#[derive(Subcommand)]
pub enum EnvironmentCommand {
    /// Read the device's environment and refusal or warning
    Status,
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
#[derive(Subcommand)]
pub enum ImageCommand {
    /// Load a Docker save or OCI archive built for this device's CPU
    Load {
        archive: PathBuf,
        #[arg(long)]
        tag: Option<String>,
    },
    /// Load an archive into the workspace code host's CI runner
    LoadCi { archive: PathBuf },
    /// List eligible local base images
    List,
    /// Declare the image's digest per architecture in the selected workspace
    Declare {
        reference: Option<String>,
        #[arg(long = "default")]
        use_default: bool,
        #[arg(long = "digest")]
        digests: Vec<String>,
    },
}
pub fn run(command: EnvironmentCommand, workspace: Option<PathBuf>) -> anyhow::Result<()> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?
        .block_on(execute(command, workspace))
}
async fn execute(command: EnvironmentCommand, workspace: Option<PathBuf>) -> anyhow::Result<()> {
    let data = gb_base::home::data()?;
    let runtime = data.join("msb");
    let usage = matches!(&command, EnvironmentCommand::Usage { .. });
    match command {
        EnvironmentCommand::Remove => {
            gb_environment::runtime::remove_snapshots(
                &runtime,
                &gb_server::environment::workspace(workspace)?,
            )
            .await
            .map_err(anyhow::Error::msg)?;
        }
        EnvironmentCommand::Login { tool } | EnvironmentCommand::Usage { tool } => {
            let home = std::env::var_os("HOME")
                .map(PathBuf::from)
                .ok_or_else(|| anyhow::anyhow!("HOME is not set"))?;
            let host = gb_environment::Host {
                data,
                home,
                package: gb_base::home::python_package(),
                identity_root: gb_base::home::identity_root()?,
            };
            let result = gb_server::tool_login::login(
                &host,
                &gb_server::environment::workspace(workspace)?,
                &tool,
                usage,
            )
            .await
            .map_err(anyhow::Error::msg)?;
            println!("{}", serde_json::to_string_pretty(&result)?);
        }
        EnvironmentCommand::Build { force } => {
            let workspace = gb_server::environment::workspace(workspace)?;
            let home = std::env::var_os("HOME")
                .map(PathBuf::from)
                .ok_or_else(|| anyhow::anyhow!("HOME is not set"))?;
            let language = gb_environment::language::ui_language()
                .await
                .unwrap_or_else(|| "en".into());
            let built =
                gb_server::environment::build(&workspace, &data, &home, force, &language, |line| {
                    println!("{line}")
                })
                .await?;
            println!(
                "The environment {} is ready.",
                built.file_name().unwrap_or_default().to_string_lossy()
            );
        }
        EnvironmentCommand::Status => {
            let home = std::env::var_os("HOME")
                .map(PathBuf::from)
                .ok_or_else(|| anyhow::anyhow!("HOME is not set"))?;
            let language = gb_environment::language::ui_language()
                .await
                .unwrap_or_else(|| "en".into());
            let view = gb_server::environment::status(
                gb_server::environment::workspace(workspace)?,
                data,
                home,
                &language,
            )
            .await?;
            for field in ["problem", "warning"] {
                if let Some(text) = view[field].as_str().filter(|text| !text.is_empty()) {
                    println!("{text}");
                }
            }
            println!("{}", serde_json::to_string_pretty(&view)?);
        }
        EnvironmentCommand::Image { command } => match command {
            ImageCommand::LoadCi { archive } => {
                let loaded = gb_server::environment::load_ci(
                    &gb_server::environment::workspace(workspace)?,
                    &archive,
                )
                .await?;
                for reference in loaded {
                    println!("loaded {reference}");
                }
            }
            ImageCommand::Load { archive, tag } => {
                gb_environment::image_archive::validate(
                    &archive,
                    gb_environment::image::device_architecture(),
                )
                .map_err(|e| anyhow::anyhow!("{e}"))?;
                gb_environment::runtime::ensure(&runtime).map_err(|e| anyhow::anyhow!("{e:?}"))?;
                for (reference, digest) in gb_environment::load_image(&runtime, &archive, tag)
                    .await
                    .map_err(anyhow::Error::msg)?
                {
                    println!("loaded {reference} {digest}");
                }
            }
            ImageCommand::List => {
                let recipe = gb_environment::recipe::Recipe::load(
                    &gb_base::home::python_package().join("templates"),
                )
                .map_err(|e| anyhow::anyhow!("{e}"))?;
                for (reference, digest, _) in
                    gb_environment::runtime::images(&runtime, &recipe.image)
                        .await
                        .map_err(|e| anyhow::anyhow!("{e:?}"))?
                {
                    println!("{reference} {digest}");
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
                    &gb_server::environment::workspace(workspace)?,
                    reference,
                    declared,
                )?;
            }
        },
    }
    Ok(())
}
