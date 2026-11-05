//! Human-readable member output is shared by the backend and CLI client.
use serde_json::Value;
pub fn markdown(payload: &Value, render: impl Fn(&str) -> String) -> String {
    let Some(values) = payload.as_object() else {
        return payload.to_string();
    };
    let mut lines = vec![];
    for (key, value) in values {
        if key == "communication_style" && value.is_object() {
            lines.push(format!(
                "## {}",
                render("cli.member.output.communication_style")
            ));
            for key in [
                "active_member_instruction",
                "voice_basis",
                "interactive_replies",
                "comments",
                "neutral_documents",
                "machine_outputs",
            ] {
                if let Some(value) = value.get(key) {
                    lines.push(format!("- **{key}**: {}", value.as_str().unwrap_or("")));
                }
            }
            continue;
        }
        if key == "capabilities" && value.is_string() {
            lines.push(format!("## {}", render("cli.member.output.capabilities")));
            lines.push(value.as_str().unwrap().into());
            continue;
        }
        if key == "failed_logs"
            && let Some(logs) = value.as_array()
        {
            lines.push(format!("## {}", render("cli.member.output.failed_logs")));
            if logs.is_empty() {
                lines.push(format!("_{}_", render("cli.member.output.no_failed_logs")));
                continue;
            }
            for item in logs.iter().filter(|v| v.is_object()) {
                let name = item["name"]
                    .as_str()
                    .filter(|v| !v.is_empty())
                    .map(str::to_owned)
                    .unwrap_or_else(|| render("cli.member.output.unnamed_job"));
                let conclusion = item["conclusion"]
                    .as_str()
                    .filter(|v| !v.is_empty())
                    .map(str::to_owned)
                    .unwrap_or_else(|| render("cli.member.output.unknown"));
                lines.push(format!("### {name} ({conclusion})"));
                let mut metadata = item.clone();
                metadata.as_object_mut().unwrap().remove("log");
                lines.push(metadata.to_string());
                let log = item["log"].as_str().unwrap_or("");
                let (mut ticks, mut longest) = (0, 0);
                for c in log.chars() {
                    if c == '`' {
                        ticks += 1;
                        longest = longest.max(ticks);
                    } else {
                        ticks = 0;
                    }
                }
                let fence = "`".repeat(3.max(longest + 1));
                lines.extend([
                    format!("{fence}text"),
                    log.trim_end_matches('\n').into(),
                    fence,
                ]);
            }
            continue;
        }
        lines.push(format!(
            "- **{key}**: {}",
            value
                .as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| value.to_string())
        ));
    }
    lines.join("\n")
}
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn render(key: &str) -> String {
        match key {
            "cli.member.output.communication_style" => "Communication Style",
            "cli.member.output.capabilities" => "Member Capabilities",
            "cli.member.output.failed_logs" => "Failed job logs",
            _ => "unknown",
        }
        .into()
    }
    #[test]
    fn failed_logs_are_multiline_with_a_fence_longer_than_embedded_backticks() {
        let value = json!({"failed_logs":[{"name":"test","conclusion":"failure","run_id":9,"log":"first\nsecond\n"},{"name":"ticks","conclusion":"failure","log":"```\nraw\n"}]});
        let output = markdown(&value, render);
        assert!(output.contains("## Failed job logs"));
        assert!(output.contains("### test (failure)"));
        assert!(output.contains("```text\nfirst\nsecond\n```"));
        assert!(!output.contains("first\\nsecond"));
        assert!(output.contains("````text\n```\nraw\n````"));
    }
    #[test]
    fn communication_style_and_capabilities_remain_readable_sections() {
        let output = markdown(
            &json!({"communication_style":{"comments":"clear","machine_outputs":"valid"},"capabilities":"### Rules\nkeep context"}),
            render,
        );
        assert!(output.contains(
            "## Communication Style\n- **comments**: clear\n- **machine_outputs**: valid"
        ));
        assert!(output.contains("## Member Capabilities\n### Rules\nkeep context"));
    }
}
