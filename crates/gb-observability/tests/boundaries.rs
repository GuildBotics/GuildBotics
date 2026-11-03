//! Enumerate production trace roots, rather than sampling their callers.
use std::collections::BTreeSet;
use std::path::Path;
use syn::visit::{self, Visit};

#[derive(Default)]
struct Roots {
    function: String,
    roots: BTreeSet<String>,
    boundaries: BTreeSet<String>,
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
        if node.method == "run_command" {
            self.boundaries.insert(self.function.clone());
        }
        visit::visit_expr_method_call(self, node);
    }
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
    let mut discovered = BTreeSet::new();
    for file in String::from_utf8(files.stdout)
        .unwrap()
        .lines()
        .filter(|p| p.contains("/src/") && p.ends_with(".rs"))
    {
        let source = std::fs::read_to_string(root.join(file)).unwrap();
        let mut found = Roots::default();
        found.visit_file(&syn::parse_file(&source).unwrap());
        for function in found.roots {
            assert!(
                found.boundaries.contains(&function),
                "{file}:{function} bypasses the boundary"
            );
            discovered.insert((file.to_owned(), function));
        }
    }
    let declared = BTreeSet::from([("crates/gb-server/src/commands.rs".into(), "start".into())]);
    assert_eq!(discovered, declared, "declare how each new root ends");
    assert!(gb_observability::TRACE_COMPLETED_EVENT_TYPES.contains(&"command.finished"));
}

#[test]
fn discovery_catches_an_undeclared_root_but_excludes_joining() {
    let mut found = Roots::default();
    found.visit_file(&syn::parse_file("fn fresh() { let t = gb_observability::Trace::open(d, s); } fn joined() { Trace::join(d, id); }").unwrap());
    assert_eq!(found.roots, BTreeSet::from(["fresh".into()]));
    assert!(found.boundaries.is_empty());
}
