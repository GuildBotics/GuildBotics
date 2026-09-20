# Chat judgment

In **Setup → LLM / AI CLI tools → Advanced → Chat judgment engine**, select the engine and target, then save at the top of the section.

- **LLM** uses an existing model slot, including its provider, model, parameters, and credentials.
- **CLI** uses an existing AI CLI slot and its model/effort settings through the shared Codex, Claude, Grok, Copilot, or Antigravity adapter. Prepare the device environment and log in first.
- **Jev** uses `jev-latest`, which follows the latest official release. No model selection is needed. Its API key field appears when Jev is selected. Enter a key and use the section’s **Save** button to save both settings and credentials. Registered keys appear masked. Leave the field unchanged to keep the registered key. The key is stored as `TYPESAFE_API_KEY` in the workspace SecretStore.

The **Chat judgment engine** card sits between the AI CLI tool definitions and the environment declaration. Select the engine and, for LLM or CLI, the slot to use. Model settings belong to the selected slot.

The UI and save validation restrict Jev to `chat_decision`. Ordinary command assignments such as `agent` and `default` cannot select it: Jev consumes structured state and questions rather than generating free-text responses.

Switching to LLM or CLI hides the Jev key field and retains the registered key. Saving does not invoke a model. Missing credentials, execution failures, and malformed responses delegate to the response agent.

For example, a team using Jev stores this entry in the existing `.guildbotics/config/intelligences/brain_mapping.yml`:

```yaml
chat_decision:
  class: guildbotics.intelligences.brains.jev.JevBrain
  args:
    model: jev-latest
```

For LLM, `AgnoAgentDefaultBrain.model` names an existing model slot; for CLI, `CliAgentBrain.cli_agent` names an existing CLI slot. The initial assignment uses the LLM `default` slot. Member feature overrides use the normal mapping inheritance, preserving team values for untouched features and slots. The old `decision.yml` is not read; reselect any trial configuration using this assignment. Stored API keys remain in SecretStore.

Judgment creates a Brain through the common BrainFactory and calls `Brain.run()` once. CLI evaluations use fresh conversations without workspace files, shared documents, past sessions, or cache mounts. Member capability calls are rejected, and network access is limited to the provider API and the rejecting member broker. Only credential refreshes are persisted; the evaluation environment is destroyed at the end. The member's work conversation is unaffected.

## What happens before a reply

After the existing mention and participation gates, the workflow evaluates the entire unread batch once, together with thread history, standing roles, character, handoffs, and recorded actions. Corrections and cancellations apply across the batch. Twelve independent yes/no questions cover context, unanswered requests, useful contribution, acknowledgment, repetition, social fit, handoffs, and remaining work. One Choice question selects `ack`, `agree`, `celebrate`, `support`, or `none` for the last non-self message in the batch.

Jev Noul probabilities at or above `0.6` become true and at or below `0.4` become false. Values between those bounds remain unknown. For Choice, the returned option with the highest probability is adopted without a probability or confidence threshold. The probabilities and confidence remain in the evaluation record. Agno and AI CLI return explicit true, false, or unknown; their optional confidence is preserved as reported, without treating it as a Jev probability.

The current adoption version is `jev-noul-0.4-0.6-choice-top-3/structured-1`. After evaluating 48 new synthetic Japanese conversations beyond the initial eight cases, the Noul range was adopted provisionally for operational validation. Preventing missed obligations takes priority over avoiding unnecessary agent starts. If a Noul unknown prevents establishing that no substantive response is needed, the agent runs. Choice only selects a reaction after the Noul rules allow it; it cannot cancel a required agent start.

The shared questions preserve the response prompt's responsibilities:

| Questions | Response-prompt responsibility |
| --- | --- |
| `context_sufficient`, `pending_request` | Instructions 4 and 13: understand current requests, and ask for missing information |
| `role_contribution`, `ack_only`, `repeated_supplement` | Instruction 6: add standing-role value, avoid repeated acknowledgment or supplementation |
| `social_fit` | Instruction 7: require natural character or role fit for social replies |
| `other_role_needed`, `handoff_done`, `handoff_reopen` | Instruction 8: invite a needed role without repeating a completed handoff |
| `reaction` | Instructions 11 and 12: choose a lightweight reaction for the fixed target or a no-op |
| `work_files`, `work_repo_research`, `work_repo_decision` | Remaining file changes, repository investigation, or decisions requiring repository guidelines |

Noul is used where a condition's presence changes the route. Its intermediate probabilities mean uncertainty, not partial usefulness or partial completion. Choice represents unordered reaction meanings. Score is not used because intermediate degrees of novelty, relevance, or social fit would lead to the same action. The thresholds above are this implementation's initial policy, not a claimed probability of correctness.

The ordered rules are:

