fn main() {
    let path = "../../hub-update.json";
    println!("cargo:rerun-if-changed={path}");
    let declaration = match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => "{}".into(),
        Err(error) => panic!("{path}: {error}"),
    };
    std::fs::write(
        std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap()).join("hub-update.json"),
        declaration,
    )
    .unwrap();
}
