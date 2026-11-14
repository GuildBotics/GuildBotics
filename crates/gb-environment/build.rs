//! The SDK installs its pinned runtime during its own build. Embed that
//! runtime in the final executable instead of depending on a developer's home.
//! The SDK does not fetch Windows assets: release builders must supply the
//! pinned Windows msb.exe and libkrunfw.dll under MSB_HOME before compiling.
fn main() {
    if std::env::var_os("CARGO_FEATURE_HOST").is_none() {
        return;
    }
    let home = std::env::var_os("MSB_HOME")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| {
            std::path::PathBuf::from(std::env::var_os("HOME").expect("HOME")).join(".microsandbox")
        });
    let os = std::env::var("CARGO_CFG_TARGET_OS").unwrap();
    let lib = match os.as_str() {
        "macos" => "libkrunfw.5.dylib",
        "windows" => "libkrunfw.dll",
        _ => "libkrunfw.so.5.6.1",
    };
    println!("cargo:rerun-if-env-changed=MSB_HOME");
    println!("cargo:rerun-if-env-changed=HOME");
    println!(
        "cargo:rustc-env=GB_MSB={}",
        home.join("bin")
            .join(if os == "windows" { "msb.exe" } else { "msb" })
            .display()
    );
    println!(
        "cargo:rustc-env=GB_KRUN={}",
        home.join("lib").join(lib).display()
    );
}
