//! Branvil's Markdown references, shared by the GUI and upstream publishing.
//! Only prose is rewritten: code, links, images, and HTML are left as written.

use std::ops::Range;

use pulldown_cmark::{Event, Options, Parser, Tag, TagEnd};

/// What a reference names.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum Kind {
    Ticket,
    Pull,
}

/// One of Branvil's references: `#3`, `!3`, `<workspace>/<name>#3`,
/// `<workspace>/<name>!3`, or a Branvil URL of a ticket or a pull request.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Reference {
    pub kind: Kind,
    /// The repository path when written (`None`: the pull request's own).
    pub repository: Option<String>,
    pub number: u64,
    /// Written as a URL.
    pub url: bool,
}

/// The byte ranges of `markdown` that are prose: text outside code spans,
/// code blocks, links, images, and HTML. Adjacent text is merged.
pub fn prose(markdown: &str) -> Vec<Range<usize>> {
    let mut out: Vec<Range<usize>> = Vec::new();
    let mut inside = 0usize;
    let mut html = Vec::new();
    let options =
        Options::ENABLE_TABLES | Options::ENABLE_STRIKETHROUGH | Options::ENABLE_TASKLISTS;
    for (event, range) in Parser::new_ext(markdown, options).into_offset_iter() {
        // Inline HTML belongs to one Markdown block. An unclosed tag (even
        // Vec<String>) must not suppress references in later blocks. Use
        // every block boundary, including tight-list items and table cells;
        // inline formatting, links, and line breaks keep their HTML scope.
        let boundary = match &event {
            Event::Start(tag) => Some(tag.to_end()),
            Event::End(tag) => Some(*tag),
            _ => None,
        };
        if matches!(&event, Event::Rule)
            || boundary.is_some_and(|tag| {
                !matches!(
                    tag,
                    TagEnd::Emphasis
                        | TagEnd::Strong
                        | TagEnd::Strikethrough
                        | TagEnd::Link
                        | TagEnd::Image
                )
            })
        {
            html.clear();
        }
        match event {
            Event::Start(
                Tag::CodeBlock(_) | Tag::Link { .. } | Tag::Image { .. } | Tag::HtmlBlock,
            ) => inside += 1,
            Event::End(TagEnd::CodeBlock | TagEnd::Link | TagEnd::Image | TagEnd::HtmlBlock) => {
                inside = inside.saturating_sub(1)
            }
            Event::InlineHtml(tag) if inside == 0 => html_tag(&tag, &mut html),
            Event::Text(_) if inside == 0 && html.is_empty() => match out.last_mut() {
                Some(last) if last.end == range.start => last.end = range.end,
                _ => out.push(range),
            },
            _ => {}
        }
    }
    out
}

/// Inline HTML has no Start/End events. Keep its text out of prose too;
/// void and self-closing tags must not hide the prose that follows them.
fn html_tag(tag: &str, stack: &mut Vec<String>) {
    let Some(tag) = tag.strip_prefix('<') else {
        return;
    };
    let closing = tag.starts_with('/');
    let tag = tag.strip_prefix('/').unwrap_or(tag);
    let name: String = tag
        .chars()
        .take_while(|c| c.is_ascii_alphanumeric() || *c == '-')
        .collect();
    if name.is_empty() {
        return;
    }
    let name = name.to_ascii_lowercase();
    if closing {
        if let Some(i) = stack.iter().rposition(|x| *x == name) {
            stack.truncate(i);
        }
    } else if !tag.trim_end().ends_with("/>")
        && !matches!(
            name.as_str(),
            "area"
                | "base"
                | "br"
                | "col"
                | "embed"
                | "hr"
                | "img"
                | "input"
                | "link"
                | "meta"
                | "param"
                | "source"
                | "track"
                | "wbr"
        )
    {
        stack.push(name);
    }
}

fn name_char(c: u8) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, b'_' | b'.' | b'-')
}

/// A name (`[A-Za-z0-9_.-]+`) at `i`: its end.
fn name_at(s: &[u8], i: usize) -> Option<usize> {
    let end = i + s[i..].iter().take_while(|c| name_char(**c)).count();
    (end > i).then_some(end)
}

/// Digits at `i` that end at a word boundary: `(number, end)`.
fn number_at(s: &[u8], i: usize) -> Option<(u64, usize)> {
    let end = i + s[i..].iter().take_while(|c| c.is_ascii_digit()).count();
    if end == i
        || s.get(end)
            .is_some_and(|c| c.is_ascii_alphanumeric() || *c == b'_')
    {
        return None;
    }
    let n = std::str::from_utf8(&s[i..end]).ok()?.parse().ok()?;
    (n > 0).then_some((n, end))
}

fn kind_of(c: u8) -> Option<Kind> {
    match c {
        b'#' => Some(Kind::Ticket),
        b'!' => Some(Kind::Pull),
        _ => None,
    }
}

