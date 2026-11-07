//! Activity sessions are shared lifecycles, enriched by their fact records.
use chrono::{DateTime, Datelike, FixedOffset, Timelike};
use serde_json::{Value, json};

pub fn lifecycle_from_run(r: &crate::runs::RunRecord) -> Value {
    json!({"trace_id":r.run_id,"person_id":r.member_id,"source":r.source,"command":r.work_kind,"status":r.status,"started_at":r.started_at,"ended_at":r.finished_at.as_deref().unwrap_or(""),"attributes":r.attributes,"summary":if r.result.is_some(){r.safe_summary.as_str()}else{""},"has_evidence":r.result.is_some()||!r.provider_evidence.is_empty()})
}
pub fn lifecycle_from_session(r: &Value) -> Value {
    json!({"trace_id":text(r,"trace_id"),"person_id":text(r,"person_id"),"source":"interactive","command":text(r,"command"),"status":text(r,"status"),"started_at":text(r,"started_at"),"ended_at":text(r,"last_seen_at"),"attributes":r.get("attributes").filter(|v|v.is_object()).cloned().unwrap_or(json!({})),"summary":"","has_evidence":true})
}

fn text<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}
fn first(values: &[&Value]) -> String {
    values
        .iter()
        .find_map(|v| v.as_str().filter(|s| !s.is_empty()))
        .unwrap_or("")
        .into()
}
fn time(v: &Value, key: &str) -> Option<DateTime<FixedOffset>> {
    parse_timestamp(text(v, key))
}
pub fn parse_timestamp(value: &str) -> Option<DateTime<FixedOffset>> {
    DateTime::parse_from_rfc3339(value)
        .ok()
        .or_else(|| {
            chrono::NaiveDateTime::parse_from_str(&value.replace(' ', "T"), "%Y-%m-%dT%H:%M:%S%.f")
                .ok()
                .map(|v| v.and_utc().fixed_offset())
        })
        .or_else(|| {
            chrono::NaiveDate::parse_from_str(value, "%Y-%m-%d")
                .ok()
                .map(|v| v.and_hms_opt(0, 0, 0).unwrap().and_utc().fixed_offset())
        })
        .filter(|v| v.nanosecond() < 1_000_000_000 && (1..=9999).contains(&v.year()))
        .map(|v| v.with_nanosecond(v.nanosecond() / 1000 * 1000).unwrap())
}
pub fn iso_timestamp(value: DateTime<FixedOffset>) -> String {
    let mut result = value.format("%Y-%m-%dT%H:%M:%S").to_string();
    let micros = value.timestamp_subsec_micros();
    if micros != 0 {
        result.push_str(&format!(".{micros:06}"));
    }
    result.push_str(&value.format("%:z").to_string());
    result
}
fn link(kind: &str, label: String, url: String, timestamp: &str) -> Value {
    json!({"kind":kind,"label":label,"url":url,"timestamp":timestamp})
}
pub fn dedupe(links: Vec<Value>) -> Vec<Value> {
    let mut result: Vec<Value> = vec![];
    let specific = |v: &Value| matches!(text(v, "kind"), "ticket" | "pull_request" | "commit");
    let key = |v: &Value| {
        if text(v, "url").is_empty() {
            format!("{}:label:{}", text(v, "kind"), text(v, "label"))
        } else {
            text(v, "url").into()
        }
    };
    for item in links {
        if let Some(existing) = result.iter_mut().find(|v| key(v) == key(&item)) {
            if !specific(existing) && specific(&item) {
                *existing = item;
            }
        } else {
            result.push(item);
        }
    }
    result
}
fn attribute_links(a: &Value, stamp: &str) -> Vec<Value> {
    let url = text(a, "target.url");
    let label = text(a, "target.label");
    if url.is_empty() && label.is_empty() {
        return vec![];
    }
    let kind = match text(a, "target.kind") {
        "ticket" => "ticket",
        "pull_request" => "pull_request",
        _ => "external",
    };
    let label = if label.is_empty() {
        url.into()
    } else {
        crate::target_reference_label(text(a, "target.kind"), label)
    };
    vec![link(kind, label, url.into(), stamp)]
}
pub fn links(record: &Value) -> Vec<Value> {
    let a = &record["attributes"];
    if crate::is_read_only_action(a) {
        return vec![];
    }
    let p = &record["payload"];
    let stamp = text(record, "timestamp");
    let mut result = attribute_links(a, stamp);
    let sources: Vec<_> = p["source"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|v| v.is_object())
        .collect();
    for source in &sources {
        let url = text(source, "url");
        if !url.is_empty() {
            let title = if sources.len() == 1 {
                text(p, "title")
            } else {
                ""
            };
            result.push(link(
                "external",
                if title.is_empty() {
                    url.into()
                } else {
                    title.into()
                },
                url.into(),
                stamp,
            ));
        }
    }
    let label = first(&[&p["title"], &a["memory.doc_id"], &a["memory.path"]]);
    if !label.is_empty() {
        let mut query = url::form_urlencoded::Serializer::new(String::new());
        for (key, value) in [
            ("tab", "memory"),
            ("doc_id", text(a, "memory.doc_id")),
            ("memory_trace_id", text(record, "trace_id")),
            ("timestamp", stamp),
            ("action", text(a, "memory.action")),
            ("person_id", text(record, "person_id")),
        ] {
            if !value.is_empty() {
                query.append_pair(key, value);
            }
        }
        result.push(link(
            "doc",
            label,
            format!("/diagnostics?{}", query.finish()),
            stamp,
        ));
    }
    for c in p["commits"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|v| v.is_object())
    {
        let url = first(&[&c["url"], &c["html_url"]]);
        let sha = first(&[&c["id"], &c["sha"]]);
        if url.is_empty() && sha.is_empty() {
            continue;
        }
        let message = text(c, "message").trim().lines().next().unwrap_or("");
        let label = if !message.is_empty() {
            message.into()
        } else if !sha.is_empty() {
            sha.chars().take(7).collect()
        } else {
            url.clone()
        };
        result.push(link("commit", label, url, stamp));
    }
    dedupe(result)
}

