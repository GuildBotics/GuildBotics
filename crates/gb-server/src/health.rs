//! Recorded AI outcomes and the device's unresolved system alerts.
use crate::api::{ApiError, Body, Shared};
use axum::{
    Json,
    extract::State,
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use gb_observability::{
    Diagnostics,
    health::{SystemAlerts, text},
};
use serde::Deserialize;
use serde_json::{Value, json};

pub(crate) async fn turns(State(api): State<Shared>) -> Json<Value> {
    Json(
        api.selected()
            .workspace
            .as_deref()
            .map(|w| Diagnostics::open(w).last_cli_agent_turns())
            .unwrap_or(json!([])),
    )
}
pub(crate) async fn failures(State(api): State<Shared>) -> Json<Value> {
    Json(
        api.selected()
            .workspace
            .as_deref()
            .map(|w| Diagnostics::open(w).inference_failures())
            .unwrap_or(json!({"llm":{},"jev":null})),
    )
}
pub(crate) fn environment_problems(view: &Value) -> Vec<Value> {
    let mut problems = Vec::new();
    if !text(view, "problem_setting").is_empty() {
        problems.push(json!({"person_id":"","slot":"","setting":view["problem_setting"],"reason":view["problem"]}));
    }
    if !text(view, "warning").is_empty() {
        problems.push(
            json!({"person_id":"","slot":"","setting":"image_differs","reason":view["warning"]}),
        );
    }
    let members: Vec<_> = view["members"].as_array().into_iter().flatten().collect();
    let used: std::collections::BTreeSet<_> = members
        .iter()
        .flat_map(|m| {
            m["slots"]
                .as_array()
                .into_iter()
                .flatten()
                .map(|s| text(s, "tool"))
        })
        .collect();
    for tool in view["tools"].as_array().into_iter().flatten() {
        if !text(tool, "problem").is_empty()
            && (used.contains(text(tool, "name")) || tool["usage_check"]["status"] == "failed")
        {
            problems.push(json!({"person_id":"","slot":tool["name"],"setting":"tool","reason":tool["problem"]}));
        }
    }
    for member in members {
        for slot in member["slots"].as_array().into_iter().flatten() {
            for p in slot["problems"].as_array().into_iter().flatten() {
                problems.push(json!({"person_id":member["person_id"],"slot":slot["slot"],"setting":p["setting"],"reason":p["reason"]}));
            }
        }
    }
    problems
}
async fn list(api: Shared, headers: HeaderMap, dismiss: Option<String>) -> Response {
    let selected = api.selected();
    let Some(workspace) = &selected.workspace else {
        return Json(json!({"alerts":[]})).into_response();
    };
    let language = crate::i18n::language(
        headers
            .get("x-guildbotics-language")
            .and_then(|v| v.to_str().ok())
            .unwrap_or(""),
    );
    let host = gb_environment::Host {
        data: api.data.clone(),
        home: api.home.clone(),
        package: gb_base::home::python_package(),
        identity_root: api.identity_root.clone(),
    };
    let problems =
        crate::environment::status_with(&host, workspace, language, api.keychain.as_ref(), true)
            .await
            .map(|v| environment_problems(&v))
            .unwrap_or_default();
    let runtime = selected.service.view().await;
    let store = Diagnostics::open(workspace);
    let _guard = api.health_lock.lock().await;
    Json(SystemAlerts::list(
        &store,
        &runtime,
        &problems,
        dismiss.as_deref(),
    ))
    .into_response()
}
pub(crate) async fn alerts(State(api): State<Shared>, headers: HeaderMap) -> Response {
    list(api, headers, None).await
}
#[derive(Deserialize)]
pub(crate) struct Dismiss {
    alert_id: String,
}
pub(crate) async fn dismiss(
    State(api): State<Shared>,
    headers: HeaderMap,
    body: Result<Body<Dismiss>, ApiError>,
) -> Response {
    match body {
        Ok(Body(body)) if !body.alert_id.is_empty() => {
            list(api, headers, Some(body.alert_id)).await
        }
        _ => ApiError::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            "validation_error",
            &headers,
        )
        .into_response(),
    }
}
