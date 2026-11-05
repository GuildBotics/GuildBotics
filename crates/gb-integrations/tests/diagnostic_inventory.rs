//! Every retained provider diagnostic has a production Rust use or a reason
//! why its former boundary no longer exists. Test literals cannot count as use.
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use syn::visit::{self, Visit};

const NAMESPACES: [&str; 2] = ["integrations.branvil.api", "integrations.resources"];

fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn is_diagnostic(key: &str) -> bool {
    NAMESPACES
        .iter()
        .any(|ns| key.starts_with(&format!("{ns}.")))
}

// Evaluate cfg with test=false. Other predicates remain possible so platform
// and optional feature branches are inventoried without building each one.
fn possible_cfg(meta: &syn::Meta) -> Option<bool> {
    match meta {
        syn::Meta::Path(p) if p.is_ident("test") => Some(false),
        syn::Meta::List(list) => {
            let nested = list
                .parse_args_with(
                    syn::punctuated::Punctuated::<syn::Meta, syn::Token![,]>::parse_terminated,
                )
                .unwrap();
            let values: Vec<_> = nested.iter().map(possible_cfg).collect();
            if list.path.is_ident("not") {
                values.first().copied().flatten().map(|v| !v)
            } else if list.path.is_ident("all") {
                if values.contains(&Some(false)) {
                    Some(false)
                } else if values.iter().all(|v| *v == Some(true)) {
                    Some(true)
                } else {
                    None
                }
            } else if list.path.is_ident("any") {
                if values.contains(&Some(true)) {
                    Some(true)
                } else if values.iter().all(|v| *v == Some(false)) {
                    Some(false)
                } else {
                    None
                }
            } else {
                None
            }
        }
        _ => None,
    }
}

fn production(attrs: &[syn::Attribute]) -> bool {
    attrs.iter().all(|attr| {
        !attr.path().is_ident("cfg") || {
            possible_cfg(&attr.parse_args::<syn::Meta>().unwrap()) != Some(false)
        }
    })
}

#[derive(Default)]
struct Usage {
    directory: PathBuf,
    keys: BTreeSet<String>,
}

impl Usage {
    fn file(&mut self, file: &Path) {
        let source =
            std::fs::read_to_string(file).unwrap_or_else(|e| panic!("{}: {e}", file.display()));
        let parsed = syn::parse_file(&source).unwrap_or_else(|e| panic!("{}: {e}", file.display()));
        let previous = self.directory.clone();
        self.directory = if matches!(
            file.file_stem().and_then(|s| s.to_str()),
            Some("lib" | "main" | "mod")
        ) {
            file.parent().unwrap().to_owned()
        } else {
            file.with_extension("")
        };
        self.visit_file(&parsed);
        self.directory = previous;
    }
}

