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
