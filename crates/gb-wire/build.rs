//! Generates the wire's Rust types from the JSON Schemas the Python version
//! writes (`wire/schema`, from its pydantic types), so the two cannot differ.

use std::path::Path;

/// The schemas the host reads and writes; what they refer to comes with them.
const ROOTS: &[&str] = &[
    "ResolvedGuest",
    "Entry",
    "HostTurn",
    "CommandFacts",
    "CommandRequest",
    "CommandReply",
    "CliAgentExecutionResult",
    "TroubleshootingResult",
];

fn main() {
    let schemas = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../wire/schema");
    let mut settings = typify::TypeSpaceSettings::default();
    settings.with_derive("PartialEq".into());
    let mut space = typify::TypeSpace::new(&settings);
    for root in ROOTS {
        let path = schemas.join(format!("{root}.json"));
        println!("cargo::rerun-if-changed={}", path.display());
        let text = std::fs::read_to_string(&path).expect("a schema");
        let schema: schemars::schema::RootSchema =
            serde_json::from_str(&text).expect("JSON Schema");
        space
            .add_root_schema(schema)
            .expect("a schema typify reads");
    }
    let file: syn::File = syn::parse2(space.to_stream()).expect("Rust");
    let out = Path::new(&std::env::var("OUT_DIR").expect("OUT_DIR")).join("wire.rs");
    std::fs::write(out, prettyplease::unparse(&file)).expect("written");
}
