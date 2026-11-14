#[path = "src/build_identity.rs"]
mod build_identity;
use std::{path::Path, process::Command};

fn main() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let git = |args: &[&str]| {
        Command::new("git")
            .arg("-C")
            .arg(&root)
            .args(args)
            .output()
            .ok()
            .filter(|out| out.status.success())
            .and_then(|out| String::from_utf8(out.stdout).ok())
            .map(|out| out.trim().to_owned())
    };
    let (commit, dirty) = build_identity::identity(&root);
    // Watch all tracked inputs, including deletions; untracked caches do not affect identity.
    if let Some(files) = git(&["ls-files"]) {
        for file in files.lines() {
            println!("cargo:rerun-if-changed={}", root.join(file).display());
        }
        for name in [
            "HEAD".to_owned(),
            git(&["symbolic-ref", "HEAD"]).unwrap_or_default(),
            "index".into(),
        ] {
            if let Some(path) = git(&["rev-parse", "--git-path", &name]) {
                println!("cargo:rerun-if-changed={}", root.join(path).display());
            }
        }
    } else {
        println!(
            "cargo:rerun-if-changed={}",
            root.join(".build-commit").display()
        );
    }
    println!(
        "cargo:rustc-env=GB_BUILD_VERSION={}+{commit}{}",
        env!("CARGO_PKG_VERSION"),
        if dirty { ".dev" } else { "" }
    );
}
