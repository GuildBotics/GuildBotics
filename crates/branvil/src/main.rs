//! The `branvil` executable.

fn main() -> std::process::ExitCode {
    // Started by Git as GIT_ASKPASS when talking to an upstream host.
    if let Some(code) = branvil::askpass_main() {
        return code;
    }
    branvil::main(branvil::Invocation::standalone(), std::env::args_os())
}
