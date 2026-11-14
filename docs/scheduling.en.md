# Scheduling Guide

A detailed guide to per-member scheduled execution. For an overview, see [“Automate Recurring Work” in the README](../README.md#automate-recurring-work).

The scheduler supports two ways of running commands.

- **Patrol commands** (`routine_commands`): commands repeated at a fixed interval while the service is running
- **Scheduled commands** (`task_schedules`): commands run at the times given in cron notation

- [Configuring on the Screens](#configuring-on-the-screens)
- [Patrol Commands](#patrol-commands)
- [Scheduled Commands](#scheduled-commands)
- [Cron Expression Format](#cron-expression-format)
- [How Scheduling Works Internally](#how-scheduling-works-internally)
- [Configuration Examples](#configuration-examples)

## Configuring on the Screens

Per-member settings live under **Setup → Members → Patrol**.

- **Configure patrol commands for this member**: turn it on and choose the commands to run on patrol. Members with it turned off do not run patrol work
- **Add schedule**: choose the command to run and its time. The command can be picked from the **Catalog** (with argument fields) or entered as a **Custom** command line. The time is an Hourly / Daily / Weekly preset, or a **Detailed schedule**

Service-wide settings live on the **Service** screen.

- **Patrol interval (minutes)**: how often patrol commands run (default 10 minutes)
- **Stop after consecutive failures**: stops that member's worker after this many consecutive failures (default 3)
- **Include in service run**: enables patrol commands / scheduled commands / event triggers individually
- **Start the services when the server starts**: the server starts the service by itself whenever it starts, for example at every login when it runs as an OS service (off by default: the server starts alone)

These are saved in the workspace's `.guildbotics/local/service.yml` on the machine running the service, and every start uses them: from any screen, from `guildbotics service start`, or by the server as it starts.

The per-member settings above are stored in the workspace's `team/members/<person_id>/member.yml` (the member's participation in that workspace) as `routine_commands` and `task_schedules`. The sections below describe that stored format, and serve as the reference when you edit the file by hand.

Ready-lane candidates follow board order, including tickets picked up by mentions. Assigned cards with unfinished dependencies are skipped. In-progress ticket and PR ordering is unchanged. Reordering takes effect at the next candidate fetch for scheduled and manual runs; patrol sees it after exhausting and refreshing its current candidate list.

Patrol and scheduled ticket workflows defer only new ready-lane tickets while the member has reached the open PR limit (default 2, range 1–20). Drafts and PRs without tickets count; PRs linked to interactive tickets, including inherited interactive settings, do not. Counts are refreshed when listing candidates and immediately before starting work; merging or closing a PR frees capacity. Working-lane resumes, comments and mentions, PR feedback, reviews, CI resumes, and manual runs continue. Set the shared limit for all members on the Service screen; it applies from the next service start. While running, the screen shows each member’s last observed count / limit and whether new work is deferred.

## Patrol Commands

**Patrol commands** (`routine_commands`) are executed continuously in round-robin order.

**Characteristics**:

- While the service is running, one command is executed per patrol interval (default 10 minutes)
- When several commands are configured, they run one at a time in order
- During initial setup, a new agent member is configured with `workflows/ticket_driven_workflow`. Members without `routine_commands` do not run patrol work

**Example**:

```yaml
# team/members/alice/member.yml
is_active: true

# Override the default patrol commands (optional)
routine_commands:
  - workflows/ticket_driven_workflow
  - workflows/custom_workflow
```

**Typical uses**:

- Periodic task board checks (for example `workflows/ticket_driven_workflow`)
- Continuous monitoring tasks
- Patrol-style processing

## Scheduled Commands

**Scheduled commands** (`task_schedules`) are executed at the specific times defined in cron notation.

**Characteristics**:

- Checked every minute and executed when the current time matches a schedule
- One command can have multiple schedule patterns
- Randomization syntax (jitter) is supported

**Example**:

```yaml
# team/members/alice/member.yml
is_active: true

# Schedule commands to run at specific times
task_schedules:
  - command: workflows/cleanup
    schedules:
      - "0 2 * * *" # Every day at 2:00 AM
      - "30 14 * * 5" # Every Friday at 2:30 PM
  - command: workflows/backup
    schedules:
      - "0 0 1 * *" # First day of each month at midnight
```

**Typical uses**:

- Periodic cleanup
- Backups and report generation
- Tasks that must run at a fixed time

## Cron Expression Format

GuildBotics uses standard five-field cron notation:

```
* * * * *
│ │ │ │ │
│ │ │ │ └─── day of week (0-6, Sunday=0)
│ │ │ └───── month (1-12)
│ │ └─────── day of month (1-31)
│ └───────── hour (0-23)
└─────────── minute (0-59)
```

**Common examples**:

```yaml
schedules:
  - "0 9 * * *" # Every day at 9:00 AM
  - "*/15 * * * *" # Every 15 minutes
  - "0 */2 * * *" # Every 2 hours
  - "0 0 * * 0" # Every Sunday at midnight
  - "30 8 1,15 * *" # The 1st and 15th of each month at 8:30 AM
  - "0 22 * * 1-5" # Weekdays at 10:00 PM
```

**Randomization syntax (jitter)**:

GuildBotics extends standard cron notation with randomization:

- `?`: a random value within the default range
- `?(min-max)`: a random value within the given range

**Examples**:

```yaml
schedules:
  - "? 9 * * *" # A random minute between 9:00 and 9:59 every day
  - "?(0-30) 14 * * *" # A random minute between 14:00 and 14:30 every day
  - "0 ?(9-17) * * 1-5" # A random hour between 9 and 17 on weekdays (at :00)
```

**Why randomize**:

- Avoids simultaneous execution across multiple agents
- Simulates human-like irregular timing
- Spreads load across a time window

## How Scheduling Works Internally

When an author starts addressing PR feedback, Branvil returns running CI for a deferred head to its original queue position.

How the scheduler behaves (from `crates/gb-server/src/native_scheduler.rs` and `crates/gb-server/src/schedule.rs`):

**Architecture**:

1. **A worker thread per member**: each active team member gets a dedicated worker thread
2. **A per-minute check cycle**: every minute, each worker thread:
   - Checks all `task_schedules` of its member
   - Runs the commands whose schedule matches the current time
   - Runs one `routine_command` in round-robin order, if the patrol interval (default 10 minutes) has elapsed

**Randomization handling**:

1. At initialization, the next execution time of a randomized schedule is computed
2. For `?` fields, a random value is sampled within the bounds
3. It is resampled after each execution boundary is reached

**Error handling**:

- The worker thread stops after consecutive command failures (default: 3)
- A searchable execution summary is stored in `<workspace>/.guildbotics/local/run/diagnostics.jsonl`,
  and the full per-execution record in `run/sessions/`

## Configuration Examples

Some real-world schedule configurations.

### Multi-Agent Scheduled Workflow

**Scenario**: two agents with different schedules

**Agent 1** (`.guildbotics/config/team/members/agent1/member.yml`):

```yaml
is_active: true

# Run the ticket-driven workflow on patrol
routine_commands:
  - workflows/ticket_driven_workflow

# Generate a morning report on weekday mornings
task_schedules:
  - command: examples/reports/morning_summary
    schedules:
      - "0 9 * * 1-5" # Weekdays at 9:00 AM
```

**Agent 2** (`.guildbotics/config/team/members/agent2/member.yml`):

```yaml
is_active: true

# Run code review checks on patrol
routine_commands:
  - workflows/code_review_check

# Weekly and monthly maintenance tasks
task_schedules:
  - command: workflows/cleanup_old_branches
    schedules:
      - "0 0 * * 0" # Sunday at midnight
  - command: workflows/dependency_update_check
    schedules:
      - "?(0-59) 10 1 * *" # A random minute in the 10 AM hour on the 1st of each month
```

**Starting both agents**:

Press **Run** on the **Service** screen (`guildbotics service start` starts it as well). Both agents run in parallel, each repeating its patrol commands and running its scheduled commands at the configured times.

### Multiple Schedule Patterns

Configuring several schedules for one command:

```yaml
# team/members/maintenance_bot/member.yml
is_active: true

task_schedules:
  # Clean up on weekdays at 2 AM and on weekends at midnight
  - command: workflows/cleanup
    schedules:
      - "0 2 * * 1-5" # Weekdays at 2:00 AM
      - "0 0 * * 0,6" # Weekends at midnight

  # Back up daily at 3 AM and at the start of each month
  - command: workflows/backup
    schedules:
      - "0 3 * * *" # Every day at 3:00 AM
      - "0 0 1 * *" # First day of each month at midnight (monthly backup)
```

### Randomization Usage

Randomized settings that avoid contention between agents:

```yaml
# team/members/agent_alpha/member.yml
is_active: true

task_schedules:
  # Run a check at a random time in the 9 AM hour
  - command: workflows/morning_check
    schedules:
      - "?(0-59) 9 * * 1-5" # A random minute between 9:00 and 9:59 on weekdays

  # Run monitoring at a random time during the day
  - command: workflows/health_check
    schedules:
      - "0 ?(9-17) * * *" # A random hour between 9 and 17 every day (at :00)
```
