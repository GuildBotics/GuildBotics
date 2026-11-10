//! Route declarations shared with boundary tests; neither host knows the other.

/// Credentials admitted by a route. Permissions within a member route remain
/// the domain's responsibility (for example agent-only or runner-only writes).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RouteAuth {
    OwnerSession,
    MemberSession,
    Session,
    OwnerOrConsent,
    Basic,
    HookSecret,
    Public,
}

/// A registered method and path, including explicit catch-all routes.
#[derive(Clone, Copy, Debug)]
pub struct Route {
    pub method: &'static str,
    pub path: &'static str,
    pub auth: RouteAuth,
}

/// Builds the router and its inventory from the same method declarations.
/// Omitting a classification is a compilation error.
#[macro_export]
macro_rules! classified_routes {
    ($vis:vis fn $name:ident() -> $router:ty, $table:ident,
     $(($path:expr, $(($method:ident, $handler:expr, $auth:ident)),+)),* $(,)?) => {
        $vis fn $name() -> $router {
            axum::Router::new()$(.route($path,
                axum::routing::MethodRouter::new()$(.$method($handler))+))*
        }
        pub(crate) const $table: &[$crate::Route] = &[
            $($($crate::Route { method: stringify!($method), path: $path,
                auth: $crate::RouteAuth::$auth },)+)*
        ];
    };
}

#[cfg(test)]
mod tests {
    #[test]
    fn mounted_routers_cannot_register_unclassified_routes() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap();
        for (file, direct_routes) in [
            ("branvil/src/http.rs", 1), // Git's body limit; path comes from GIT_ROUTE.
            ("branvil/src/ci/api.rs", 0),
            ("branvil/src/ci/admin.rs", 0),
            ("branvil/src/upstream/http.rs", 0),
            ("branvil/src/autoland.rs", 0),
            ("branvil/src/landing.rs", 0),
            ("gb-server/src/api.rs", 2), // The catalog macro and standalone root.
            ("gb-server/src/ui.rs", 2),  // MAIN_PATHS and ASSET_PATH.
        ] {
            let source = std::fs::read_to_string(root.join(file)).unwrap();
            let production = source.split("#[cfg(test)]\nmod tests").next().unwrap();
            assert_eq!(
                production.matches(".route(").count(),
                direct_routes,
                "{file}: register the route in the classified catalog"
            );
        }
    }
}
