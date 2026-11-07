//! Completed replies replace deltas; interrupted replies retain partial text.
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};

pub(crate) fn collapse(mut records: Vec<Value>) -> Vec<Value> {
    let mut pending: BTreeMap<String, Vec<usize>> = BTreeMap::new();
    let mut thinking: BTreeMap<String, Vec<usize>> = BTreeMap::new();
    let mut dropped = BTreeSet::new();
    for (i, r) in records.iter().enumerate() {
        if r["type"] != "agent_runtime.assistant" {
            continue;
        }
        let stream = r["span_id"].as_str().unwrap_or("").to_string();
        match r["payload"]["name"].as_str().unwrap_or("") {
            "started" | "delta" => pending.entry(stream).or_default().push(i),
            "thinking" => thinking.entry(stream).or_default().push(i),
            "completed" => dropped.extend(pending.remove(&stream).unwrap_or_default()),
            _ => (),
        }
    }
    for indexes in thinking.values() {
        let mut start = 0;
        for end in 1..=indexes.len() {
            if end == indexes.len() || indexes[end] != indexes[end - 1] + 1 {
                let run = &indexes[start..end];
                if run.len() >= 2 {
                    merge(&mut records, run, "thinking", &mut dropped);
                }
                start = end;
            }
        }
    }
    for indexes in pending.values() {
        let deltas: Vec<_> = indexes
            .iter()
            .copied()
            .filter(|i| records[*i]["payload"]["name"] == "delta")
            .collect();
        if !deltas.is_empty() {
            merge(&mut records, &deltas, "partial", &mut dropped);
            let anchor = *deltas.last().unwrap();
            dropped.extend(indexes.iter().copied().filter(|i| *i != anchor));
        }
    }
    records
        .into_iter()
        .enumerate()
        .filter_map(|(i, r)| (!dropped.contains(&i)).then_some(r))
        .collect()
}
fn merge(records: &mut [Value], indexes: &[usize], name: &str, dropped: &mut BTreeSet<usize>) {
    let anchor = *indexes.last().unwrap();
    let message: String = indexes
        .iter()
        .flat_map(|i| {
            records[*i]["payload"]["message"]
                .as_str()
                .unwrap_or("")
                .chars()
        })
        .take(8192)
        .collect();
    records[anchor]["payload"] = json!({"name":name,"message":message});
    if name == "partial" {
        records[anchor]["payload"]["partial"] = true.into();
    }
    dropped.extend(indexes.iter().copied().filter(|i| *i != anchor));
}

#[cfg(test)]
mod tests {
    #[test]
    fn every_frozen_stream_case_preserves_partial_caps_span_boundaries_and_reasoning_runs() {
        let cases: Vec<serde_json::Value> = serde_json::from_str(include_str!(
            "../../../records/golden/activity-streams.json"
        ))
        .unwrap();
        assert_eq!(cases.len(), 13);
        for case in cases {
            assert_eq!(
                super::collapse(case["arguments"]["records"].as_array().unwrap().clone()),
                case["expected"].as_array().unwrap().clone(),
                "{}",
                case["test"]
            );
        }
    }
    #[test]
    fn completed_interrupted_and_thinking_streams_match_python() {
        let fixture: serde_json::Value =
            serde_json::from_str(include_str!("../../../records/golden/shared-records.json"))
                .unwrap();
        for case in fixture["streams"].as_array().unwrap() {
            assert_eq!(
                super::collapse(case["raw"].as_array().unwrap().clone()),
                case["expected"].as_array().unwrap().clone()
            );
        }
    }
}
