//! Identity and participation are separate documents, with closed field sets.
use super::*;
use gb_config::secrets::DeviceKey;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct IdentityProfile {
    #[serde(default)]
    pub character: BTreeMap<String, Value>,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Identity {
    pub person_id: String,
    pub name: String,
    pub person_type: String,
    #[serde(default)]
    pub account_info: BTreeMap<String, String>,
    #[serde(default)]
    pub speaking_style: String,
    #[serde(default)]
    pub relationships: String,
    #[serde(default)]
    pub profile: IdentityProfile,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct ParticipationProfile {
    #[serde(default)]
    pub roles: BTreeMap<String, Value>,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TaskSchedule {
    pub command: String,
    pub schedules: Vec<String>,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct Participation {
    #[serde(default)]
    pub is_active: bool,
    #[serde(default)]
    pub profile: ParticipationProfile,
    #[serde(default)]
    pub message_channels: Vec<Value>,
    #[serde(default)]
    pub routine_commands: Vec<String>,
    #[serde(default)]
    pub task_schedules: Vec<TaskSchedule>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MemberInput {
    pub identity: Identity,
    pub participation: Participation,
    #[serde(default)]
    pub accounts: BTreeMap<String, Value>,
}
#[derive(Debug)]
pub struct MemberSnapshot {
    pub identity: Identity,
    pub participation: Participation,
    pub revisions: Revisions,
}
#[derive(Debug)]
pub struct MemberSummary {
    pub person_id: String,
    pub identity: Option<Identity>,
    pub participating: bool,
}

fn prefix(id: &str) -> String {
    format!("{}_", id.replace('-', "_").to_uppercase())
}

fn unique_prefix(view: &ConfigView, id: &str) -> anyhow::Result<String> {
    let members = view.path("team/members")?;
    if members.exists() {
        for entry in fs::read_dir(members)? {
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().into_owned();
            if name != id
                && prefix(&name) == prefix(id)
                && entry.path().join("identity.yml").is_file()
            {
                anyhow::bail!("member secret namespace is shared");
            }
        }
    }
    Ok(prefix(id))
}

impl Setup<'_> {
    /// Includes orphan participation files so they can be removed for repair.
    pub fn list_members(&self, identities: bool) -> anyhow::Result<Vec<MemberSummary>> {
        self.repository
            .write(
                &Revisions::new(),
                |view, _| {
                    let identity_view = self.repository.identity_view();
                    let source = if identities { &identity_view } else { view };
                    let members = source.path("team/members")?;
                    if !members.exists() {
                        return Ok(Vec::new());
                    }
                    let mut result = Vec::new();
                    for entry in fs::read_dir(members)? {
                        let entry = entry?;
                        let id = entry.file_name().to_string_lossy().into_owned();
                        if !valid_person_id(&id) {
                            continue;
                        }
                        let ip = member_path(&id, "identity")?;
                        let mp = member_path(&id, "member")?;
                        let identity = identity_view
                            .read_config(&ip)?
                            .map(|s| -> anyhow::Result<Identity> {
                                Ok(serde_json::from_value(s.value()?)?)
                            })
                            .transpose()?;
                        let participating = view.read_config(&mp)?.is_some();
                        if identity.as_ref().is_some_and(|i| i.person_id != id) {
                            anyhow::bail!("stored person_id cannot change");
                        }
                        if (identities && identity.is_some()) || (!identities && participating) {
                            result.push(MemberSummary {
                                person_id: id,
                                identity,
                                participating,
                            });
                        }
                    }
                    result.sort_by(|a, b| a.person_id.cmp(&b.person_id));
                    Ok(result)
                },
                |_| Ok(Revisions::new()),
            )
            .map(|r| r.result)
    }
    pub fn read_member(&self, id: &str) -> anyhow::Result<MemberSnapshot> {
        let ip = member_path(id, "identity")?;
        let mp = member_path(id, "member")?;
        self.repository
            .write(
                &Revisions::new(),
                |view, _| {
                    let identities = self.repository.identity_view();
                    let identity: Identity = serde_json::from_value(read(&identities, &ip)?)?;
                    ensure!(identity.person_id == id, "person_id cannot change");
                    let mut revisions = identities.revisions(&[&ip])?;
                    revisions.extend(view.revisions(&[&mp])?);
                    Ok(MemberSnapshot {
                        identity,
                        participation: serde_json::from_value(read(view, &mp)?)?,
                        revisions,
                    })
                },
                |_| Ok(Revisions::new()),
            )
            .map(|r| r.result)
    }

    /// `id` is the original route/directory ID, not a mutable field from a form.
    pub fn save_member(
        &self,
        id: &str,
        input: &MemberInput,
        expected: &Revisions,
        device: DeviceKey<'_>,
    ) -> anyhow::Result<ConfigWriteReceipt<()>> {
        let ip = member_path(id, "identity")?;
        let mp = member_path(id, "member")?;
        ensure!(input.identity.person_id == id, "person_id cannot change");
        ensure!(
            !input.identity.name.trim().is_empty(),
            "person_name is required"
        );
        let human = input.identity.person_type == "human";
        ensure!(
            human || input.identity.person_type == "agent",
            "invalid person_type"
        );
        let mut identity = input.identity.clone();
        let mut participation = input.participation.clone();
        let mut secrets = BTreeMap::new();
        for (provider, form) in &input.accounts {
            let stored = self.providers.account(provider, form, id, human)?;
            identity.account_info.extend(stored.account_info);
            ensure!(
                stored
                    .secrets
                    .keys()
                    .all(|key| self.providers.member_secret_names().contains(key)),
                "provider returned an undeclared secret name"
            );
            secrets.extend(stored.secrets);
        }
        identity
            .account_info
            .insert("git_user".into(), identity.name.clone());
        if human {
            identity.speaking_style.clear();
            identity.relationships.clear();
            identity.profile = IdentityProfile::default();
            participation.is_active = false;
            participation.message_channels.clear();
            participation.routine_commands.clear();
            participation.task_schedules.clear();
        }
        for schedule in &participation.task_schedules {
            ensure!(
                !schedule.command.trim().is_empty()
                    && schedule
                        .schedules
                        .iter()
                        .all(|s| s.split_whitespace().count() == 5),
                "invalid task schedule"
            );
        }
        let store = self.repository.identity_secrets(device)?;
        self.repository.write(
            &Revisions::new(),
            |view, writer| {
                let identities = self.repository.identity_view();
                check_revisions(&identities, expected, &[&ip])?;
                check_revisions(view, expected, &[&mp])?;
                if let Some(old) = identities.read_config(&ip)? {
                    ensure!(
                        old.value()?["person_id"] == id,
                        "stored person_id cannot change"
                    );
                }
                let prefix = unique_prefix(&identities, id)?;
                let project = read(view, "team/project.yml")?;
                if !human
                    && view.read_config(&mp)?.is_none()
                    && participation.routine_commands.is_empty()
                    && project["services"].get("ticket_service").is_some()
                {
                    participation
                        .routine_commands
                        .push("workflows/ticket_driven_workflow".into());
                }
                for channel in &mut participation.message_channels {
                    let reference = channel["name"]
                        .as_str()
                        .context("channel name required")?
                        .trim()
                        .trim_start_matches('#');
                    ensure!(!reference.is_empty(), "channel name required");
                    let mut chat = self.providers.channel(&project["services"], reference)?;
                    ensure!(chat.is_object(), "channel model must be a mapping");
                    let policy = channel["chat"]["participation"]
                        .as_str()
                        .unwrap_or("strict")
                        .to_string();
                    ensure!(
                        ["strict", "social", "muted"].contains(&policy.as_str()),
                        "invalid chat participation"
                    );
                    chat["enabled"] = json!(true);
                    chat["participation"] = json!(policy);
                    chat["notify_work_stops"] = json!(channel["chat"]["notify_work_stops"] == true);
                    channel["name"] = json!(reference);
                    channel["used_as"] = json!(["internal_communication"]);
                    channel["used_by"] = json!([id]);
                    channel["channel_info"] = chat
                        .get("channel_id")
                        .map(|id| json!({"channel_id":id}))
                        .unwrap_or(json!({}));
                    channel["chat"] = chat;
                }
                // Validate every path and value before credentials are persisted.
                let identity_path = identities.path(&ip)?;
                let member_path = view.path(&mp)?;
                let values: BTreeMap<_, _> = secrets
                    .iter()
                    .map(|(k, v)| (format!("{prefix}{k}"), v.clone()))
                    .collect();
                if !secrets.is_empty() {
                    store.update_with(writer, |current| current.extend(values))?;
                }
                writer.write_json(&identity_path, &identity)?;
                writer.write_json(&member_path, &participation)
            },
            |view| {
                let mut revisions = self.repository.identity_view().revisions(&[&ip])?;
                revisions.extend(view.revisions(&[&mp])?);
                Ok(revisions)
            },
        )
    }

    pub fn remove_member(
        &self,
        id: &str,
        expected: &Revisions,
    ) -> anyhow::Result<ConfigWriteReceipt<()>> {
        let mp = member_path(id, "member")?;
        self.repository.write(
            &Revisions::new(),
            |view, writer| {
                check_revisions(view, expected, &[&mp, "team/project.yml"])?;
                let mut project = read(view, "team/project.yml")?;
                let project_path = view.path("team/project.yml")?;
                writer.delete(&view.path(&mp)?)?;
                if project["default_person_id"] == id {
                    project.as_object_mut().unwrap().remove("default_person_id");
                    writer.write_json(&project_path, &project)?;
                }
                Ok(())
            },
            |view| view.revisions(&[&mp, "team/project.yml"]),
        )
    }

    pub fn delete_identity(
        &self,
        id: &str,
        expected: &Revisions,
        device: DeviceKey<'_>,
    ) -> anyhow::Result<()> {
        let ip = member_path(id, "identity")?;
        let store = self.repository.identity_secrets(device)?;
        self.repository.write(
            &Revisions::new(),
            |_, writer| {
                let identities = self.repository.identity_view();
                check_revisions(&identities, expected, &[&ip])?;
                ensure!(identities.read_config(&ip)?.is_some(), "identity not found");
                let prefix = unique_prefix(&identities, id)?;
                // A longer member prefix may nest under this one; only delete
                // provider-declared keys rather than every key starting with it.
                let identity: Identity = serde_json::from_value(read(&identities, &ip)?)?;
                ensure!(identity.person_id == id, "stored person_id cannot change");
                let keys: Vec<_> = self
                    .providers
                    .member_secret_names()
                    .iter()
                    .map(|key| format!("{prefix}{key}"))
                    .collect();
                store.delete_many_with(writer, &keys)?;
                writer.delete(&identities.path(&ip)?)
            },
            |_| Ok(Revisions::new()),
        )?;
        Ok(())
    }
}
