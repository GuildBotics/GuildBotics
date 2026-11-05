//! Five-field cron commands with the frozen host's random-field semantics.
use chrono::{Datelike, Duration, NaiveDate, NaiveDateTime, Timelike};
use croner::Cron;
use std::str::FromStr;

const RANGES: [(u32, u32); 5] = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)];

pub struct ScheduledCommand {
    pub command: String,
    schedule: String,
    cron: Cron,
    random: [Option<(u32, u32)>; 5],
    boundary: NaiveDateTime,
    due: NaiveDateTime,
    executed: bool,
}

impl ScheduledCommand {
    pub fn new(command: String, schedule: String, now: NaiveDateTime) -> anyhow::Result<Self> {
        let parts: Vec<_> = schedule.split_whitespace().collect();
        anyhow::ensure!(parts.len() == 5, "Exactly 5 fields required");
        let mut random = [None; 5];
        let mut max_fields = Vec::new();
        for (i, part) in parts.iter().enumerate() {
            let range = if *part == "?" {
                Some(RANGES[i])
            } else if let Some(range) = part.strip_prefix("?(").and_then(|s| s.strip_suffix(')')) {
                let (low, high) = range
                    .split_once('-')
                    .ok_or_else(|| anyhow::anyhow!("invalid random range"))?;
                Some((low.parse::<u32>()?, high.parse::<u32>()?))
            } else {
                None
            };
            if let Some((low, high)) = range {
                anyhow::ensure!(
                    low >= RANGES[i].0 && high <= RANGES[i].1 && low <= high,
                    "invalid random range"
                );
                random[i] = Some((low, high));
                max_fields.push(high.to_string());
            } else {
                max_fields.push((*part).to_owned());
            }
        }
        let cron = Cron::from_str(&max_fields.join(" "))?;
        // Croniter used naive local dates. UTC here is only a calendar adapter;
        // no timezone conversion changes those wall-clock fields.
        let boundary = cron
            .find_next_occurrence(&now.and_utc(), false)?
            .naive_utc();
        let due = sample(boundary, &random, &mut draw)?;
        Ok(Self {
            command,
            schedule,
            cron,
            random,
            boundary,
            due,
            executed: false,
        })
    }

    pub fn due_at(&self) -> NaiveDateTime {
        self.due
    }

    pub fn should_run(&mut self, now: NaiveDateTime) -> anyhow::Result<bool> {
        if !self.executed && now >= self.due {
            self.executed = true;
            return Ok(true);
        }
        if now >= self.boundary {
            self.boundary = self
                .cron
                .find_next_occurrence(&self.boundary.and_utc(), false)?
                .naive_utc();
            self.due = sample(self.boundary, &self.random, &mut draw)?;
            self.executed = false;
        }
        Ok(false)
    }

    pub fn defer(&mut self) {
        self.executed = false;
    }
}

impl std::fmt::Display for ScheduledCommand {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "ScheduledCommand(command={}, schedule={}, next_run={}, executed={})",
            self.command,
            self.schedule,
            self.due,
            if self.executed { "True" } else { "False" }
        )
    }
}

fn draw(low: u32, high: u32) -> u32 {
    let width = high - low + 1;
    // Rejection sampling avoids a modulo bias, including for small ranges.
    let cutoff = u32::MAX - u32::MAX % width;
    loop {
        // The last four bytes contain no UUID version or variant bits.
        let id = uuid::Uuid::new_v4();
        let value = u32::from_be_bytes(id.as_bytes()[12..16].try_into().unwrap());
        if value < cutoff {
            return low + value % width;
        }
    }
}

fn last_day(year: i32, month: u32) -> u32 {
    let (year, month) = if month == 12 {
        (year + 1, 1)
    } else {
        (year, month + 1)
    };
    NaiveDate::from_ymd_opt(year, month, 1)
        .unwrap()
        .pred_opt()
        .unwrap()
        .day()
}

