//! The `branvil` executable.

fn main() -> std::process::ExitCode {
    // Started by Git as GIT_ASKPASS when talking to an upstream host.
    if let Some(code) = branvil::askpass_main() {
        return code;
    }
    // Its secrets: the directory and key the environment names (`vault::Sealed`).
    branvil::main(
        branvil::Invocation::standalone(),
        std::sync::Arc::new(branvil::vault::Sealed::from_env()),
        std::env::args_os(),
    )
}
