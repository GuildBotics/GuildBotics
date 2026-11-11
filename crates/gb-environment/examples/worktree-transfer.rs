//! Exercise the production client export from the cross-language contract test.
fn main() {
    let root = std::path::PathBuf::from(std::env::args_os().nth(1).expect("source"));
    let copy = gb_environment::worktree::Copy::new(
        root,
        "/work".into(),
        vec![".git".into(), ".guildbotics".into()],
        std::path::PathBuf::new(),
    )
    .unwrap();
    let upload = gb_environment::worktree::transfer::Upload::capture(&copy).unwrap();
    println!("{}", serde_json::to_string(&upload).unwrap());
}
