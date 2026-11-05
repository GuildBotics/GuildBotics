//! Enumerate production trace roots, rather than sampling their callers.
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
use syn::visit::{self, Visit};

#[derive(Default)]
struct Roots {
    function: String,
    roots: BTreeSet<String>,
    boundaries: BTreeMap<String, BTreeSet<String>>,
}

impl<'ast> Visit<'ast> for Roots {
    fn visit_item_mod(&mut self, node: &'ast syn::ItemMod) {
        if node.ident != "tests" {
            visit::visit_item_mod(self, node);
        }
    }
    fn visit_item_fn(&mut self, node: &'ast syn::ItemFn) {
        let previous = std::mem::replace(&mut self.function, node.sig.ident.to_string());
        visit::visit_item_fn(self, node);
        self.function = previous;
    }
    fn visit_impl_item_fn(&mut self, node: &'ast syn::ImplItemFn) {
        let previous = std::mem::replace(&mut self.function, node.sig.ident.to_string());
        visit::visit_impl_item_fn(self, node);
        self.function = previous;
    }
    fn visit_expr_call(&mut self, node: &'ast syn::ExprCall) {
        if let syn::Expr::Path(path) = node.func.as_ref() {
            let names: Vec<_> = path
                .path
                .segments
                .iter()
                .map(|s| s.ident.to_string())
                .collect();
            if names.ends_with(&["Trace".into(), "open".into()]) {
                self.roots.insert(self.function.clone());
            }
        }
        visit::visit_expr_call(self, node);
    }
    fn visit_expr_method_call(&mut self, node: &'ast syn::ExprMethodCall) {
        self.boundaries
            .entry(self.function.clone())
            .or_default()
            .insert(node.method.to_string());
        visit::visit_expr_method_call(self, node);
    }
    fn visit_expr_struct(&mut self, node: &'ast syn::ExprStruct) {
        self.boundaries
            .entry(self.function.clone())
            .or_default()
            .insert(node.path.segments.last().unwrap().ident.to_string());
        visit::visit_expr_struct(self, node);
    }
}

fn production_source(path: &str) -> bool {
    let name = Path::new(path).file_name().unwrap().to_str().unwrap();
    path.contains("/src/")
        && name.ends_with(".rs")
        && !path.contains("/tests/")
        && name != "tests.rs"
        && !name.starts_with("test_")
        && !name.ends_with("_tests.rs")
}

#[test]
fn every_root_declares_its_terminal_event_and_uses_the_cancellation_safe_boundary() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let files = std::process::Command::new("git")
        .args(["ls-files", "crates", "--", "*.rs"])
        .current_dir(&root)
        .output()
        .unwrap();
    assert!(files.status.success());
    // A root either runs a command, delegates to the ticket command boundary,
    // or holds the review-limit settlement guard (whose Drop records failure).
    let declared = BTreeMap::from([
        (
            ("crates/gb-server/src/commands.rs", "start"),
            ("run_command", "command.finished"),
        ),
        (
            (
                "crates/gb-server/src/workflow_runtime.rs",
                "selection_failed",
            ),
            ("run_command", "command.finished"),
        ),
        (
            ("crates/gb-server/src/workflow_runtime.rs", "command"),
            ("run_command", "command.finished"),
        ),
        (
            ("crates/gb-server/src/workflow_runtime.rs", "manual_ticket"),
            ("ticket_traced", "command.finished"),
        ),
        (
            ("crates/gb-server/src/workflow_runtime.rs", "ticket_traced"),
            ("run_command", "command.finished"),
        ),
        (
            ("crates/gb-server/src/ticket_selector.rs", "hand_over"),
            ("ReviewSettlement", "review_limit.finished"),
        ),
    ]);
    let mut discovered = BTreeSet::new();
    for file in String::from_utf8(files.stdout)
        .unwrap()
        .lines()
        .filter(|p| production_source(p))
    {
        let source = std::fs::read_to_string(root.join(file)).unwrap();
        let mut found = Roots::default();
        found.visit_file(&syn::parse_file(&source).unwrap());
        for function in found.roots {
            let key = (file, function.as_str());
            let (boundary, _) = declared
                .get(&key)
                .unwrap_or_else(|| panic!("declare how new root {file}:{function} ends"));
            assert!(
                found
                    .boundaries
                    .get(&function)
                    .is_some_and(|calls| calls.contains(*boundary)),
                "{file}:{function} bypasses the boundary"
            );
            discovered.insert((file.to_owned(), function));
        }
    }
    assert_eq!(
        discovered,
        declared
            .keys()
            .map(|(file, function)| (file.to_string(), function.to_string()))
            .collect(),
        "declare how each new root ends"
    );
    for (_, event) in declared.values() {
        assert!(gb_observability::TRACE_COMPLETED_EVENT_TYPES.contains(event));
    }
}

#[test]
fn discovery_catches_an_undeclared_root_but_excludes_joining() {
    let mut found = Roots::default();
    found.visit_file(&syn::parse_file("fn fresh() { let t = gb_observability::Trace::open(d, s); } fn joined() { Trace::join(d, id); }").unwrap());
    assert_eq!(found.roots, BTreeSet::from(["fresh".into()]));
    assert!(found.boundaries.is_empty());
}

#[test]
fn discovery_keeps_each_boundary_with_its_own_function() {
    let mut found = Roots::default();
    found.visit_file(
        &syn::parse_file(
            r#"
        fn command() { trace.run_command(); }
        fn manual_ticket() { self.ticket_traced(); }
        fn hand_over() { let guard = ReviewSettlement { trace }; }
        fn unguarded() { Trace::open(d, s); }
        mod tests { fn ignored() { Trace::open(d, s); } }
    "#,
        )
        .unwrap(),
    );
    for (function, boundary) in [
        ("command", "run_command"),
        ("manual_ticket", "ticket_traced"),
        ("hand_over", "ReviewSettlement"),
    ] {
        assert_eq!(
            found.boundaries[function],
            BTreeSet::from([boundary.into()])
        );
    }
    assert!(!found.boundaries.contains_key("unguarded"));
    assert_eq!(found.roots, BTreeSet::from(["unguarded".into()]));
}

#[test]
fn production_scan_excludes_out_of_line_test_modules() {
    for path in [
        "crates/gb-server/src/ticket_selector/tests.rs",
        "crates/gb-server/src/tool_login/tests/real_device.rs",
        "crates/gb-server/src/test_chat.rs",
        "crates/gb-server/src/gateway_tests.rs",
        "crates/gb-server/tests/workflow.rs",
    ] {
        assert!(!production_source(path), "{path}");
    }
    for path in [
        "crates/gb-server/src/ticket_selector.rs",
        "crates/gb-server/src/workflow_runtime.rs",
        "crates/gb-server/src/fresh.rs",
    ] {
        assert!(production_source(path), "{path}");
    }
}