impl<'ast> Visit<'ast> for Usage {
    fn visit_item(&mut self, node: &'ast syn::Item) {
        let attrs = match node {
            syn::Item::Const(i) => &i.attrs,
            syn::Item::Enum(i) => &i.attrs,
            syn::Item::ExternCrate(i) => &i.attrs,
            syn::Item::Fn(i) => &i.attrs,
            syn::Item::ForeignMod(i) => &i.attrs,
            syn::Item::Impl(i) => &i.attrs,
            syn::Item::Macro(i) => &i.attrs,
            syn::Item::Mod(i) => &i.attrs,
            syn::Item::Static(i) => &i.attrs,
            syn::Item::Struct(i) => &i.attrs,
            syn::Item::Trait(i) => &i.attrs,
            syn::Item::TraitAlias(i) => &i.attrs,
            syn::Item::Type(i) => &i.attrs,
            syn::Item::Union(i) => &i.attrs,
            syn::Item::Use(i) => &i.attrs,
            _ => return,
        };
        if production(attrs) {
            visit::visit_item(self, node);
        }
    }
    fn visit_item_mod(&mut self, node: &'ast syn::ItemMod) {
        if !production(&node.attrs) {
            return;
        }
        let previous = self.directory.clone();
        if let Some((_, items)) = &node.content {
            self.directory.push(node.ident.to_string());
            for item in items {
                self.visit_item(item);
            }
        } else {
            let explicit = node.attrs.iter().find_map(|a| {
                if a.path().is_ident("path")
                    && let syn::Meta::NameValue(v) = &a.meta
                    && let syn::Expr::Lit(l) = &v.value
                    && let syn::Lit::Str(s) = &l.lit
                {
                    Some(self.directory.join(s.value()))
                } else {
                    None
                }
            });
            let file = explicit.unwrap_or_else(|| {
                let direct = self.directory.join(format!("{}.rs", node.ident));
                if direct.exists() {
                    direct
                } else {
                    self.directory.join(node.ident.to_string()).join("mod.rs")
                }
            });
            self.file(&file);
        }
        self.directory = previous;
    }
    fn visit_item_fn(&mut self, node: &'ast syn::ItemFn) {
        if production(&node.attrs) {
            visit::visit_item_fn(self, node);
        }
    }
    fn visit_impl_item_fn(&mut self, node: &'ast syn::ImplItemFn) {
        if production(&node.attrs) {
            visit::visit_impl_item_fn(self, node);
        }
    }
    fn visit_trait_item_fn(&mut self, node: &'ast syn::TraitItemFn) {
        if production(&node.attrs) {
            visit::visit_trait_item_fn(self, node);
        }
    }
    fn visit_impl_item_const(&mut self, node: &'ast syn::ImplItemConst) {
        if production(&node.attrs) {
            visit::visit_impl_item_const(self, node);
        }
    }
    fn visit_trait_item_const(&mut self, node: &'ast syn::TraitItemConst) {
        if production(&node.attrs) {
            visit::visit_trait_item_const(self, node);
        }
    }
    fn visit_local(&mut self, node: &'ast syn::Local) {
        if production(&node.attrs) {
            visit::visit_local(self, node);
        }
    }
    fn visit_stmt_macro(&mut self, node: &'ast syn::StmtMacro) {
        if production(&node.attrs) {
            visit::visit_stmt_macro(self, node);
        }
    }
    // Matching an already emitted key only classifies it; it cannot establish
    // that any production path still emits the diagnostic.
    fn visit_pat(&mut self, _node: &'ast syn::Pat) {}

    fn visit_lit_str(&mut self, node: &'ast syn::LitStr) {
        if is_diagnostic(&node.value()) {
            self.keys.insert(node.value());
        }
    }
    fn visit_macro(&mut self, node: &'ast syn::Macro) {
        // Tokenization has removed comments; match complete string arguments
        // inside opaque macro groups, including nested groups.
        let tokens = node.tokens.to_string();
        if !tokens.contains("integrations.") {
            return;
        }
        let keys =
            regex::Regex::new(r#""(integrations\.(?:branvil\.api|resources)\.[a-z_]+)""#).unwrap();
        for found in keys.captures_iter(&tokens) {
            self.keys.insert(found[1].to_owned());
        }
    }
}

fn locale(namespace: &str, language: &str) -> BTreeMap<String, String> {
    let path = root().join("guildbotics/templates/locales").join(format!(
        "{}.{}.yml",
        namespace.replace('.', "/"),
        language
    ));
    let value: serde_json::Value =
        serde_saphyr::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    value[language]
        .as_object()
        .unwrap()
        .iter()
        .map(|(key, value)| {
            let text = value.as_str().expect("diagnostics must be strings");
            assert!(
                !text.trim().is_empty(),
                "{namespace}.{key} has empty {language} text"
            );
            (format!("{namespace}.{key}"), text.into())
        })
        .collect()
}

fn exclusions(source: &str) -> BTreeSet<String> {
    let mut keys = BTreeSet::new();
    for line in source
        .lines()
        .filter(|l| !l.trim().is_empty() && !l.starts_with('#'))
    {
        let (key, reason) = line
            .split_once('\t')
            .expect("exclusions require key TAB reason");
        assert!(!reason.trim().is_empty(), "{key} needs an exclusion reason");
        assert!(keys.insert(key.to_owned()), "duplicate exclusion: {key}");
    }
    keys
}

fn check_population(
    locales: &BTreeSet<String>,
    live: &BTreeSet<String>,
    excluded: &BTreeSet<String>,
) {
    assert!(
        live.is_disjoint(excluded),
        "a restored production key must lose its exclusion"
    );
    assert_eq!(
        locales,
        &live.union(excluded).cloned().collect(),
        "every locale key needs a production use or a reasoned exclusion"
    );
}

#[test]
fn all_provider_locale_keys_are_accounted_for_in_production() {
    let mut usage = Usage::default();
    for entry in std::fs::read_dir(root().join("crates")).unwrap() {
        for name in ["lib.rs", "main.rs"] {
            let file = entry.as_ref().unwrap().path().join("src").join(name);
            if file.exists() {
                usage.file(&file);
            }
        }
    }
    let placeholders = regex::Regex::new(r"%\{([^}]+)\}").unwrap();
    let mut keys = BTreeSet::new();
    for namespace in NAMESPACES {
        let en = locale(namespace, "en");
        let ja = locale(namespace, "ja");
        assert_eq!(
            en.keys().collect::<Vec<_>>(),
            ja.keys().collect::<Vec<_>>(),
            "{namespace} locale keys differ"
        );
        for (key, text) in &en {
            let params = |text: &str| {
                placeholders
                    .captures_iter(text)
                    .map(|m| m[1].to_owned())
                    .collect::<BTreeSet<_>>()
            };
            assert_eq!(params(text), params(&ja[key]), "{key} placeholder mismatch");
            keys.insert(key.clone());
        }
    }
    check_population(
        &keys,
        &usage.keys,
        &exclusions(
            &std::fs::read_to_string(
                Path::new(env!("CARGO_MANIFEST_DIR"))
                    .join("tests/fixtures/diagnostic_exclusions.txt"),
            )
            .unwrap(),
        ),
    );
}

#[test]
fn inventory_ignores_test_only_literals_and_comments() {
    let mut usage = Usage::default();
    usage.visit_file(
        &syn::parse_file(
            r#"
        // "integrations.resources.comment"
        #[cfg(test)] const TEST_KEY: &str = "integrations.resources.test_constant";
        #[cfg(test)] mod tests { fn fake() { t("integrations.resources.fake"); } }
        #[cfg(all(test, unix))] fn test_only() { t("integrations.resources.test_only"); }
        #[cfg(not(test))] fn real() { fail!("integrations.resources.real"); }
        #[cfg(any(test, unix))] fn platform() { t("integrations.resources.platform"); }
    "#,
        )
        .unwrap(),
    );
    assert_eq!(
        usage.keys,
        BTreeSet::from([
            "integrations.resources.real".into(),
            "integrations.resources.platform".into()
        ])
    );
}

#[test]
#[should_panic(expected = "every locale key needs")]
fn deleting_a_live_use_fails_inventory() {
    check_population(
        &BTreeSet::from(["integrations.resources.real".into()]),
        &BTreeSet::new(),
        &BTreeSet::new(),
    );
}

#[test]
#[should_panic(expected = "every locale key needs")]
fn removing_an_exclusion_fails_inventory() {
    let retained = BTreeSet::from(["integrations.branvil.api.timeout".into()]);
    check_population(&retained, &BTreeSet::new(), &exclusions(""));
}

#[test]
#[should_panic(expected = "a restored production key")]
fn live_keys_cannot_keep_an_exclusion() {
    let key = BTreeSet::from(["integrations.branvil.api.timeout".into()]);
    check_population(&key, &key, &key);
}

#[test]
#[should_panic(expected = "needs an exclusion reason")]
fn exclusions_require_a_reason() {
    exclusions("integrations.branvil.api.timeout\t ");
}

#[test]
fn every_resource_diagnostic_preserves_its_key_and_safe_arguments_at_runtime() {
    use gb_ports::{ServiceError, ServiceErrorClass};
    let cases = [
        (ServiceError::Failed, "service_failed", vec![]),
        (ServiceError::OutOfScope, "out_of_scope", vec![]),
        (
            ServiceError::MissingCredential {
                name: "MEMBER_SERVICE_TOKEN".into(),
            },
            "missing_credential",
            vec![("name", "MEMBER_SERVICE_TOKEN")],
        ),
        (
            ServiceError::CredentialPrefixConflict {
                member: "test-one".into(),
                conflicting_member: "test_one".into(),
            },
            "credential_prefix_conflict",
            vec![("member", "test-one"), ("conflicting_member", "test_one")],
        ),
        (ServiceError::NotFound, "not_found", vec![]),
        (ServiceError::Unavailable, "unavailable", vec![]),
        (ServiceError::Unsupported, "operation_unsupported", vec![]),
        (ServiceError::InvalidResult, "invalid_result", vec![]),
        (
            ServiceError::Diagnostic {
                class: ServiceErrorClass::Unsupported,
                key: "integrations.resources.unsupported",
                args: [
                    ("service".into(), "ticket_service".into()),
                    ("name".into(), "unknown-provider".into()),
                ]
                .into(),
            },
            "unsupported",
            vec![("service", "ticket_service"), ("name", "unknown-provider")],
        ),
        (ServiceError::ResponseTooLarge, "too_large", vec![]),
        (ServiceError::InvalidResource, "resource", vec![]),
        (ServiceError::InvalidParameters, "parameters", vec![]),
        (ServiceError::InvalidContinuation, "continuation", vec![]),
        (
            ServiceError::AttachmentMissing {
                url: "local://tickets/test/repo/1".into(),
                id: "test-report".into(),
            },
            "attachment_missing",
            vec![
                ("url", "local://tickets/test/repo/1"),
                ("id", "test-report"),
            ],
        ),
        (
            ServiceError::AttachmentTooLarge {
                name: "report.txt".into(),
                limit: 123,
            },
            "attachment_too_large",
            vec![("name", "report.txt"), ("limit", "123")],
        ),
    ];
    let placeholders = regex::Regex::new(r"%\{([^}]+)\}").unwrap();
    let mut asserted = BTreeSet::new();
    for (error, suffix, args) in cases {
        let key = format!("integrations.resources.{suffix}");
        assert_eq!(error.translation_key(), key);
        assert_eq!(error.to_string(), key);
        let expected: BTreeMap<String, String> = args
            .into_iter()
            .map(|(name, value)| (name.into(), value.into()))
            .collect();
        assert_eq!(error.translation_args(), expected, "{key}");
        for language in ["en", "ja"] {
            let text = &locale("integrations.resources", language)[&key];
            let names: BTreeSet<_> = placeholders
                .captures_iter(text)
                .map(|m| m[1].to_owned())
                .collect();
            assert_eq!(
                names,
                expected.keys().cloned().collect(),
                "{key} runtime arguments do not satisfy {language} placeholders"
            );
        }
        assert!(asserted.insert(key));
    }
    assert_eq!(
        asserted,
        locale("integrations.resources", "en").into_keys().collect(),
        "add a runtime constructor assertion for each new resource diagnostic"
    );
}

#[test]
fn classification_patterns_do_not_count_as_production_emissions() {
    let mut usage = Usage::default();
    usage.visit_file(
        &syn::parse_file(
            r#"
        fn class(key: &str) -> bool {
            match key {
                "integrations.branvil.api.every_on_board"
                | "integrations.branvil.api.foreign_ticket" => true,
                _ => false,
            }
        }
    "#,
        )
        .unwrap(),
    );
    assert!(usage.keys.is_empty());
}
