//! The shared re-exec helper must either establish its preconditions or fail once.
#[path = "support/process.rs"]
mod process;

const MARKER: &str = "GB_TEST_RERUN";
const SCENARIO: &str = "GB_TEST_REEXEC_PROBE";
const DIRTY: &str = "GB_TEST_DIRTY";

#[test]
fn child_probe() {
    let Ok(scenario) = std::env::var(SCENARIO) else {
        return;
    };
    // Bound the reproduction independently of the helper under test, so even
    // removing its guard cannot exhaust the host's process table.
    let visits = std::env::var_os("GB_TEST_PROBE_VISITS").unwrap();
    let count: u32 = std::fs::read_to_string(&visits).unwrap().parse().unwrap();
    std::fs::write(&visits, (count + 1).to_string()).unwrap();
    assert!(count < 2, "probe recursion limit exceeded");
    match scenario.as_str() {
        "clean" => {
            if !process::rerun("child_probe", false, &[DIRTY]) {
                assert!(std::env::var_os(DIRTY).is_none());
            }
        }
        // The marker is deliberately restored by the launcher after env_remove.
        // This models a precondition that remains unmet in the child without
        // requiring root, capabilities, or an unbounded process reproduction.
        "stuck" => {
            process::rerun("child_probe", false, &[MARKER]);
        }
        _ => panic!("unexpected scenario"),
    }
}

#[test]
fn reruns_clean_the_environment_and_refuse_unmet_child_preconditions() {
    for (scenario, marker, success) in [
        ("clean", "parent", true),
        ("stuck", "parent", false),
        ("stuck", "child_probe", false),
    ] {
        let temp = tempfile::tempdir().unwrap();
        let visits = temp.path().join("visits");
        std::fs::write(&visits, "0").unwrap();
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "child_probe", "--nocapture"])
            .env(SCENARIO, scenario)
            .env("GB_TEST_PROBE_VISITS", &visits)
            .env(MARKER, marker)
            .env(DIRTY, "dirty")
            .output()
            .unwrap();
        let text = format!(
            "{}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        assert_eq!(output.status.success(), success, "{scenario}: {text}");
        assert_eq!(
            std::fs::read_to_string(visits).unwrap(),
            if marker == "child_probe" { "1" } else { "2" },
            "{text}"
        );
        if !success {
            assert!(
                text.contains("re-exec preconditions still unmet in child"),
                "{text}"
            );
        }
    }
}
