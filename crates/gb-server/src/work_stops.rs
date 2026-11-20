//! A stopped ticket's notice, separate from the execution that stopped.
use branvil::agent::port::Task;
use gb_member::context::PublicMember;
use gb_observability::runs::{Completion, RunRecord, RunState};
use serde_json::Value;

#[derive(Clone, Debug, PartialEq)]
pub struct Subscription {
    pub channel_id: String,
    pub channel_name: String,
    pub participation: String,
    pub startup_minutes: u32,
    pub interval_seconds: f64,
    pub overlap_seconds: f64,
    pub limit: usize,
}

pub fn subscriptions(member: &Value) -> Vec<Subscription> {
    member["message_channels"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|channel| {
            let config = channel["chat"]
                .as_object()
                .filter(|v| !v.is_empty())
                .or_else(|| channel["channel_info"]["chat"].as_object())?;
            if config.get("enabled").and_then(Value::as_bool) == Some(false) {
                return None;
            }
            let text = |key: &str| config.get(key).and_then(Value::as_str).unwrap_or("").trim();
            let channel_id = if text("channel_id").is_empty() {
                channel["channel_info"]["channel_id"]
                    .as_str()
                    .unwrap_or("")
                    .trim()
            } else {
                text("channel_id")
            };
            let channel_name = if text("channel_name").is_empty() {
                channel["name"].as_str().unwrap_or("").trim()
            } else {
                text("channel_name")
            };
            let participation = text("participation").to_ascii_lowercase();
            let number = |key: &str, default: f64| {
                config
                    .get(key)
                    .and_then(Value::as_f64)
                    .unwrap_or(default)
                    .max(0.0)
            };
            Some(Subscription {
                channel_id: channel_id.into(),
                channel_name: channel_name.into(),
                participation: if ["strict", "social", "muted"].contains(&participation.as_str()) {
                    participation
                } else {
                    "strict".into()
                },
                startup_minutes: number("startup_backfill_minutes", 60.0) as u32,
                interval_seconds: number("backfill_interval_seconds", 300.0),
                overlap_seconds: number("backfill_overlap_seconds", 60.0),
                limit: (number("backfill_limit", 100.0) as usize).max(1),
            })
        })
        .collect()
}

#[derive(Clone, Debug, PartialEq)]
pub struct WorkStop {
    pub reason: &'static str,
    pub run_id: String,
    pub url: String,
    pub label: String,
    pub title: String,
    pub comment_url: String,
}

impl WorkStop {
    pub fn of(task: &Task, reason: &'static str, run_id: &str, comment_url: &str) -> Self {
        Self {
            reason,
            run_id: run_id.into(),
            url: task
                .pull_request_url
                .as_ref()
                .or(task.url.as_ref())
                .cloned()
                .unwrap_or_default(),
            label: task.label.clone(),
            title: task.title.clone(),
            comment_url: comment_url.into(),
        }
    }
}

pub fn comment_url(evidence: &[Value], url: &str) -> String {
    let comments = evidence
        .iter()
        .filter(|item| {
            matches!(
                item["type"].as_str(),
                Some(
                    "issue_comment" | "pr_comment" | "pr_review" | "pr_review_comment" | "pr_reply"
                )
            )
        })
        .map(|item| &item["payload"])
        .filter(|payload| payload["url"].as_str().is_some_and(|url| !url.is_empty()))
        .collect::<Vec<_>>();
    comments
        .iter()
        .rev()
        .find(|payload| payload["target"]["url"] == url)
        .or_else(|| comments.last())
        .and_then(|payload| payload["url"].as_str())
        .unwrap_or("")
        .into()
}

pub fn suppressed(record: &RunRecord, roster: &[PublicMember]) -> bool {
    record.finished_at.is_some()
        && record.status != RunState::Running
        && record.result.as_ref().is_some_and(|result| {
            matches!(result.status, Completion::Asking | Completion::Blocked)
                && !result.waiting_for.is_empty()
                && result.waiting_for.iter().all(|id| {
                    roster.iter().any(|member| {
                        member.person_id == *id && member.is_active && member.person_type != "human"
                    })
                })
        })
}

pub fn completed_stop(
    task: &Task,
    record: &RunRecord,
    roster: &[PublicMember],
) -> Option<WorkStop> {
    if record.finished_at.is_none() || record.status == RunState::Running {
        return None;
    }
    let result = record.result.as_ref()?;
    let reason = match result.status {
        Completion::Asking | Completion::Blocked => {
            if suppressed(record, roster) {
                return None;
            }
            if result.status == Completion::Asking {
                "asking"
            } else {
                "blocked"
            }
        }
        _ => return None,
    };
    let mut stop = WorkStop::of(task, reason, &record.run_id, "");
    stop.comment_url = comment_url(&record.provider_evidence, &stop.url);
    Some(stop)
}

