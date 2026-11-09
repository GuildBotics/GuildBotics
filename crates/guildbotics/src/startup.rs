//! Process-wide initialization before any command or asynchronous task starts.

pub(crate) fn initialize() {
    // Select ring explicitly: the complete executable also links aws-lc-rs.
    let _ = rustls::crypto::ring::default_provider().install_default();
}

#[cfg(test)]
mod tests {
    // Run each probe in a fresh process, including under ordinary cargo test.
    #[test]
    fn tls_startup() {
        for (probe, success) in [("initialized", true), ("uninitialized", false)] {
            let output = std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "startup::tests::tls_probe", "--nocapture"])
                .env("GB_TLS_STARTUP_PROBE", probe)
                .output()
                .unwrap();
            assert_eq!(output.status.success(), success, "{output:?}");
            if !success {
                assert!(String::from_utf8_lossy(&output.stderr).contains(
                    "Could not automatically determine the process-level CryptoProvider"
                ));
            }
        }
    }

    #[test]
    fn tls_probe() {
        let Ok(probe) = std::env::var("GB_TLS_STARTUP_PROBE") else {
            return;
        };
        assert!(rustls::crypto::CryptoProvider::get_default().is_none());
        if probe == "initialized" {
            super::initialize();
            super::initialize();
        }
        // Other dependencies may still use the process-wide default.
        let config = rustls::ClientConfig::builder()
            .with_root_certificates(rustls::RootCertStore::empty())
            .with_no_client_auth();
        rustls::ClientConnection::new(
            std::sync::Arc::new(config),
            "slack.test".try_into().unwrap(),
        )
        .unwrap();
    }
}
