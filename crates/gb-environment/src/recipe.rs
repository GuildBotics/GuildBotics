//! Snapshot build scripts and identity, independent of the microVM runtime.

use std::collections::BTreeMap;
use std::path::Path;

use serde::Deserialize;
use sha2::{Digest, Sha256};

#[derive(Debug, Clone, PartialEq, serde::Serialize, Deserialize)]
pub struct Step {
    pub label: String,
    pub script: String,
}

#[derive(Deserialize)]
struct Pins {
    image: String,
    uv_version: String,
    node_version: String,
    node_sha256: BTreeMap<String, String>,
    node_platforms: BTreeMap<String, String>,
    python_version: String,
    venv: String,
    code_root: String,
    pdf_packages: Vec<String>,
}

#[derive(Deserialize)]
struct Provision {
    name: String,
    package: String,
    install: String,
}

/// Everything that changes a snapshot's contents, in execution order.
#[derive(Debug, Clone, PartialEq)]
pub struct Recipe {
    pub image: String,
    pub steps: Vec<Step>,
}

impl Recipe {
    /// Read shipped data, including the generated projection of CLI pins.
    pub fn load(templates: &Path) -> Result<Self, Box<dyn std::error::Error + Send + Sync>> {
        let directory = templates.join("environment");
        let pins: Pins = serde_json::from_slice(&std::fs::read(directory.join("recipe.json"))?)?;
        let provision: Vec<Provision> =
            serde_json::from_slice(&std::fs::read(directory.join("cli_provision.json"))?)?;
        let requirements = std::fs::read_to_string(directory.join("requirements.txt"))?;
        let mut steps = Vec::new();
        let mut step = |label: &str, script: String| {
            steps.push(Step {
                label: label.into(),
                script,
            });
        };
        // The home is in the recipe, so a snapshot names the home it was built with.
        step("home", format!("install -d -m 0700 {}", crate::GUEST_HOME));
        step(
            "uv",
            format!(
                "arch=\"$(uname -m)\"\ncurl -fsSL \"https://github.com/astral-sh/uv/releases/download/{}/uv-${{arch}}-unknown-linux-gnu.tar.gz\" | tar -xz -C /usr/local/bin --strip-components=1 \"uv-${{arch}}-unknown-linux-gnu/uv\" \"uv-${{arch}}-unknown-linux-gnu/uvx\"\nuv --version",
                pins.uv_version,
            ),
        );
        step(
            "pdf",
            format!(
                "apt-get update\napt-get install -y --no-install-recommends {}\nrm -rf /var/lib/apt/lists/*",
                args(&pins.pdf_packages),
            ),
        );
        let venv = &pins.venv;
        let python = &pins.python_version;
        step(
            "python",
            format!(
                "export UV_PYTHON_INSTALL_DIR=/opt/uv/python\nuv venv --no-cache --python {python} {venv}\ncat > {venv}/requirements.txt <<'REQUIREMENTS'\n{}\nREQUIREMENTS\nuv pip install --no-cache --python {venv} -r {venv}/requirements.txt\necho {} > {venv}/lib/python{python}/site-packages/guildbotics.pth\n{venv}/bin/python -c 'import weasyprint'",
                requirements.trim_end(),
                pins.code_root,
            ),
        );
        // Keep the recipe's architecture order, which participates in the digest.
        let mut node = "case \"$(uname -m)\" in\n".to_owned();
        for arch in ["x86_64", "aarch64"] {
            node.push_str(&format!(
                "  {arch}) platform={} sha256={} ;;\n",
                pins.node_platforms[arch], pins.node_sha256[arch]
            ));
        }
        node.push_str("  *) echo \"unsupported architecture: $(uname -m)\" >&2; exit 1 ;;\nesac\n");
        let archive = format!("node-v{}-$platform", pins.node_version);
        node.push_str(&format!(
            "curl -fsSL \"https://nodejs.org/dist/v{}/{archive}.tar.gz\" -o /tmp/node.tar.gz\necho \"$sha256  /tmp/node.tar.gz\" | sha256sum -c -\nrm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack\ntar -xzf /tmp/node.tar.gz -C /usr/local --strip-components=1 --no-same-owner \"{archive}/bin\" \"{archive}/include\" \"{archive}/lib\" \"{archive}/share\"\nrm -f /tmp/node.tar.gz\nnode --version",
            pins.node_version,
        ));
        step("node", node);
        let packages = provision
            .iter()
            .filter(|p| !p.package.is_empty())
            .map(|p| p.package.clone())
            .collect::<Vec<_>>();
        step(
            "npm",
            format!(
                "npm install -g {}\nnpm cache clean --force",
                args(&packages)
            ),
        );
        for p in provision.iter().filter(|p| !p.install.is_empty()) {
            step(&p.name, p.install.clone());
        }
        step("git", "git --version".into());
        Ok(Self {
            image: pins.image,
            steps,
        })
    }

    /// A custom image is identified by the content the runtime actually holds.
    pub fn name(&self, held_image: Option<&str>) -> String {
        let mut recipe = serde_json::json!({
            "image": held_image.unwrap_or(&self.image),
            "steps": self.steps.iter().map(|s| [&s.label, &s.script]).collect::<Vec<_>>(),
        });
        recipe.sort_all_objects();
        let digest = Sha256::digest(serde_json::to_vec(&recipe).expect("JSON"));
        let short = digest[..8]
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>();
        format!("guildbotics-{short}")
    }
}

fn args(values: &[String]) -> String {
    gb_base::shell::join(values)
}