1. Invalid output or insufficient context starts the response agent.
2. An unresolved request or required work starts the response agent; unknown also does so.
3. A missing role that still needs a handoff starts the response agent. Handoffs are considered per topic across the batch, including reasons to reopen them.
4. A useful, non-repetitive substantive contribution starts the response agent. Social participation also requires character or role fit.
5. Otherwise, send the selected reaction, or complete without a visible action when `none` is selected. Jev uses the highest-probability option even when that probability is low. An explicit `unknown` from Agno or AI CLI still starts the response agent.

For example, when Noul excludes any outstanding work or substantive response, an `ack` selected at probability `0.53` produces only an acknowledgment reaction. A document request still starts the agent even if Choice selects `celebrate` at `0.87`.

Unknown values that cannot change a rule's result do not force escalation. Before a reaction or no-op completes, the workflow checks reception and rereads the thread for new messages or edits. Changed or unavailable input remains pending. Reactions record evidence and join the thread; a no-op does not join it. Repeated completion attempts reuse evidence, and an already-present Slack reaction counts as success.

The agent remains responsible for the final reply, reaction, question, handoff, or blocked result. Judgment selects only whether to start the agent, send a reaction, or take no action. It never specifies the response agent’s model or effort, and stores no thread effort. The response command uses its own configuration on success, uncertainty, and failure alike. If that response cannot run, the batch remains pending.

## Inspect and replay

After a reaction or completion recording failure, retries of the same input reuse the latest saved judgment. They do not choose a different reaction or repeat a recorded send. Changed conversation content, batch membership, or participation conditions require a new judgment.

Before invoking the model, `<evaluation_id>1.json` records the resolved Brain class, provider, slot, requested model, and public inference settings. Parameters use an allowlist including model names, temperature, top_p, seed, token limits, and effort; arbitrary credentials and environment variables are excluded. The completed record retains resolved settings and the requested model even on failure. Values unknown before invocation, such as a CLI-selected default model, remain empty; the actual returned model is recorded when available. Failure to record the resolved configuration prevents the model call and delegates to the agent.

Each evaluation writes mandatory device-local JSON records below the workspace's local `run/required-io/` directory, even when optional transcripts are off. The `decision.evaluated` diagnostics event and `chat_decision` run evidence identify the evaluation. The completed record is `<evaluation_id>.json`; `<evaluation_id>0.json` records the attempt before the call. Records contain full masked input, questions, versions, input hash, requested Brain feature, actual returned model, raw and normalized answers, adoption reasons, elapsed time, and available usage, cost, and retry counts. Unknown measurements stay `null`. These records are not shared by Workspace Sync. A recording failure forbids reaction/no-op completion.

To compare another model on a saved input from the current question version without posting to chat or changing receipt state (older question versions are rejected before any model call):

```bash
uv run --no-sync python scripts/evaluate-chat-decision.py /path/to/evaluation.json \
  --workspace /path/to/workspace --person aiko \
  --brain chat_decision
```

To compare a different model, add another feature assignment (for example `comparison`) targeting its model or CLI slot, then pass `--brain comparison`. Each replay preserves the saved questions, writes a new evaluation record, and prints the old and new selections and IDs. It makes a real request using that workspace's credentials. Masked secret text cannot be reconstructed.

Jev's [API](https://docs.typesafe.ai/api) and [model documentation](https://docs.typesafe.ai/models) describe the probability contract and model aliases. Validate Japanese conversations with representative examples before relying on a particular model's fast-path choices.

## Comparison using 14 saved evaluations (2026-09-20)

The [evaluation data](evaluations/chat_decisions_2026-09-20.json) includes predefined agent-start labels and acceptable reactions, conversation inputs, questions, raw answers, timings, and evaluation IDs. Jev `jev-1.13.0` and Agno / OpenAI `gpt-5.6-luna` were each called once per case, with median times of 917 ms and 4,649 ms. Repeated identical Jev requests do not add independent samples. The earlier Noul study contained 48 distinct conversations; three calls each do not make 144 cases.

The table applies Noul 0.4/0.6 and highest-probability Choice adoption to saved answers. Historical `effort_*` question names were mapped to `work_*` and routed with current rule version `chat-3`. **The original question version was `chat-1`: these are not live evaluations of current questions `chat-2`.** Values are reaction names, `no-op`, or `agent`.

| Case (case ID suffix in the dataset) | Expected agent start | Jev | Agno |
| --- | --- | --- | --- |
| receipt | No | ack | celebrate |
| cancel | No | ack | ack |
| agree | No | agree | agree |
| celebrate | No | celebrate | celebrate |
| support | No | support | support |
| none-explicit | No | no-op | no-op |
| none-log | No | no-op | no-op |
| none-sensitive | No | no-op | no-op |
| thanks | No | support | ack |
| rejected | No | ack | ack |
| pending-thanks | Yes | agent | agent |
| pending-win | Yes | agent | agent |
| missing | Yes | agent | agent |
| false-claim | Yes | agent | agent |

