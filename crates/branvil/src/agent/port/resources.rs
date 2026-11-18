use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use super::*;

pub const MAX_PAGE_BYTES: usize = 10 * 1024 * 1024 / 16;
pub const MAX_LOG_TAIL_BYTES: usize = MAX_PAGE_BYTES / 10;
/// A literal repository or tracker path, with a namespace of any depth.
pub fn valid_repository_path(path: &str) -> bool {
    path.contains('/')
        && path.split('/').all(|name| {
            !name.is_empty()
                && name != "."
                && name != ".."
                && name
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"_.-".contains(&c))
        })
}

macro_rules! resources {
    ($($variant:ident => ($name:literal, $ticket:literal, $detail:literal)),+ $(,)?) => {
        #[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
        pub enum Resource {
            $(#[serde(rename = $name)] $variant),+
        }
        impl Resource {
            pub const ALL: &'static [Self] = &[$(Self::$variant),+];
            pub fn name(self) -> &'static str {
                match self { $(Self::$variant => $name),+ }
            }
            pub fn is_ticket_service(self) -> bool {
                match self { $(Self::$variant => $ticket),+ }
            }
            pub fn is_detail(self) -> bool {
                match self { $(Self::$variant => $detail),+ }
            }
        }
        impl std::str::FromStr for Resource {
            type Err = ServiceError;
            fn from_str(name: &str) -> Result<Self, Self::Err> {
                match name { $($name => Ok(Self::$variant)),+, _ => Err(ServiceError::InvalidResource) }
            }
        }
    }
}

resources! {
    TrackerTickets => ("tracker_tickets", true, false),
    Tickets => ("tickets", true, true),
    TicketComments => ("ticket_comments", true, false),
    TicketAttachments => ("ticket_attachments", true, false),
    TicketPullRequests => ("ticket_pull_requests", false, false),
    PullRequests => ("pull_requests", false, true),
    PullRequestComments => ("pull_request_comments", false, false),
    PullRequestReviews => ("pull_request_reviews", false, false),
    PullRequestFiles => ("pull_request_files", false, false),
    PullRequestThreads => ("pull_request_threads", false, false),
    PullRequestReadiness => ("pull_request_readiness", false, true),
    DependencyAlerts => ("dependency_alerts", false, false),
}

/// One page of Branvil's views of a resource, with the ticket or pull request
/// it was read of when that is one item.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct ReadPage {
    items: Vec<Value>,
    continuation: Option<String>,
    target: Option<WorkTarget>,
}

impl ReadPage {
    pub fn of<T: Serialize>(
        items: impl IntoIterator<Item = T>,
        continuation: Option<String>,
        target: Option<WorkTarget>,
    ) -> Result<Self, ServiceError> {
        Ok(Self {
            items: items
                .into_iter()
                .map(|i| serde_json::to_value(i).map_err(|_| ServiceError::InvalidResult))
                .collect::<Result<_, _>>()?,
            continuation,
            target,
        })
    }

    pub fn items(&self) -> &[Value] {
        &self.items
    }
    pub fn continuation(&self) -> Option<&str> {
        self.continuation.as_deref()
    }
    pub fn target(&self) -> Option<&WorkTarget> {
        self.target.as_ref()
    }
}

/// Validate reads before calling any provider. Conditions are data, never
/// HTTP methods, paths, headers, or executable queries.
pub fn read_conditions(
    resource: Resource,
    parameters: Option<&Value>,
    continuation: &str,
) -> Result<Value, ServiceError> {
    if resource.is_detail() && !continuation.is_empty() {
        return Err(ServiceError::InvalidContinuation);
    }
    let empty = json!({});
    let given = parameters
        .unwrap_or(&empty)
        .as_object()
        .ok_or(ServiceError::InvalidParameters)?;
    let mut result = match resource {
        Resource::TrackerTickets => json!({"text":"", "state":"open", "label":"", "page_size":30}),
        Resource::DependencyAlerts => json!({"state":"open", "page_size":30, "id":""}),
        Resource::PullRequestReadiness => {
            json!({"failed_logs":false, "log_tail_bytes":MAX_LOG_TAIL_BYTES})
        }
        _ if resource.is_detail() => json!({}),
        _ => json!({"page_size":30}),
    };
    if resource == Resource::TrackerTickets {
        result["lane"] = Value::Null;
    }
    let defaults = result.as_object_mut().expect("conditions are objects");
    for (key, value) in given {
        let valid = match key.as_str() {
            "page_size" => value.as_u64().is_some_and(|n| (1..=100).contains(&n)),
            "log_tail_bytes" => value
                .as_u64()
                .is_some_and(|n| (1..=MAX_LOG_TAIL_BYTES as u64).contains(&n)),
            "lane" => value
                .as_str()
                .is_some_and(|s| ["new", "ready", "in_progress", "done"].contains(&s)),
            "failed_logs" => value.is_boolean(),
            "state" => value.as_str().is_some_and(|s| {
                if resource == Resource::TrackerTickets {
                    ["open", "closed", "all"].contains(&s)
                } else {
                    ["open", "resolved", "dismissed"].contains(&s)
                }
            }),
            _ => value.as_str().is_some_and(|s| s.chars().count() <= 256),
        };
        if !defaults.contains_key(key) || !valid {
            return Err(ServiceError::InvalidParameters);
        }
        defaults.insert(key.clone(), value.clone());
    }
    if resource == Resource::DependencyAlerts
        && result["id"] != ""
        && (given.len() != 1 || !continuation.is_empty())
    {
        return Err(ServiceError::InvalidParameters);
    }
    Ok(result)
}