pub fn message(person: &PublicMember, stop: &WorkStop, language: &str) -> String {
    let item = [stop.label.as_str(), stop.title.as_str()]
        .into_iter()
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
    let mut body = crate::i18n::t(
        language,
        &format!("capabilities.work_stops.{}", stop.reason),
        &[("name", &person.name), ("item", &item), ("url", &stop.url)],
    );
    if !stop.comment_url.is_empty() {
        body.push('\n');
        body.push_str(&crate::i18n::t(
            language,
            "capabilities.work_stops.comment",
            &[("url", &stop.comment_url)],
        ));
    }
    body
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn person(id: &str, kind: &str, active: bool) -> PublicMember {
        serde_json::from_value(
            json!({"person_id":id,"name":id,"person_type":kind,"is_active":active,
            "roles":{},"profile":{},"speaking_style":"","accounts":{}}),
        )
        .unwrap()
    }

    #[test]
    fn only_completed_waits_exclusively_for_active_agents_suppress_a_stop() {
        let roster = vec![
            person("aiko", "agent", true),
            person("owner", "human", true),
            person("inactive", "agent", false),
        ];
        let task = Task {
            url: Some("ticket".into()),
            ..Task::default()
        };
        for (waiting, expected) in [
            (vec!["aiko"], false),
            (vec!["owner"], true),
            (vec!["inactive"], true),
            (vec!["missing"], true),
            (vec![], true),
            (vec!["aiko", "owner"], true),
        ] {
            let mut record:RunRecord=serde_json::from_value(json!({"run_id":"run","work_kind":"ticket","execution_mode":"autonomous",
                "member_id":"aiko","device_id":"device","started_at":"2026-11-05T00:00:00Z","finished_at":"2026-11-05T00:01:00Z","status":"failed",
                "result":{"subject_type":"ticket","subject_id":"ticket","status":"asking","waiting_for":waiting}})).unwrap();
            assert_eq!(completed_stop(&task, &record, &roster).is_some(), expected);
            if expected {
                assert_eq!(
                    completed_stop(&task, &record, &roster).unwrap().reason,
                    "asking"
                );
            }
            for completion in [
                Completion::Done,
                Completion::Asking,
                Completion::Blocked,
                Completion::AwaitingChecks,
            ] {
                record.result.as_mut().unwrap().status = completion;
                let reason = match completion {
                    Completion::Asking => Some("asking"),
                    Completion::Blocked => Some("blocked"),
                    Completion::Done | Completion::AwaitingChecks => None,
                };
                let stop = completed_stop(&task, &record, &roster);
                if expected && let Some(reason) = reason {
                    assert_eq!(stop, Some(WorkStop::of(&task, reason, "run", "")));
                } else {
                    assert!(stop.is_none());
                }
            }
            record.result = None;
            assert!(completed_stop(&task, &record, &roster).is_none());
            record.finished_at = None;
            assert!(completed_stop(&task, &record, &roster).is_none());
        }
    }

    #[test]
    fn subscriptions_preserve_enabled_participation_and_backfill_policies() {
        let member = json!({"message_channels":[
            {"name":"primary","chat":{"enabled":true,"participation":" SOCIAL ","backfill_limit":20}},
            {"name":"disabled","chat":{"enabled":false}},
            {"name":"muted","channel_info":{"channel_id":"C2","chat":{"participation":"muted"}}},
            {"name":"unknown","chat":{"participation":"invalid"}},
            {"name":"not chat"}
        ]});
        let channels = subscriptions(&member);
        assert_eq!(channels.len(), 3);
        assert_eq!(channels[0].participation, "social");
        assert_eq!(channels[0].channel_name, "primary");
        assert_eq!(channels[0].limit, 20);
        assert_eq!(channels[1].channel_id, "C2");
        assert_eq!(channels[1].participation, "muted");
        assert_eq!(channels[2].participation, "strict");
    }

    #[test]
    fn latest_comment_on_the_subject_precedes_comments_elsewhere() {
        let evidence = vec![
            json!({"type":"issue_comment","payload":{"target":{"url":"ticket"},"url":"comment1"}}),
            json!({"type":"pr_reply","payload":{"target":{"url":"pr"},"url":"comment2"}}),
            json!({"type":"pr_comment","payload":{"target":{"url":"ticket"},"url":"comment3"}}),
        ];
        assert_eq!(comment_url(&evidence, "ticket"), "comment3");
        assert_eq!(comment_url(&evidence, "pr"), "comment2");
        assert_eq!(comment_url(&evidence, "other"), "comment3");
        assert_eq!(comment_url(&[], "ticket"), "");
        for kind in [
            "issue_comment",
            "pr_comment",
            "pr_review",
            "pr_review_comment",
            "pr_reply",
        ] {
            assert_eq!(
                comment_url(
                    &[json!({"type":kind,"payload":{"target":{"url":"ticket"},"url":"linked"}})],
                    "ticket"
                ),
                "linked"
            );
        }
        assert_eq!(
            comment_url(
                &[
                    json!({"type":"pr_create","payload":{"url":"created"}}),
                    json!({"type":"reaction_add","payload":{"url":"reaction"}}),
                    json!({"type":"issue_comment","payload":{"target":{"url":"ticket"}}})
                ],
                "ticket"
            ),
            ""
        );
    }
}