Both produced four agent starts, seven reactions, and three no-ops. This sample contained no missed obligations, unnecessary starts, or reactions outside the predefined acceptable sets. Agreement on a small synthetic sample does not establish equal model accuracy. Live evaluation of the entire current implementation, live CLI comparisons, production missed-obligation rates, and end-to-end response savings remain unmeasured.

## Initial Japanese evaluation (2026-09-20)

These historical results include the former response-effort selection. Current rules (`chat-3`, questions `chat-2`) do not select response effort; the measurements below are not results of the current implementation.

The [eight synthetic cases](../tests/fixtures/chat_decisions.ja.json) define expected routes before execution. They cover acknowledgment, an unresolved request followed by thanks, cancellation, missing context, a new topic needing a handoff, a completed handoff, unrelated social chatter, and a guideline-dependent design request. Run the same cases with either engine:

```bash
uv run --no-sync python scripts/evaluate-chat-decision.py tests/fixtures/chat_decisions.ja.json \
  --workspace /path/to/workspace --person aiko --brain chat_decision \
  --report /path/to/jev-report.json
```

The report includes answers, probabilities, usage, elapsed time, versions, input hashes, and expected-route matches. Both runs used question/rule version `chat-1` and adoption version `jev-0.1-0.9-1/structured-1`.

| Measurement (8 cases, one run each) | Jev `jev-1.13.0` | Agno / OpenAI `gpt-5.6-luna` |
| --- | --- | --- |
| Median evaluation time, including local recording before the call | 702 ms | 6,597 ms |
| Total evaluation time | 5.70 s | 55.27 s |
| Input / output tokens | 15,331 / 2,240 | 13,084 / 5,380 |
| Response-agent starts | 8 | 4 |
| Incorrect skips / inappropriate reactions observed | 0 / 0 | 0 / 0 |
| Unnecessary starts against the manually defined cases | 4 | 0 |
| Required high effort in the two explicit repository-work cases | 2 / 2 | 2 / 2 |

Jev's context probabilities were 0.57–0.78, below the adoption threshold, so every case went to the agent. It also returned uncertain effort for six cases; seven routes used high effort overall. This sample demonstrates conservative operation but **does not demonstrate a startup reduction for Jev**. Agno used reactions for acknowledgment and cancellation, no-op for completed handoff and unrelated chatter, and the agent for the remaining four cases. Its missing-context case incorrectly asserted sufficient context, but the pending-request and uncertain-effort answers still selected the agent with high effort.

These are individual observations, not an accuracy estimate. Costs were not returned and remain unknown; Jev made zero retries, while Agno's internal retry count was unavailable. No live chat action was performed. End-to-end reply latency, actual saved response-agent usage, long histories, and sustained production accuracy remain unmeasured. This initial measurement predates the common Brain integration. A live CLI comparison was not run because the preview host lacked Documents access required by the existing environment readiness check.
### Thresholds and limits of the comparison

The 0.1/0.9 cutoffs were the initial values specified in Issue #543; they were not calibrated for this use case. That evaluation applied thresholds to the Noul probability of yes and the probability of the selected Choice option. Choice's distribution-derived `confidence` was recorded but was not the adoption threshold. Agno and AI CLI answers are structured assertions, with no numeric threshold. The shared questions and downstream rules therefore do **not** constitute a comparison at matched error rates. An Agno `true` assertion does not provide the same guarantee as a Jev probability above 0.9.

For the acknowledgment case, Jev returned context sufficiency 0.78, pending request 0.07, repository research 0.14, and a `support` reaction probability of 0.62. At the initial cutoffs, context became unknown and started the agent; relaxing that gate alone still left research and reaction uncertainty. Since Jev skipped no starts, its zero unsafe skips do not validate the safety of skipping.

Applying alternative thresholds only to the saved answers gives the following sensitivity analysis. This does not rerun a model or change operational settings.

| Noul false ceiling / true floor | Choice adoption floor | Skipped starts |
| --- | --- | --- |
| 0.1 / 0.9 (initial) | 0.9 | 0 / 8 |
| 0.2 / 0.8 | 0.8 | 0 / 8 |
| 0.3 / 0.7 | 0.7 | 0 / 8 |
| 0.4 / 0.6 | 0.6 | 2 / 8 |
| 0.5 / 0.5 (true wins at the boundary) | 0.5 | 3 / 8 |

These results do not justify recommending 0.5 or 0.6. Further evaluation needs question-level labels and held-out cases, including whether the context question conflates information needed to choose a route with information needed to perform the task. Tune and validate on separate examples, comparing missed obligations, reaction types and skipped starts for both engines before treating the adoption policy as validated.
