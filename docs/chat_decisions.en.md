# Chat judgment

In **Settings → LLM / AI CLI tools → Advanced settings → Chat judgment engine**, register credentials, choose an engine and model, check the connection, and save the settings.

- **Jev**: enter the API key and press **Save Jev credentials**. This stores `TYPESAFE_API_KEY` in the workspace SecretStore. Registration stays accessible when Jev cannot be selected. Choose `jev-1.13.0` for a fixed model or `jev-latest` / `jev-preview` for an alias. **Check connection / refresh models** checks both Noul (a probability of yes) and Choice (one criterion), and refreshes the model candidates.
- **Agno**: register the provider key in the existing provider API credential controls, then choose that provider and enter its model ID. Connection checking evaluates the same two question types without tools.
- **AI CLI**: prepare the agent environment and sign in from the existing AI CLI controls. Chat judgment currently supports Claude Code. Enter a Claude model ID and check the connection. Judgment runs in a fresh probe environment without repository, member broker, tools, MCP servers, or conversation reuse.

Typing a key does not register it. A registered key is initially **authentication unchecked**; only a successful check shows **Connection verified**. Authentication and connection errors are distinct. **Refresh availability** rereads local credentials and environment readiness. Removing credentials makes a saved choice unavailable but keeps its engine and model. Another engine is never selected automatically.

The team selection is stored as `.guildbotics/config/intelligences/decision.yml`:

```yaml
engine: jev
provider: ''
model: jev-1.13.0
```

The corresponding file under `team/members/<person_id>/intelligences/` overrides the team selection. Turn off **Inherit team defaults** in the member's intelligence settings to edit it; restoring inheritance removes the override. Keys remain in SecretStore, outside this configuration. Each device needs credentials; use the existing Secret transfer controls when sharing a workspace between devices.

## What happens before a reply

After the existing mention and participation gates, the workflow evaluates the entire unread batch once, together with thread history, standing roles, character, handoffs, and recorded actions. Corrections and cancellations apply across the batch. Twelve independent yes/no questions cover context, unanswered requests, useful contribution, acknowledgment, repetition, social fit, handoffs, and effort. One Choice question selects `ack`, `agree`, `celebrate`, `support`, or `none` for the last non-self message in the batch.

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
| `effort_files`, `effort_repo_research`, `effort_repo_decision` | The previous effort prompt's three high-effort conditions |

Noul is used where a condition's presence changes the route. Its intermediate probabilities mean uncertainty, not partial usefulness or partial completion. Choice represents unordered reaction meanings. Score is not used because intermediate degrees of novelty, relevance, or social fit would lead to the same action. The thresholds above are this implementation's initial policy, not a claimed probability of correctness.

The ordered rules are:

1. Invalid output or insufficient context starts the response agent.
2. An unresolved request or required work starts the response agent; unknown also does so.
3. A missing role that still needs a handoff starts the response agent. Handoffs are considered per topic across the batch, including reasons to reopen them.
4. A useful, non-repetitive substantive contribution starts the response agent. Social participation also requires character or role fit.
5. Otherwise, send the selected reaction, or complete without a visible action when `none` is selected. Jev uses the highest-probability option even when that probability is low. An explicit `unknown` from Agno or AI CLI still starts the response agent.

For example, when Noul excludes any outstanding work or substantive response, an `ack` selected at probability `0.53` produces only an acknowledgment reaction. A document request still starts the agent even if Choice selects `celebrate` at `0.87`.

Unknown values that cannot change a rule's result do not force escalation. Before a reaction or no-op completes, the workflow checks reception and rereads the thread for new messages or edits. Changed or unavailable input remains pending. Reactions record evidence and join the thread; a no-op does not join it. Repeated completion attempts reuse evidence, and an already-present Slack reaction counts as success.

The agent remains responsible for the final reply, reaction, question, handoff, or blocked result. File changes, repository investigation, or a repository-guideline-dependent decision require high effort. All three false values use default effort; an unknown or failed effort judgment uses high. An existing high effort is preserved. A reaction or no-op does not promote effort. Unconfigured, unavailable, malformed, or failed judgment falls back to the response agent with high effort; if that response cannot run, the batch remains pending.

## Inspect and replay

Each evaluation writes mandatory device-local JSON records below the workspace's local `run/required-io/` directory, even when optional transcripts are off. The `decision.evaluated` diagnostics event and `chat_decision` run evidence identify the evaluation. The completed record is `<evaluation_id>.json`; `<evaluation_id>0.json` records the attempt before the call. Records contain full masked input, questions, versions, input hash, requested engine/model, actual returned model, raw and normalized answers, adoption reasons, elapsed time, and available usage, cost, and retry counts. Unknown measurements stay `null`. These records are not shared by Workspace Sync. A recording failure forbids reaction/no-op completion.

To compare another model on a saved input without posting to chat or changing receipt state:

```bash
uv run --no-sync python scripts/evaluate-chat-decision.py /path/to/evaluation.json \
  --workspace /path/to/workspace --person aiko \
  --engine jev --model jev-1.13.0
```

For Agno add `--engine agno --provider openai --model <model-id>`; for Claude use `--engine cli --provider claude --model <model-id>`. Each replay preserves the saved questions, writes a new evaluation record, and prints the old and new selections and IDs. It makes a real model request using that workspace's credentials. Masked secret text cannot be reconstructed by replay.

Jev's [API](https://docs.typesafe.ai/api) and [model documentation](https://docs.typesafe.ai/models) describe the probability contract and model aliases. Validate Japanese conversations with representative examples before relying on a particular model's fast-path choices; a passing connection check verifies the response contract, not conversation accuracy.

## Initial Japanese evaluation (2026-09-20)

The [eight synthetic cases](../tests/fixtures/chat_decisions.ja.json) define expected routes before execution. They cover acknowledgment, an unresolved request followed by thanks, cancellation, missing context, a new topic needing a handoff, a completed handoff, unrelated social chatter, and a guideline-dependent design request. Run the same cases with either engine:

```bash
uv run --no-sync python scripts/evaluate-chat-decision.py tests/fixtures/chat_decisions.ja.json \
  --workspace /path/to/workspace --person aiko --engine jev --model jev-1.13.0 \
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

These are individual observations, not an accuracy estimate. Costs were not returned and remain unknown; Jev made zero retries, while Agno's internal retry count was unavailable. No live chat action was performed. End-to-end reply latency, actual saved response-agent usage, long histories, and sustained production accuracy remain unmeasured. Claude's invocation and cleanup are covered by a stubbed environment test; a live CLI comparison was not run because the preview host lacked Documents access required by the existing environment readiness check.
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

These results do not justify recommending 0.5 or 0.6. Further evaluation needs question-level labels and held-out cases, including whether the context question conflates information needed to choose a route with information needed to perform the task. Tune and validate on separate examples, comparing missed obligations, reaction types, unnecessary high effort, and skipped starts for both engines before treating the adoption policy as validated.