/// The reference starting at `i` and its end.
fn reference_at(s: &[u8], i: usize, base_url: &str) -> Option<(Reference, usize)> {
    if !name_char(s[i]) && kind_of(s[i]).is_none() {
        return None;
    }
    // An escaped sigil deliberately stays literal. In particular, inserting
    // an HTML link after its backslash would change how Markdown is parsed.
    if s[..i].iter().rev().take_while(|c| **c == b'\\').count() % 2 == 1 {
        return None;
    }
    let starts_word = i == 0 || !(name_char(s[i - 1]) || s[i - 1] == b'/');
    if !starts_word {
        return None;
    }
    // A Branvil URL: {base}/{workspace}/{name}/-/tickets|pulls/{n}
    if s[i..].starts_with(format!("{base_url}/").as_bytes()) {
        let at = i + base_url.len() + 1;
        let ws = name_at(s, at)?;
        let name = name_at(s, ws.checked_add(1).filter(|_| s.get(ws) == Some(&b'/'))?)?;
        let tail = &s[name..];
        let (kind, skip) = if tail.starts_with(b"/-/tickets/") {
            (Kind::Ticket, 11)
        } else if tail.starts_with(b"/-/pulls/") {
            (Kind::Pull, 9)
        } else {
            return None;
        };
        let (number, end) = number_at(s, name + skip)?;
        let repository = std::str::from_utf8(&s[at..name]).ok()?.to_string();
        return Some((
            Reference {
                kind,
                repository: Some(repository),
                number,
                url: true,
            },
            end,
        ));
    }
    // `#3` / `!3`
    if let Some(kind) = kind_of(s[i]) {
        let (number, end) = number_at(s, i + 1)?;
        return Some((
            Reference {
                kind,
                repository: None,
                number,
                url: false,
            },
            end,
        ));
    }
    // `<workspace>/<name>#3` / `<workspace>/<name>!3`
    let ws = name_at(s, i)?;
    if s.get(ws) != Some(&b'/') {
        return None;
    }
    let name = name_at(s, ws + 1)?;
    let kind = kind_of(*s.get(name)?)?;
    let (number, end) = number_at(s, name + 1)?;
    Some((
        Reference {
            kind,
            repository: Some(std::str::from_utf8(&s[i..name]).ok()?.to_string()),
            number,
            url: false,
        },
        end,
    ))
}

/// Every reference in the prose of `markdown`, with its byte range.
pub fn find(markdown: &str, base_url: &str) -> Vec<(Range<usize>, Reference)> {
    let s = markdown.as_bytes();
    let mut out = Vec::new();
    for range in prose(markdown) {
        let mut i = range.start;
        while i < range.end {
            match reference_at(&s[..range.end], i, base_url) {
                Some((r, end)) => {
                    out.push((i..end, r));
                    i = end;
                }
                None => i += 1,
            }
        }
    }
    out
}

/// `markdown` with each reference that `replace` answers replaced.
pub fn rewrite(
    markdown: &str,
    base_url: &str,
    replace: impl Fn(&Reference) -> Option<String>,
) -> String {
    let mut out = String::with_capacity(markdown.len());
    let mut at = 0;
    for (range, r) in find(markdown, base_url) {
        if let Some(text) = replace(&r) {
            out.push_str(&markdown[at..range.start]);
            out.push_str(&text);
            at = range.end;
        }
    }
    out.push_str(&markdown[at..]);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    const BASE: &str = "http://127.0.0.1:7878";

    #[test]
    fn shared_display_and_publishing_examples() {
        let examples: serde_json::Value =
            serde_json::from_str(include_str!("../tests/fixtures/markdown-references.json"))
                .unwrap();
        for case in examples["cases"].as_array().unwrap() {
            assert_eq!(
                serde_json::json!(names(case["source"].as_str().unwrap())),
                case["found"],
                "{}",
                case["name"]
            );
        }
    }

    fn names(markdown: &str) -> Vec<String> {
        find(markdown, BASE)
            .into_iter()
            .map(|(range, _)| markdown[range].to_string())
            .collect()
    }

    #[test]
    fn references_are_found_in_prose_only() {
        let md = "Fixes #2 and acme/other#4, see !3.\n\n\
                  `#5` stays, [#6](http://x) stays, <b>#7</b> stays.\n\n\
                  ```\n#8 in a block\n```\n\n    #9 indented\n\n\
                  - item #10\n> quoted #11\n\n# Heading #12\n";
        assert_eq!(names(md), ["#2", "acme/other#4", "!3", "#10", "#11", "#12"]);
    }

    #[test]
    fn only_whole_references_count() {
        assert_eq!(
            names("a#2 #0 #2a abc!1 x_#3 issue#4 GH-5 v1.0#6"),
            Vec::<String>::new()
        );
        assert_eq!(names("(#2) [x] #3, #4."), ["#2", "#3", "#4"]);
    }

    #[test]
    fn branvil_urls_are_references() {
        let md = "See http://127.0.0.1:7878/acme/demo/-/tickets/3 and \
                  http://127.0.0.1:7878/acme/demo/-/pulls/4, not \
                  http://127.0.0.1:7878/acme/demo/-/jobs/5 or https://github.com/a/b/issues/6.";
        let found = find(md, BASE);
        assert_eq!(found.len(), 2);
        assert_eq!(
            found[0].1,
            Reference {
                kind: Kind::Ticket,
                repository: Some("acme/demo".into()),
                number: 3,
                url: true
            }
        );
        assert_eq!(found[1].1.kind, Kind::Pull);
    }

    #[test]
    fn rewriting_replaces_what_is_answered_and_keeps_the_rest() {
        let md = "Fixes #2 and #3 (`#2`).";
        let out = rewrite(md, BASE, |r| (r.number == 2).then(|| "#7".to_string()));
        assert_eq!(out, "Fixes #7 and #3 (`#2`).");
    }
}
