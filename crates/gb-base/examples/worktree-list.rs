//! Exercise host selection independently of the environment runtime.
fn main() -> anyhow::Result<()> {
    let mut args = std::env::args().skip(1);
    let root = args.next().expect("working directory");
    let manifest = gb_base::worktree::list(std::path::Path::new(&root), &args.collect::<Vec<_>>())?;
    serde_json::to_writer(std::io::stdout(), &manifest)?;
    Ok(())
}
