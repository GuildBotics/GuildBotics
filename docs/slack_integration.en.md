# Slack Integration Guide

A configuration guide for the chat workflow, where a member watches Slack channels and handles the requests it receives there as itself.
For an overview, see [“Ask for Work in Slack” in the README](../README.md#ask-for-work-in-slack).

- [Overview](#overview)
- [Slack App Setup](#slack-app-setup)
- [Configuring in the Desktop App](#configuring-in-the-desktop-app)
- [Scheduled Posts](#scheduled-posts)
- [`person.yml` Reference](#personyml-reference)
- [Posting and Reacting Manually](#posting-and-reacting-manually)
- [How Chat Handling Works Internally](#how-chat-handling-works-internally)

## Overview

A member watches the channels you configure. When a message arrives, the configured AI CLI tool reads the thread context along with the member's own roles and participation policy, performs whatever work is needed, and then chooses how to respond in Slack.

**Doing the requested work**: when the message explicitly asks for an operation in another domain such as GitHub (for example, "please handle the review comments on this PR"), the member carries it out. The member's working directory has no repository checkout, so it identifies the target repository from the message and thread context and prepares a working branch with `guildbotics member git prepare` before working. If the target is ambiguous, it asks in the thread and completes with `asking`.

**How it responds in Slack**: with or without work to do, it always closes out the thread in one of these ways.

- Reply
- Add a reaction
- Do nothing (recorded as a no-op)
- Ask a question (`asking`)
- Report why it cannot proceed (`blocked`)

Whether to reply is decided by whether the member's role adds something new. When the same point has already been made, when a simple acknowledgement is enough, or when the topic is outside its role and its confidence is low, a reaction-only response or a no-op is preferred. When a perspective outside its role is needed, it hands off to another member with that role by mention.

Posts, replies, reactions, and completion records in Slack all go through the member-only CLI (`guildbotics member chat ...`), where they are executed and recorded.

To receive chat events, start the service with **Event triggers** included on the **Service** screen in the desktop app (from the CLI, `guildbotics start`). Scheduled posts go through a separate path: configure them as scheduled commands, as described in [Scheduled Posts](#scheduled-posts).

## Slack App Setup

Create a Slack App (send + receive) in Slack that acts as the AI agent.

1. Create a Slack App at https://api.slack.com/apps
2. Grant the required permissions (scopes)
   - Add them under `OAuth & Permissions` -> `Scopes` in the Slack App admin page
   - Minimum required (add according to the conversation types you use)
     - `chat:write` (for `chat.postMessage`)
     - `reactions:write` (for `reactions.add`)
     - `channels:history` (for `conversations.history` on public channels)
     - `groups:history` (for `conversations.history` on private channels)
     - `im:history` (when handling DMs)
     - `mpim:history` (when handling group DMs)
   - To configure channels by `channel_name`, also add the following for name resolution (`conversations.list`)
     - `channels:read` (public channels)
     - `groups:read` (private channels)
   - To import a member's avatar from Slack (in the setup screen), also add
     - `users:read` (for `users.info`)
   - Reference URLs (official Slack docs)
     - `conversations.history`: `https://api.slack.com/methods/conversations.history`
     - `conversations.list`: `https://api.slack.com/methods/conversations.list`
     - `chat.postMessage`: `https://api.slack.com/methods/chat.postMessage`
     - `reactions.add`: `https://api.slack.com/methods/reactions.add`
     - `users.info`: `https://api.slack.com/methods/users.info`
3. Install the app into your workspace (reinstallation may be required after changing scopes)
4. Note down the Bot Token (`xoxb-...`)
5. Configure Socket Mode
   - Turn on `Enable Socket Mode` under `Socket Mode`
   - Enable `Event Subscriptions` and add the bot events
     - For channels: `message.channels`, `message.groups`
     - For DMs: `message.im`, `message.mpim`
   - Issue an App-Level Token (`xapp-...`) under `Basic Information` and note it down
6. Invite the bot to the target channels
7. Register the two tokens and the target channels as described in [Configuring in the Desktop App](#configuring-in-the-desktop-app)

### Adding Multiple Agents

You can add a second (and further) agent the same way, but you can also skip the Socket Mode setup and share the receiving connection configured for the first AI agent.

To share the receiving connection, set the same App-Level Token as the existing member in the second member's **Slack App token** (or, when passing it as an environment variable, `{PERSON_ID}_SLACK_APP_TOKEN` — for example the same value in `ALICE_SLACK_APP_TOKEN` and `BOB_SLACK_APP_TOKEN`).

If you want a separate receiving path (for example, a separate workspace or a separate Slack App), create an additional Slack App and configure Socket Mode / Event Subscriptions / App-Level Token for it.

## Configuring in the Desktop App

Configure this per member under **Setup → Members → Slack**.

- **Slack Bot token** (`xoxb-...`) and **Slack App token** (`xapp-...`): the tokens you noted down during the Slack App setup
- **Slack User ID**: set this for human members (the member ID starting with `U`)
- **Channel**: add the channel names or IDs to watch (for example `general`, `C0123456789`)
- **When to join**: choose per channel from
  - **Join actively** (`social`): for casual channels. The member may join naturally even without being mentioned
  - **Join when needed** (`strict`, default): for work channels. Responds when mentioned, or when following up in a thread where it was already invited
  - **Mentions only** (`muted`): for announcement channels. Responds only to explicit mentions

The member's persona, which influences both what it says and whether it joins (character archetype, personality traits, interests, join when, avoid when, how to contribute, speaking style), is configured on the **Basic** tab.

Scheduled posts are configured as **Scheduled commands** on the **Patrol** tab (→ [Scheduled Posts](#scheduled-posts)).

Tokens are stored in the OS keychain or `.env`; everything else is stored in `team/members/<person_id>/person.yml`. For the stored format and the settings that are not available in the GUI, see the [`person.yml` Reference](#personyml-reference).

## Scheduled Posts

Posts at fixed times are made by running `workflows/chat_post_command` on a schedule (the post body is the output of a GuildBotics custom command).

Under **Setup → Members → Patrol**, press **Add schedule**, switch the command field to **Custom**, and enter a command line such as the following. Set the time with the Hourly / Daily / Weekly presets or a **Detailed schedule**.

```text
workflows/chat_post_command service=slack channel_name=dev-chat command='examples/reports/ai_news_digest query="OpenAI OR Anthropic OR Gemini" language=ja country=JP limit=10 max_age_hours=24'
```

An example of the command that generates the body (an AI news digest):

```bash
guildbotics run examples/reports/ai_news_digest query="OpenAI OR Anthropic OR Gemini" language=ja country=JP limit=10 max_age_hours=24
```

`examples/reports/ai_news_digest` is a built-in sample command: its first stage collects news candidates from Google News RSS, and its second stage has an LLM format them into a digest for Slack.

Running the whole thing including the post, once, by hand:

```bash
guildbotics run workflows/chat_post_command service=slack channel_name=dev-chat command='examples/reports/ai_news_digest query="OpenAI OR Anthropic OR Gemini" language=ja country=JP limit=10 max_age_hours=24'
```

## `person.yml` Reference

The Slack settings you save in the desktop app are written to `team/members/<person_id>/person.yml` in the following form. On a server without the GUI, edit this file directly.

```yaml
# team/members/alice/person.yml
person_id: alice
name: Alice
is_active: true

message_channels:
  - service: slack
    name: dev-chat
    chat:
      enabled: true
      participation: strict
      startup_backfill_minutes: 60
      backfill_interval_seconds: 300

task_schedules:
  - command: 'workflows/chat_post_command service=slack channel_id=C0123456789 command="examples/reports/ai_news_digest query=\"OpenAI OR Anthropic OR Gemini\" language=ja country=JP limit=10 max_age_hours=24"'
    schedules:
      - "0 9 * * 1-5"
```

Key points:

- Watched channels are defined in `message_channels`; those with `chat.enabled: true` are watched
- `chat.participation` corresponds to **When to join** in the GUI. `strict` (default) handles explicit mentions and follow-ups in threads it was already invited to, `social` also allows natural participation without a mention in casual channels, and `muted` handles explicit mentions only
- `startup_backfill_minutes` and `backfill_interval_seconds` cannot be set from the GUI. At startup, recent channel messages and known thread replies are pulled from Slack history (backfill); the defaults are `60` and `300` respectively. Setting `backfill_interval_seconds` to `0` disables the periodic history check after startup
- `character` defines interests, preferences, conversation participation policy, and so on (this corresponds to the **Basic** tab in the GUI). Chat decisions and reply generation read this profile through the AI CLI tool

The Bot Token and App-Level Token are not stored in `person.yml`. They are passed through the OS keychain, `.env`, or the environment variables `{PERSON_ID}_SLACK_BOT_TOKEN` / `{PERSON_ID}_SLACK_APP_TOKEN` (for example `ALICE_SLACK_BOT_TOKEN` for `alice`).

## Posting and Reacting Manually

You can also post replies and reactions as a member directly from the CLI.

```bash
guildbotics member chat reply --person alice --service slack --channel-id C0123456789 --thread-ts 1777554000.000000 --content-stdin <<'EOF'
A reply body containing `$HOME`, backticks (`command`), and `$(command)` literally
EOF
guildbotics member chat reaction add --person alice --service slack --channel-id C0123456789 --message-ts 1777554000.000000 --reaction ack
```

## How Chat Handling Works Internally

- Chat events are received by the event listener runner, and processed serially by the event queue source inside each member's worker. Both start when the service starts (**Service → Run** in the GUI, or `guildbotics start`)
- When the service is started without **Event triggers** (`guildbotics start --only scheduler` from the CLI), no chat events are received. Conversely, when **Patrol commands** and **Scheduled commands** are excluded (`--only events` from the CLI), the member worker still processes queued chat events
- For chat handling by the AI CLI tool, `functions/handle_chat_event` runs with the member's working directory as `cwd`. By default this is `<workspace>/.guildbotics/data/workspaces/<person_id>/`, where cloned repositories under it can be referenced
- Replies, reactions, no-ops, and completions are recorded through `guildbotics member chat reply|post|reaction add|noop|complete`. The workflow verifies these execution records and does not accept the tool's natural-language stdout alone as evidence that something was posted to Slack
