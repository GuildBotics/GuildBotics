use gb_member::context::{command_summary, help};
#[test]
fn command_summary_comes_from_the_catalog() {
    assert_eq!(
        command_summary("git push").unwrap(),
        "Push the current branch with the member credential and report readiness for matching open PRs."
    );
}
#[test]
fn command_summary_rejects_unknown_command_path() {
    for command in ["git shove", "git", "pr", "chat inspect"] {
        assert!(command_summary(command).is_err());
    }
}
#[test]
fn reference_covers_every_member_domain() {
    let text = help();
    assert!(text.contains("guildbotics member git commit"));
    assert!(text.contains("include_diff=true"));
    assert!(text.contains("guildbotics member pr create"));
    assert!(text.contains("[--issue-url <url> ... [--closes-issue|--refs-issue]]"));
    assert!(text.contains("--issue-url, which may be repeated, links each ticket"));
    assert!(text.contains(
        "a code host that cannot close one of those tickets on a merge refuses --closes-issue"
    ));
    assert!(text.contains("guildbotics member pr update"));
    assert!(text.contains("guildbotics member pr review-comment"));
    assert!(text.contains("guildbotics run repository/pr_checks"));
    assert!(text.contains("failed_logs=true"));
    assert!(text.contains("guildbotics member pr artifact download"));
    assert!(text.contains("guildbotics member pr reaction add"));
    assert!(text.contains("guildbotics member ticket create"));
    assert!(text.contains("guildbotics member ticket reaction add"));
    assert!(text.contains("remove repository files after inspection"));
    assert!(text.contains("--content-file"));
    assert!(text.contains("guildbotics member chat reply"));
    assert!(text.contains("guildbotics member task complete"));
    assert!(text.contains("guildbotics member help"));
}
#[test]
fn reference_prepare_allows_issue_and_pr_url_together() {
    let text = help();
    assert!(text.contains("--issue-url <url> [--pr-url <url> | --repo <repository>]"));
    assert!(text.contains("a ticket branch `ticket/<key>`"));
}
#[test]
fn reference_includes_standard_work_procedure() {
    let text = help();
    assert!(text.contains("### Standard work procedure"));
    assert!(text.contains("Inspect first"));
    assert!(text.contains("before publishing any code change"));
    assert!(text.contains("the completion notice of a background job never arrives"));
    assert!(text.contains("in the foreground with a timeout long enough"));
    assert!(text.contains("check for its completion marker at short intervals"));
    assert!(text.contains("Never wait a fixed time for it"));
    assert!(text.contains("never end the turn while it still runs"));
    assert!(text.contains("This covers only the commands you run inside the environment"));
    assert!(text.contains("Stage with plain git"));
    assert!(text.contains("Pass `--issue-url` once for each ticket the PR works on"));
    assert!(text.contains("add `--closes-issue` only when every one of those tickets is complete once this PR merges into the branch that receives its work"));
    assert!(text.contains("`--closes-issue` applies to all of them"));
    assert!(text.contains("A child ticket can therefore be closed by a PR"));
    assert!(text.contains("`--issue-url <child> --closes-issue`"));
    assert!(text.contains("refer to the parent ticket in ordinary body prose"));
    assert!(text.contains("feature branch to the default branch closes the parent ticket"));
    assert!(text.contains("tickets that the merge does not complete"));
    assert!(text.contains(
        "A merge into a branch other than the default branch closes no ticket automatically"
    ));
    assert!(text.contains("follow the manual-close rule below"));
    assert!(text.contains("does not by itself authorize closing the ticket"));
    assert!(text.contains("files[].commentable_lines"));
    assert!(text.contains("`line`, `side`"));
    assert!(text.contains("`member pr review-comment --head <head_sha>`"));
    assert!(text.contains("take `path` from the containing file"));
    assert!(text.contains("--start-line"));
    assert!(text.contains("--start-side"));
    assert!(text.contains("reply_target_id"));
    assert!(text.contains("After opening or updating a PR"));
    assert!(text.contains("repository/pr_checks failed_logs=true"));
    assert!(text.contains("check CI again"));
    assert!(text.contains("unless `readiness` is `ready`"));
    assert!(text.contains("resolve its `completion_blockers`"));
    assert!(text.contains("the host requires an up-to-date head"));
    assert!(text.contains("checked head SHA must still be current"));
    assert!(text.contains("In any workflow or command run"));
    assert!(text.contains("do not wait for CI in the turn"));
    assert!(text.contains("end the run with `member checks await`"));
    assert!(text.contains("summary says where the outcome is to be reported"));
    assert!(text.contains("the patrol resumes the run's work once the checks finish"));
    assert!(!text.contains("awaiting_checks"));
    assert!(text.contains("externally visible trace"));
    assert!(text.contains("entry point"));
    assert!(text.contains("Avoid duplicate posts"));
}
#[test]
fn reference_grounds_repository_judgments_in_guidelines() {
    let text = help();
    assert!(text.contains("AGENTS.md"));
    assert!(text.contains("current default branch is canonical"));
    assert!(text.contains("guildbotics member git show --person <person> --repo <repository>"));
    assert!(text.contains("--path AGENTS.md --path CLAUDE.md --path CONTRIBUTING.md"));
    assert!(text.contains("with or without a checkout"));
    assert!(text.contains("do not rely on possibly stale working-tree files"));
    assert!(!text.contains("git fetch origin"));
    assert!(!text.contains("git show origin/<default-branch>:<file>"));
    assert!(!text.contains("create a checkout first"));
}
#[test]
fn reference_states_pr_work_record_contract() {
    let text = help();
    assert!(text.contains("member memory record --scope team --pr <pr_url>"));
    assert!(text.contains("--ticket <url>"));
    assert!(text.contains("--thread <url>"));
    assert!(text.contains("remaining follow-up"));
    assert!(text.contains("separate memory documents"));
}
#[test]
fn reference_maps_communication_style_output_kinds() {
    let text = help();
    assert!(!text.is_empty());
    assert!(text.contains("interactive_replies"));
    assert!(text.contains("comments"));
    assert!(text.contains("neutral_documents"));
    assert!(text.contains("machine_outputs"));
}
#[test]
fn reference_states_cross_cutting_rules() {
    let text = help();
    assert!(text.contains("### Rules"));
    assert!(text.contains("git add"));
    assert!(text.contains("git config"));
    assert!(text.contains("memory.pinned"));
    assert!(text.contains("ticket URL, PR URL, chat thread URL"));
    assert!(text.contains("canonical current state"));
    assert!(text.contains("autonomous workflow runs must propose policy"));
    assert!(text.contains("use memory as the primary basis for the answer"));
    assert!(text.contains("Never display, infer, store, or copy secrets"));
    assert!(text.contains("ticket, code-host, and chat access, reads and writes alike"));
    assert!(text.contains("Never use a service's own CLI such as `gh`"));
    assert!(text.contains("When a needed read has no member command, ask a human"));
    assert!(text.contains("read-only copies GuildBotics keeps"));
    assert!(text.contains("never leave a result beside them"));
    assert!(text.contains("under `~/Documents/GuildBotics/`"));
}
#[test]
fn reference_states_which_code_host_writes_stay_human_decisions() {
    let text = help();
    assert!(text.contains("--human-approved"));
    assert!(text.contains("never on the member's own judgment"));
    assert!(text.contains("inspect the current PR and ticket"));
    assert!(text.contains("verify that the merge completed the ticket's whole scope"));
    assert!(text.contains("post a completion comment and close the ticket"));
    assert!(
        text.contains("ticket update --state closed --state-reason completed --human-approved")
    );
    assert!(text.contains("leave the ticket open and comment on what remains"));
    assert!(text.contains("Priority is a human triage decision"));
    assert!(
        text.contains(
            "do not express urgency by adding a label or setting a board or ticket field"
        )
    );
    assert!(text.contains("Assignees and the lane are read-only on purpose"));
    assert!(text.contains("the labels the target tracker already defines"));
}
#[test]
fn reference_standardizes_free_form_input_with_utf8_files() {
    let text = help();
    assert!(text.contains("--content-file <file>"));
    assert!(text.contains("UTF-8"));
    assert!(text.contains("Create a dedicated directory exclusively"));
    assert!(text.contains("`mktemp -d`"));
    assert!(text.contains("outside the repository and worktree"));
    assert!(text.contains("one argv value"));
    assert!(text.contains("delete the directory with the file even when the command fails"));
    assert!(!text.contains("relative name with no spaces"));
    assert!(!text.contains("--title-file"));
    assert!(!text.contains("--body-file"));
    assert!(!text.contains("--body-stdin"));
    assert!(!text.contains("--message-file"));
    assert!(!text.contains("--message-stdin"));
    assert!(!text.contains("--reason-file"));
    assert!(!text.contains("--summary-file"));
}
#[test]
fn reference_documents_complete_content_command_usage() {
    let text = help();
    assert!(text.contains("[--add-to-board|--no-add-to-board]"));
    assert!(text.contains("(--channel-id <id> | --channel-name <name>) --thread-id <id>"));
    assert!(text.contains("[--scope personal|team] --title <title> --content-file <file>"));
    assert!(text.contains("[--keyword <word> ...] [--add-keyword <word> ...]"));
    assert!(text.contains("[--ticket <url>] [--pr <url>]"));
    assert!(text.contains("[--kind note|policy]"));
}
#[test]
fn reference_excludes_task_contract() {
    let text = help().to_lowercase();
    assert!(!text.contains("primary objective"));
    assert!(!text.contains("you must finish"));
}
#[test]
fn reference_excludes_ticket_specific_issue_comment_rule() {
    let text = help()
        .split_once("### Standard work procedure")
        .unwrap()
        .1
        .to_lowercase();
    assert!(!text.contains("member ticket comment"));
    assert!(!text.contains("result comment"));
    assert!(!text.contains("not a substitute"));
}

#[test]
fn draft_instructions_keep_duplicate_search_and_unavailable_outcomes() {
    let text = help().split_whitespace().collect::<Vec<_>>().join(" ");
    for required in [
        "tracker_tickets --subject <tracker> --params",
        "\"text\":",
        "\"state\":\"all\"",
        "continuation",
        "short term sets",
        "synonyms and English/Japanese variants",
        "identical conditions for each term set",
        "no open match was found",
        "still present the complete draft",
        "conditions tried",
        "duplicates could not be ruled out",
    ] {
        assert!(text.contains(required), "{required}");
    }
}