/// Lifecycle objects use the display vocabulary, not a device's command boundaries.
pub fn build(
    members: Vec<Value>,
    lifecycles: Vec<Value>,
    mut records: Vec<Value>,
    detail: impl Fn(&str) -> bool,
    sentence: impl Fn(&str, &[(&str, &str)]) -> String,
) -> Value {
    let members: Vec<_> = members
        .into_iter()
        .filter(|m| m["person_type"] != "human")
        .collect();
    let admitted = |person: &str| members.iter().any(|m| text(m, "person_id") == person);
    records.sort_by_key(|r| time(r, "timestamp"));
    let mut sessions = vec![];
    for lifecycle in lifecycles {
        let id = text(&lifecycle, "trace_id");
        if id.is_empty()
            || !admitted(text(&lifecycle, "person_id"))
            || lifecycle["source"] == "manual"
        {
            continue;
        }
        let Some(start) = time(&lifecycle, "started_at") else {
            continue;
        };
        let facts: Vec<_> = records
            .iter()
            .filter(|r| r["trace_id"] == id)
            .cloned()
            .collect();
        let end = time(&lifecycle, "ended_at").unwrap_or_else(|| {
            facts
                .iter()
                .filter_map(|r| time(r, "timestamp"))
                .fold(start, std::cmp::max)
        });
        let mut described = vec![json!({"attributes":lifecycle["attributes"]})];
        described.extend(facts.clone());
        let attrs = crate::first_seen_attributes(&described);
        let worked: Vec<_> = described
            .iter()
            .filter(|r| !crate::is_read_only_action(&r["attributes"]))
            .cloned()
            .collect();
        let mut session_links =
            attribute_links(&Value::Object(crate::first_seen_attributes(&worked)), "");
        session_links.extend(worked.iter().flat_map(links));
        let session_links = dedupe(session_links);
        let mut status = crate::TraceStatus::default();
        status.observe(text(&lifecycle, "status"));
        for r in &facts {
            status.add(r);
        }
        let rate=facts.iter().rev().find(|r|r["type"]=="workflow.rate_limited").map(|r|json!({"retry_after_at":first(&[&r["attributes"]["rate_limit.retry_after_at"],&r["payload"]["retry_after_at"]]),"retry_after_text":first(&[&r["attributes"]["rate_limit.retry_after_text"],&r["payload"]["retry_after_text"]])}));
        let interactive = lifecycle["source"] == "interactive";
        if !interactive
            && matches!(status.resolve(), "success" | "running" | "info")
            && rate.is_none()
            && lifecycle["has_evidence"] != true
            && session_links.is_empty()
            && ![
                "target.label",
                "target.url",
                "target.kind",
                "memory.doc_id",
                "memory.path",
                "memory.title",
                "memory.action",
            ]
            .iter()
            .any(|k| {
                attrs
                    .get(*k)
                    .is_some_and(|v| !v.is_null() && !v.as_str().unwrap_or("").trim().is_empty())
            })
            && !facts.iter().any(|r| r["kind"] == "memory")
        {
            continue;
        }
        let title = crate::resolve_trace_title(
            &facts,
            &attrs,
            text(&lifecycle, "command"),
            "",
            id,
            || text(&lifecycle, "summary").into(),
            |provider| {
                sentence(
                    "observability.trace_title.chat_trigger",
                    &[(
                        "provider",
                        match provider {
                            "slack" => "Slack",
                            "local" => "Local",
                            "branvil" => "Branvil",
                            p => p,
                        },
                    )],
                )
            },
        );
        // Python's datetime year range fits in an i64 count of microseconds.
        let duration = (end - start)
            .num_microseconds()
            .expect("bounded datetime range");
        sessions.push(json!({"trace_id":id,"person_id":lifecycle["person_id"],"source":lifecycle["source"],"command":lifecycle["command"],"workflow":"","title":title,"mode":if interactive {"interactive"} else {"workflow"},"status":status.resolve(),"started_at":iso_timestamp(start),"ended_at":iso_timestamp(end),"duration_seconds":(duration as f64/1_000_000.0).max(0.0),"links":session_links,"rate_limit":rate,"detail_available":detail(id)}));
    }
    sessions.sort_by_key(|r| time(r, "started_at"));
    let mut events = vec![];
    let mut seen = std::collections::BTreeSet::new();
    for (index, r) in records.iter().enumerate() {
        if time(r, "timestamp").is_none()
            || (!text(r, "person_id").is_empty() && !admitted(text(r, "person_id")))
        {
            continue;
        }
        let Some(kind) = classify(r) else {
            continue;
        };
        let p = &r["payload"];
        let a = &r["attributes"];
        let mut linked = r.clone();
        if !linked["attributes"].is_object() {
            linked["attributes"] = json!({});
        }
        for (object, kind) in [("pull_request", "pull_request"), ("ticket", "ticket")] {
            if p[object].is_object() {
                linked["attributes"]["target.kind"] = kind.into();
                for key in ["label", "url"] {
                    if !text(&p[object], key).is_empty() {
                        linked["attributes"][format!("target.{key}")] = p[object][key].clone();
                    }
                }
                break;
            }
        }
        let event_links = links(&linked);
        let mut url = first(&[
            &a["target.url"],
            &p["pull_request"]["url"],
            &p["ticket"]["url"],
            &p["html_url"],
            &p["compare"],
        ]);
        if url.is_empty() {
            url = event_links
                .first()
                .map(|l| text(l, "url"))
                .unwrap_or("")
                .into();
        }
        let trace = if text(r, "trace_id").is_empty() {
            "global"
        } else {
            text(r, "trace_id")
        };
        let id = format!(
            "{trace}:{}:{kind}:{}",
            text(r, "timestamp"),
            if url.is_empty() {
                index.to_string()
            } else {
                url.clone()
            }
        );
        if !seen.insert(id.clone()) {
            continue;
        }
        let label = first(&[
            &a["target.label"],
            &p["pull_request"]["label"],
            &p["ticket"]["label"],
        ]);
        let title = if kind == "push" {
            let commits: Vec<_> = p["commits"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|v| v.is_object())
                .collect();
            match commits.len() {
                0 => sentence("app_api.activity_events.push", &[]),
                1 => {
                    let m = text(commits[0], "message")
                        .trim()
                        .lines()
                        .next()
                        .unwrap_or("");
                    if m.is_empty() {
                        sentence("app_api.activity_events.push_one", &[])
                    } else {
                        m.into()
                    }
                }
                n => sentence(
                    "app_api.activity_events.push_many",
                    &[("count", &n.to_string())],
                ),
            }
        } else {
            let prefix = if kind.starts_with("pr_") {
                sentence("app_api.activity_events.pr", &[])
            } else {
                sentence("app_api.activity_events.ticket", &[])
            };
            let key = match kind {
                "pr_create" | "issue_create" => "app_api.activity_events.created",
                "pr_merge" => "app_api.activity_events.merged",
                "pr_closed" => "app_api.activity_events.closed",
                "pr_draft" => "app_api.activity_events.review_limit",
                _ => "app_api.activity_events.resolved",
            };
            let suffix = sentence(key, &[]);
            [prefix.as_str(), label.as_str(), suffix.as_str()]
                .into_iter()
                .filter(|s| !s.is_empty())
                .collect::<Vec<_>>()
                .join(" ")
        };
        let detail = first(&[
            &p["title"],
            &p["pull_request"]["title"],
            &p["ticket"]["title"],
            &p["ref"],
            &r["message"],
            &r["type"],
        ]);
        events.push(json!({"id":id,"timestamp":r["timestamp"],"person_id":text(r,"person_id"),"type":kind,"title":title,"detail":detail,"url":url,"links":event_links}));
    }
    json!({"members":members,"sessions":sessions,"events":events,"unsupported_event_sources":[]})
}
fn classify(r: &Value) -> Option<&'static str> {
    let event = text(r, "type");
    let normalized = event.to_lowercase().replace('-', "_");
    let p = &r["payload"];
    let action = first(&[&p["action"], &r["attributes"]["target.action"]]).to_lowercase();
    let target = text(&r["attributes"], "target.kind").to_lowercase();
    if event == "pull_request.review_limit" {
        return Some("pr_draft");
    }
    if normalized.contains("push") || p.get("commits").is_some() || action == "push" {
        return Some("push");
    }
    if normalized.contains("merge") || p["pull_request"]["merged"] == true {
        return Some("pr_merge");
    }
    if !p["pull_request"].is_null()
        || target == "pull_request"
        || normalized.contains("pull_request")
    {
        if matches!(action.as_str(), "opened" | "created" | "ready_for_review")
            || normalized.contains("create")
        {
            return Some("pr_create");
        }
        if action == "closed" || normalized.contains("closed") {
            return Some("pr_closed");
        }
    }
    if !p["ticket"].is_null() || target == "ticket" || normalized.contains("ticket") {
        if matches!(action.as_str(), "opened" | "created") || normalized.contains("create") {
            return Some("issue_create");
        }
        if matches!(action.as_str(), "closed" | "resolved") || normalized.contains("resolve") {
            return Some("issue_resolve");
        }
    }
    None
}