fn sample(
    mut date: NaiveDateTime,
    random: &[Option<(u32, u32)>; 5],
    rng: &mut impl FnMut(u32, u32) -> u32,
) -> anyhow::Result<NaiveDateTime> {
    for (i, range) in random.iter().enumerate() {
        let Some((low, high)) = *range else { continue };
        let value = rng(low, high);
        anyhow::ensure!((low..=high).contains(&value), "random value outside range");
        date = match i {
            0 => date.with_minute(value),
            1 => date.with_hour(value),
            2 => date.with_day(value.min(last_day(date.year(), date.month()))),
            3 => {
                let day = date.day().min(last_day(date.year(), value));
                date.with_day(day).and_then(|date| date.with_month(value))
            }
            4 => Some(
                date - Duration::days(i64::from(
                    (date.weekday().num_days_from_sunday() + 7 - value) % 7,
                )),
            ),
            _ => unreachable!(),
        }
        .ok_or_else(|| anyhow::anyhow!("invalid random date"))?;
    }
    Ok(date)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn at(text: &str) -> NaiveDateTime {
        NaiveDateTime::parse_from_str(text, "%Y-%m-%d %H:%M").unwrap()
    }

    #[test]
    fn occurrence_runs_once_defers_without_consuming_and_resets_at_boundary() {
        let now = at("2025-05-16 23:59");
        let mut job = ScheduledCommand::new("test".into(), "0 0 * * *".into(), now).unwrap();
        assert!(!job.should_run(now).unwrap());
        let first = at("2025-05-17 00:00");
        assert_eq!(job.due_at(), first);
        assert!(job.should_run(first).unwrap());
        job.defer();
        assert!(job.should_run(first).unwrap());
        assert!(!job.should_run(first).unwrap());
        assert!(!job.should_run(at("2025-05-17 00:01")).unwrap());
        assert!(job.should_run(at("2025-05-18 00:00")).unwrap());
    }

    #[test]
    fn random_occurrences_wait_until_the_sample_run_once_and_render_the_exact_state() {
        let now = at("2025-05-17 00:00");
        for (spec, due) in [
            ("? ? * * *", now + Duration::minutes(5)),
            ("?(10-20) ?(3-5) * * *", now + Duration::hours(4)),
        ] {
            let mut job = ScheduledCommand::new("test".into(), spec.into(), now).unwrap();
            // Freeze the sampled instant just as the frozen test replaces its RNG.
            job.due = due;
            assert!(!job.should_run(now).unwrap());
            assert_eq!(job.due_at(), due);
            assert_eq!(
                job.to_string(),
                format!(
                    "ScheduledCommand(command=test, schedule={spec}, next_run={due}, executed=False)"
                )
            );
            assert!(job.should_run(due).unwrap());
            assert_eq!(
                job.to_string(),
                format!(
                    "ScheduledCommand(command=test, schedule={spec}, next_run={due}, executed=True)"
                )
            );
            assert!(!job.should_run(due).unwrap());
        }
    }

    #[test]
    fn random_default_and_explicit_ranges_preserve_clamp_and_weekday_order() {
        let boundary = at("2025-05-17 10:20");
        let random = [Some((0, 59)), Some((0, 23)), None, None, None];
        let mut values = [15, 3].into_iter();
        assert_eq!(
            sample(boundary, &random, &mut |_, _| values.next().unwrap()).unwrap(),
            at("2025-05-17 03:15")
        );
        let random = [None, None, None, Some((2, 2)), None];
        assert_eq!(
            sample(at("2025-03-31 23:31"), &random, &mut |_, _| 2).unwrap(),
            at("2025-02-28 23:31")
        );
        let random = [None, None, Some((31, 31)), None, None];
        assert_eq!(
            sample(at("2025-02-15 10:00"), &random, &mut |_, _| 31).unwrap(),
            at("2025-02-28 10:00")
        );
        let random = [None, None, None, None, Some((2, 4))];
        assert_eq!(
            sample(at("2025-05-19 12:00"), &random, &mut |_, _| 4).unwrap(),
            at("2025-05-15 12:00")
        );
    }

    #[test]
    fn five_fields_and_random_bounds_are_required_and_sunday_uses_zero() {
        let now = at("2025-05-17 00:00");
        for invalid in [
            "* * * *",
            "* * * * * *",
            "?(20-10) * * * *",
            "?(0-60) * * * *",
        ] {
            assert!(ScheduledCommand::new("test".into(), invalid.into(), now).is_err());
        }
        let mut job = ScheduledCommand::new("test".into(), "? ? * * 0".into(), now).unwrap();
        assert_eq!(job.due_at().weekday().num_days_from_sunday(), 0);
        assert!(job.should_run(job.due_at()).unwrap());
        assert!(job.to_string().contains("executed=True"));
        assert!(job.to_string().contains("command=test, schedule=? ? * * 0"));
    }
}
