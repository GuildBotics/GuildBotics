//! Members: their identity, which every workspace on the machine shares, and
//! their participation in a workspace, each in its own file.
use super::*;
use gb_base::shared_writes::Writer;
use gb_config::repository::ConfigSnapshot;
use gb_config::team::{
    PrefixConflict, person_secret_prefix, valid_member_directory_name, valid_person_id,
};
use std::cell::Cell;
use std::collections::BTreeSet;

/// What a member joining a workspace with a ticket service runs on patrol
/// unless its form says otherwise.
pub const TICKET_WORKFLOW_COMMAND: &str = "workflows/ticket_driven_workflow";
/// The largest picture a member may have.
pub const AVATAR_LIMIT: usize = 4 * 1024 * 1024;
const AVATAR_SUFFIXES: [&str; 5] = [".png", ".jpg", ".jpeg", ".gif", ".webp"];

/// The member's file `file` (`identity` or `member`), relative to the
/// config directory of its root.
pub fn member_path(id: &str, file: &str) -> String {
    format!("team/members/{id}/{file}.yml")
}

fn invalid(reason: &str) -> anyhow::Error {
    InvalidInput(reason.into()).into()
}

fn checked_id(id: &str) -> anyhow::Result<()> {
    if valid_person_id(id) {
        Ok(())
    } else {
        Err(invalid("person_id is not a member ID"))
    }
}

fn checked_name(name: &str) -> anyhow::Result<()> {
    if valid_member_directory_name(name) {
        Ok(())
    } else {
        Err(invalid("not a stored member directory name"))
    }
}

/// A chat channel as the member screen names it, without the `#`.
fn channel_name(reference: &str) -> String {
    reference.trim().trim_start_matches('#').to_owned()
}

/// A channel's participation policy; anything else is the strict one.
fn participation_policy(value: Option<&str>) -> &'static str {
    match value.map(|v| v.trim().to_lowercase()).as_deref() {
        Some("social") => "social",
        Some("muted") => "muted",
        _ => "strict",
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TaskSchedule {
    pub command: String,
    #[serde(default)]
    pub schedules: Vec<String>,
}
impl TaskSchedule {
    /// The schedule as the form means it: trimmed, blank entries dropped,
    /// each a five-field cron expression.
    fn checked(&self) -> Result<TaskSchedule, String> {
        let command = self.command.trim();
        if command.is_empty() {
            return Err("command is required".into());
        }
        let schedules: Vec<String> = self
            .schedules
            .iter()
            .map(|s| s.trim().to_owned())
            .filter(|s| !s.is_empty())
            .collect();
        if schedules.iter().any(|s| s.split_whitespace().count() != 5) {
            return Err("schedule must be a five-field cron expression".into());
        }
        Ok(TaskSchedule {
            command: command.into(),
            schedules,
        })
    }
}

/// The member screen's form.
#[derive(Deserialize)]
pub struct PersonSetupInput {
    pub person_type: String,
    pub person_id: String,
    pub person_name: String,
    pub is_active: bool,
    #[serde(default)]
    pub git_email: String,
    #[serde(default)]
    pub roles: Vec<String>,
    #[serde(default)]
    pub speaking_style: String,
    #[serde(default)]
    pub relationships: String,
    #[serde(default)]
    pub character: Map<String, Value>,
    /// The member's account with each provider, by the provider's name.
    #[serde(default)]
    pub accounts: Map<String, Value>,
    #[serde(default)]
    pub chat_channels: Vec<String>,
    #[serde(default)]
    pub work_stop_notification_channels: Vec<String>,
    #[serde(default)]
    pub chat_channel_participation: BTreeMap<String, String>,
    #[serde(default)]
    pub routine_commands: Vec<String>,
    #[serde(default)]
    pub task_schedules: Vec<TaskSchedule>,
}

impl PersonSetupInput {
    fn human(&self) -> bool {
        self.person_type == "human"
    }

    /// The form as its model reads it, or why the request is wrong.
    fn checked(mut self) -> anyhow::Result<PersonSetupInput> {
        checked_id(&self.person_id)?;
        if self.person_name.is_empty() {
            return Err(invalid("person_name is required"));
        }
        for channels in [
            &mut self.chat_channels,
            &mut self.work_stop_notification_channels,
        ] {
            *channels = channels
                .iter()
                .map(|c| channel_name(c))
                .filter(|c| !c.is_empty())
                .collect();
        }
        self.chat_channel_participation = self
            .chat_channel_participation
            .iter()
            .map(|(channel, policy)| (channel_name(channel), policy.clone()))
            .collect();
        self.task_schedules = self
            .task_schedules
            .iter()
            .map(TaskSchedule::checked)
            .collect::<Result<_, _>>()
            .map_err(|reason| invalid(&reason))?;
        Ok(self)
    }
}

/// A member as the member screen shows it.
#[derive(Debug, Serialize)]
pub struct PersonSnapshot {
    pub person_id: String,
    pub person_name: String,
    pub person_type: String,
    pub is_active: bool,
    pub git_email: String,
    pub roles: Vec<String>,
    pub speaking_style: String,
    pub relationships: String,
    pub character: Map<String, Value>,
    pub accounts: Map<String, Value>,
    pub chat_channels: Vec<String>,
    pub work_stop_notification_channels: Vec<String>,
    pub chat_channel_participation: BTreeMap<String, String>,
    pub routine_commands: Vec<String>,
    pub task_schedules: Vec<TaskSchedule>,
    pub avatar_timestamp: i64,
    /// Whether the workspace has the member yet: false for an identity read
    /// so that it can be added.
    pub participating: bool,
    pub revisions: Revisions,
}

/// A member's identity, whichever workspaces it works in.
#[derive(Debug, Serialize, PartialEq)]
pub struct IdentitySummary {
    pub person_id: String,
    pub name: String,
    pub person_type: String,
    /// Whether this workspace has the member.
    pub participating: bool,
}

/// A member of the workspace, as stored, whether or not it can be loaded.
#[derive(Debug, Serialize, PartialEq)]
pub struct PersonSummary {
    pub person_id: String,
    pub name: String,
    pub person_type: String,
    pub is_active: bool,
    pub roles: Vec<String>,
    /// False for a member whose identity this machine does not have.
    pub identity: bool,
}

/// The secrets a member alone owns, read once; none while they are shared.
struct Owned {
    prefix: Option<String>,
    values: BTreeMap<String, String>,
}

fn strings(value: Option<&Value>) -> BTreeMap<String, String> {
    value
        .and_then(Value::as_object)
        .map(|map| {
            map.iter()
                .map(|(k, v)| {
                    let v = match v {
                        Value::String(s) => s.clone(),
                        Value::Null => String::new(),
                        other => other.to_string(),
                    };
                    (k.clone(), v)
                })
                .collect()
        })
        .unwrap_or_default()
}

/// A member file as the screens edit it: what it holds, or nothing -- a
/// member may be missing one of its files, or hold one that does not parse,
/// and still be repaired.
fn lenient(snapshot: Option<&ConfigSnapshot>) -> Map<String, Value> {
    match snapshot.map(ConfigSnapshot::value) {
        Some(Ok(Value::Object(map))) => map,
        _ => Map::new(),
    }
}

fn object(value: Option<&Value>) -> Map<String, Value> {
    value
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

/// The member's picture in its identity directory, if it has one.
pub fn avatar_in(identity_root: &Path, name: &str) -> Option<PathBuf> {
    let directory = identity_root
        .join(".guildbotics/config/team/members")
        .join(name);
    AVATAR_SUFFIXES
        .iter()
        .map(|suffix| directory.join(format!("avatar{suffix}")))
        .find(|path| path.is_file())
}

fn mtime(path: &Path) -> i64 {
    fs::metadata(path)
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

impl Setup<'_> {
    fn identity_root(&self) -> &Path {
        self.repository.identity_root()
    }

    /// The prefix of `id`'s own secrets, as a save refuses an ID sharing it.
    fn target_prefix(&self, id: &str) -> anyhow::Result<String> {
        person_secret_prefix(self.identity_root(), id).map_err(|error| {
            match error.downcast::<PrefixConflict>() {
                Ok(conflict) => SetupError::new(
                    "person_env_prefix_conflict",
                    "member_config.target_prefix_conflict",
                )
                .with("person_id", id)
                .with("members", conflict.conflicting.join(", "))
                .into(),
                Err(error) => error,
            }
        })
    }

    /// `id`'s own secrets: read from the identities' store only when `read`
    /// asks for them and nobody else shares them.
    fn owned(&self, id: &str, read: bool) -> anyhow::Result<Owned> {
        let prefix =
            match valid_person_id(id).then(|| person_secret_prefix(self.identity_root(), id)) {
                Some(Ok(prefix)) => Some(prefix),
                Some(Err(error)) if error.is::<PrefixConflict>() => None,
                Some(Err(error)) => return Err(error),
                None => None,
            };
        let mut values = BTreeMap::new();
        if let Some(prefix) = prefix.as_ref().filter(|_| read) {
            let store = secrets(self.repository.identity_secrets(self.device.clone()))?;
            let held = secrets(store.values())?;
            for name in self.providers.member_secret_names() {
                if let Some(value) = held.get(&format!("{prefix}{name}")) {
                    values.insert(name, value.clone());
                }
            }
        }
        Ok(Owned { prefix, values })
    }

    /// The saved member `id`, as its accounts see it.
    fn saved_member(&self, id: &str) -> anyhow::Result<SavedMember> {
        let identity = mapping(
            &self.repository.identity_view(),
            &member_path(id, "identity"),
        )?;
        let owned = self.owned(id, !self.providers.member_providers().is_empty())?;
        Ok(SavedMember {
            person_id: id.into(),
            name: text(&identity, "name"),
            human: text(&identity, "person_type") == "human",
            account_info: strings(identity.get("account_info")),
            secrets: owned.values,
            credentials: BTreeMap::new(),
            avatar: avatar_in(self.identity_root(), id),
        })
    }

    /// Every identity on the machine, and whether this workspace has it.
    pub fn list_identities(&self) -> anyhow::Result<Vec<IdentitySummary>> {
        let identities = self.repository.identity_view();
        let view = self.repository.view();
        (|| {
            let mut found = Vec::new();
            for name in directories(&identities, "identity")? {
                if !valid_person_id(&name) {
                    continue;
                }
                let identity = mapping(&identities, &member_path(&name, "identity"))?;
                found.push(IdentitySummary {
                    participating: view.read_config(&member_path(&name, "member"))?.is_some(),
                    name: text(&identity, "name"),
                    person_type: text(&identity, "person_type"),
                    person_id: name,
                });
            }
            Ok(found)
        })()
    }

    /// The workspace's members as stored, including those that cannot be
    /// loaded, so that they can be repaired or removed.
    pub fn list_members(&self) -> anyhow::Result<Vec<PersonSummary>> {
        let identities = self.repository.identity_view();
        let view = self.repository.view();
        (|| {
            let mut found = Vec::new();
            for name in directories(view, "member")? {
                let participation = mapping(view, &member_path(&name, "member"))?;
                let identity_path = member_path(&name, "identity");
                let identity = mapping(&identities, &identity_path)?;
                found.push(PersonSummary {
                    name: text(&identity, "name"),
                    person_type: text(&identity, "person_type"),
                    is_active: participation.get("is_active") == Some(&Value::Bool(true)),
                    roles: object(object(participation.get("profile")).get("roles"))
                        .keys()
                        .cloned()
                        .collect(),
                    identity: identities.read_config(&identity_path)?.is_some(),
                    person_id: name,
                });
            }
            Ok(found)
        })()
    }

    /// A member: its identity, and its participation in this workspace (the
    /// defaults when the workspace does not have it yet), with the revisions
    /// of both, read in one span. `name` may be a directory awaiting repair.
    pub fn read_member(&self, name: &str) -> anyhow::Result<PersonSnapshot> {
        checked_name(name)?;
        let identities = self.repository.identity_view();
        let (identity_path, member_file) =
            (member_path(name, "identity"), member_path(name, "member"));
        let owned = self.owned(name, !self.providers.member_providers().is_empty())?;
        let view = self.repository.view();
        (|| {
            let identity_file = identities.read_config(&identity_path)?;
            let member_snapshot = view.read_config(&member_file)?;
            let participating = member_snapshot.is_some();
            if !participating && identity_file.is_none() {
                return Err(SetupError::not_found().into());
            }
            let identity = lenient(identity_file.as_ref());
            let participation = lenient(member_snapshot.as_ref());
            let account_info = strings(identity.get("account_info"));
            let has = |secret: &str| owned.values.get(secret).is_some_and(|v| !v.is_empty());
            let accounts = self
                .providers
                .member_providers()
                .into_iter()
                .map(|provider| {
                    let view = self.providers.view(&provider, &account_info, &has);
                    (provider, view)
                })
                .collect();
            let (mut channels, mut notified, mut policies) = (vec![], vec![], BTreeMap::new());
            for channel in participation
                .get("message_channels")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(Value::as_object)
            {
                let name = text(channel, "name").trim().to_owned();
                if name.is_empty() {
                    continue;
                }
                let chat = object(channel.get("chat"));
                if chat.get("notify_work_stops") == Some(&Value::Bool(true)) {
                    notified.push(channel_name(&name));
                }
                let policy = chat.get("participation").and_then(Value::as_str);
                policies.insert(name.clone(), participation_policy(policy).to_owned());
                channels.push(name);
            }
            let routine_commands = participation
                .get("routine_commands")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .map(|c| match c {
                    Value::String(s) => s.trim().to_owned(),
                    other => other.to_string(),
                })
                .filter(|c| !c.is_empty())
                .collect();
            let task_schedules = participation
                .get("task_schedules")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(|s| serde_json::from_value::<TaskSchedule>(s.clone()).ok())
                .filter_map(|s| s.checked().ok())
                .collect();
            let revision = |snapshot: &Option<ConfigSnapshot>| {
                snapshot
                    .as_ref()
                    .map(|s| s.blob_id.clone())
                    .unwrap_or_default()
            };
            let mut revisions = Revisions::new();
            if valid_person_id(name) {
                revisions.insert(identity_path.clone(), revision(&identity_file));
            }
            revisions.insert(member_file.clone(), revision(&member_snapshot));
            Ok(PersonSnapshot {
                person_id: name.into(),
                person_name: text(&identity, "name"),
                person_type: text(&identity, "person_type"),
                is_active: participation.get("is_active") == Some(&Value::Bool(true)),
                git_email: account_info.get("git_email").cloned().unwrap_or_default(),
                roles: object(object(participation.get("profile")).get("roles"))
                    .keys()
                    .cloned()
                    .collect(),
                speaking_style: text(&identity, "speaking_style"),
                relationships: text(&identity, "relationships"),
                character: object(object(identity.get("profile")).get("character")),
                accounts,
                chat_channels: channels,
                work_stop_notification_channels: notified,
                chat_channel_participation: policies,
                routine_commands,
                task_schedules,
                avatar_timestamp: avatar_in(self.identity_root(), name)
                    .map(|path| mtime(&path))
                    .unwrap_or(0),
                participating,
                revisions,
            })
        })()
    }

    /// Saves the member `id` at the revisions its form was read at. Each
    /// account first obtains from its provider what the save needs, outside
    /// every lock; then the secrets, the identity and the participation are
    /// written in one span; and each account is told how the save ended:
    /// refused when nothing was written, saved when it was. A save that
    /// failed after it began writing settles nothing.
    pub async fn save_member(
        &self,
        id: &str,
        input: PersonSetupInput,
        expected: &Revisions,
    ) -> anyhow::Result<ConfigWriteReceipt<()>> {
        if input.person_id != id {
            return Err(
                SetupError::new("person_id_mismatch", "app_api.errors.person_id_mismatch").into(),
            );
        }
        let input = input.checked()?;
        let mut accounts = Vec::new();
        for (provider, form) in &input.accounts {
            accounts.push(self.providers.account(provider, form)?);
        }
        let began = Cell::new(false);
        let saved = self
            .prepare_and_save(&input, &mut accounts, expected, &began)
            .await;
        if saved.is_ok() || !began.get() {
            for account in accounts {
                account.settled(saved.is_ok()).await;
            }
        }
        saved
    }

    async fn prepare_and_save(
        &self,
        input: &PersonSetupInput,
        accounts: &mut [Box<dyn MemberAccount>],
        expected: &Revisions,
        began: &Cell<bool>,
    ) -> anyhow::Result<ConfigWriteReceipt<()>> {
        let id = input.person_id.as_str();
        let (account_info, form_secrets) =
            self.account_info(&self.repository.identity_view(), input, accounts)?;
        let mut member = SavedMember {
            person_id: id.into(),
            name: input.person_name.clone(),
            human: input.human(),
            account_info: strings(Some(&Value::Object(account_info))),
            secrets: if accounts.is_empty() {
                BTreeMap::new()
            } else {
                self.owned(id, true)?.values
            },
            credentials: BTreeMap::new(),
            avatar: avatar_in(self.identity_root(), id),
        };
        member
            .secrets
            .extend(form_secrets.into_iter().filter(|(_, v)| !v.is_empty()));
        member.credentials = self
            .providers
            .member_providers()
            .into_iter()
            .filter_map(|provider| {
                let credential = self.providers.credential(&provider, &member)?;
                Some((provider, credential))
            })
            .collect();
        for account in accounts.iter_mut() {
            account.prepare(&member).await?;
        }
        let identity_path = member_path(id, "identity");
        let member_file = member_path(id, "member");
        self.repository.write_checked(
            expected,
            &[&identity_path],
            |view, writer| {
                began.set(true);
                self.write_member(view, writer, input, accounts)
            },
            |view| {
                let mut revisions = self
                    .repository
                    .identity_view()
                    .revisions(&[&identity_path])?;
                revisions.extend(view.revisions(&[&member_file])?);
                Ok(revisions)
            },
        )
    }

    /// The secrets first, so a store failure leaves no member claiming
    /// credentials it does not have; then the identity and the participation.
    fn write_member(
        &self,
        view: &ConfigView,
        writer: &mut Writer<'_>,
        input: &PersonSetupInput,
        accounts: &[Box<dyn MemberAccount>],
    ) -> anyhow::Result<()> {
        let id = input.person_id.as_str();
        let prefix = self.target_prefix(id)?;
        let identities = self.repository.identity_view();
        let (account_info, secrets_of) = self.account_info(&identities, input, accounts)?;
        let names = self.providers.member_secret_names();
        anyhow::ensure!(
            secrets_of.keys().all(|key| names.contains(key)),
            "a provider returned an undeclared secret name"
        );
        let identity_file = identities.path(&member_path(id, "identity"))?;
        let member_file = view.path(&member_path(id, "member"))?;
        if !secrets_of.is_empty() {
            let store = secrets(self.repository.identity_secrets(self.device.clone()))?;
            let values: BTreeMap<_, _> = secrets_of
                .into_iter()
                .map(|(name, value)| (format!("{prefix}{name}"), value))
                .collect();
            secrets(store.update_with(writer, |current| current.extend(values)))?;
        }
        let joining = view.read_config(&member_path(id, "member"))?.is_none();
        let participation = self.participation(view, input, joining)?;
        writer.write_json(&identity_file, &clean(identity(input, account_info)))?;
        writer.write_json(&member_file, &clean(participation))
    }

    /// The `account_info` a save stores, with the secrets its accounts store:
    /// what the accounts the form sends store, over the stored entries none
    /// of them replaces. The form cannot edit those, so they are kept; its
    /// git name and e-mail are the form's own.
    fn account_info(
        &self,
        identities: &ConfigView,
        input: &PersonSetupInput,
        accounts: &[Box<dyn MemberAccount>],
    ) -> anyhow::Result<(Map<String, Value>, BTreeMap<String, String>)> {
        let id = input.person_id.as_str();
        let (info, secrets) = stored(accounts, id, input.human())?;
        let replaced: BTreeSet<String> = input
            .accounts
            .keys()
            .flat_map(|provider| self.providers.account_info_keys(provider))
            .chain(["git_user".into(), "git_email".into()])
            .collect();
        let identity = lenient(
            identities
                .read_config(&member_path(id, "identity"))?
                .as_ref(),
        );
        let mut kept: Map<String, Value> = object(identity.get("account_info"))
            .into_iter()
            .filter(|(key, _)| !replaced.contains(key))
            .collect();
        kept.extend(info.into_iter().map(|(k, v)| (k, Value::String(v))));
        Ok((kept, secrets))
    }

    fn participation(
        &self,
        view: &ConfigView,
        input: &PersonSetupInput,
        joining: bool,
    ) -> anyhow::Result<Value> {
        let human = input.human();
        let roles: Map<String, Value> = input
            .roles
            .iter()
            .map(|role| (role.trim_end_matches(':').to_owned(), json!({})))
            .collect();
        let mut participation = json!({
            "is_active": !human && input.is_active,
            "profile": {"roles": roles},
        });
        let services = Value::Object(object(
            mapping(view, PROJECT_CONFIG_PATHS[0])?.get("services"),
        ));
        if !human && !input.chat_channels.is_empty() {
            let mut channels = Vec::new();
            for channel in &input.chat_channels {
                let chat = self.providers.channel(&services, channel)?;
                let policy = input
                    .chat_channel_participation
                    .get(channel)
                    .map(String::as_str);
                let mut settings = json!({
                    "enabled": true,
                    "participation": participation_policy(policy),
                    "notify_work_stops": input.work_stop_notification_channels.contains(channel),
                });
                settings.as_object_mut().unwrap().extend(chat.clone());
                channels.push(json!({
                    "name": channel,
                    "used_as": ["internal_communication"],
                    "used_by": [input.person_id],
                    "channel_info": chat.get("channel_id").map(|id| json!({"channel_id": id})).unwrap_or(json!({})),
                    "chat": settings,
                }));
            }
            participation["message_channels"] = json!(channels);
        }
        let mut routine: Vec<String> = if human {
            vec![]
        } else {
            input
                .routine_commands
                .iter()
                .map(|c| c.trim().to_owned())
                .filter(|c| !c.is_empty())
                .collect()
        };
        if joining && routine.is_empty() && !human && services.get("ticket_service").is_some() {
            routine.push(TICKET_WORKFLOW_COMMAND.into());
        }
        if !routine.is_empty() {
            participation["routine_commands"] = json!(routine);
        }
        let schedules: Vec<_> = if human {
            vec![]
        } else {
            input
                .task_schedules
                .iter()
                .filter(|s| !s.schedules.is_empty())
                .collect()
        };
        if !schedules.is_empty() {
            participation["task_schedules"] = json!(schedules);
        }
        Ok(participation)
    }

    /// Takes the member `name` out of the workspace: its whole directory, and
    /// the project's default when it names it. Its identity stays for the
    /// other workspaces.
    pub fn remove_member(&self, name: &str) -> anyhow::Result<ConfigWriteReceipt<()>> {
        checked_name(name)?;
        let member_file = member_path(name, "member");
        self.repository.write(
            &Revisions::new(),
            |view, writer| {
                if !directories(view, "member")?
                    .iter()
                    .any(|stored| stored == name)
                {
                    return Err(SetupError::not_found().into());
                }
                let mut project = mapping(view, PROJECT_CONFIG_PATHS[0])?;
                writer.remove_tree(&view.path(&format!("team/members/{name}"))?)?;
                if text(&project, "default_person_id") == name {
                    project.remove("default_person_id");
                    writer.write_json(
                        &view.path(PROJECT_CONFIG_PATHS[0])?,
                        &clean(Value::Object(project)),
                    )?;
                }
                Ok(())
            },
            |view| view.revisions(&[&member_file]),
        )
    }

    /// Deletes the member `id` everywhere: each provider first undoes what
    /// saving it obtained, and a refusal keeps it; then it leaves this
    /// workspace; then its secrets go, so a store failure leaves the
    /// identity to retry with, and its identity directory, picture included.
    pub async fn delete_identity(&self, id: &str) -> anyhow::Result<ConfigWriteReceipt<()>> {
        checked_id(id)?;
        let member = self.saved_member(id)?;
        for provider in self.providers.member_providers() {
            self.providers.removed(&provider, &member).await?;
        }
        let mut revisions = Revisions::new();
        let member_file = member_path(id, "member");
        if self.repository.read_config(&member_file)?.is_some() {
            revisions.extend(self.remove_member(id)?.revisions);
        }
        let identity_path = member_path(id, "identity");
        let owned = self.owned(id, false)?;
        let store = secrets(self.repository.identity_secrets(self.device.clone()))?;
        let receipt = self.repository.write_checked(
            &Revisions::new(),
            &[&identity_path],
            |_, writer| {
                let identities = self.repository.identity_view();
                if identities.read_config(&identity_path)?.is_none() {
                    return Err(SetupError::not_found().into());
                }
                let directory = identities.path(&format!("team/members/{id}"))?;
                writer.check(&directory)?;
                if let Some(prefix) = owned.prefix {
                    let keys: Vec<_> = self
                        .providers
                        .member_secret_names()
                        .iter()
                        .map(|name| format!("{prefix}{name}"))
                        .collect();
                    secrets(store.delete_many_with(writer, &keys))?;
                }
                writer.remove_tree(&directory)
            },
            |_| self.repository.identity_view().revisions(&[&identity_path]),
        )?;
        revisions.extend(receipt.revisions);
        Ok(ConfigWriteReceipt {
            result: (),
            revisions,
        })
    }

    /// Replaces the member's picture with `bytes`, named by `filename`'s
    /// suffix (PNG for an unknown one), then hands it to each provider. A
    /// provider's refusal is reported, and the picture stays changed.
    pub async fn change_avatar(
        &self,
        id: &str,
        bytes: &[u8],
        filename: &str,
    ) -> anyhow::Result<i64> {
        checked_id(id)?;
        if bytes.len() > AVATAR_LIMIT {
            return Err(SetupError::new("avatar_invalid", "setup.avatar_too_large").into());
        }
        let lower = filename.to_lowercase();
        let suffix = AVATAR_SUFFIXES
            .iter()
            .find(|suffix| lower.ends_with(*suffix))
            .copied()
            .unwrap_or(".png");
        let identity_path = member_path(id, "identity");
        self.repository.write_checked(
            &Revisions::new(),
            &[&identity_path],
            |_, writer| {
                let identities = self.repository.identity_view();
                let directory = identities.path(&format!("team/members/{id}"))?;
                for entry in fs::read_dir(&directory).into_iter().flatten().flatten() {
                    let name = entry.file_name().to_string_lossy().to_lowercase();
                    if AVATAR_SUFFIXES.iter().any(|s| name == format!("avatar{s}")) {
                        // A picture that cannot be removed is left for the new one.
                        if let Err(error) = writer.delete(&entry.path()) {
                            tracing::warn!(
                                "cannot remove the old avatar {}: {error:#}",
                                entry.path().display()
                            );
                        }
                    }
                }
                let path = identities.path(&format!("team/members/{id}/avatar{suffix}"))?;
                writer.write_bytes(&path, bytes)
            },
            |_| Ok(Revisions::new()),
        )?;
        let timestamp = avatar_in(self.identity_root(), id)
            .map(|path| mtime(&path))
            .unwrap_or(0);
        let member = self.saved_member(id)?;
        for provider in self.providers.member_providers() {
            self.providers.avatar_changed(&provider, &member).await?;
        }
        Ok(timestamp)
    }
}

/// The member directories under `team/members` of `view` that hold the
/// member's file `file`, under a name that can address them.
fn directories(view: &ConfigView, file: &str) -> anyhow::Result<Vec<String>> {
    let members = view.path("team/members")?;
    let entries = match fs::read_dir(&members) {
        Ok(entries) => entries,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(e.into()),
    };
    let mut names = Vec::new();
    for entry in entries {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if entry.file_type()?.is_dir()
            && valid_member_directory_name(&name)
            && entry.path().join(format!("{file}.yml")).is_file()
        {
            names.push(name);
        }
    }
    names.sort();
    Ok(names)
}

/// The `account_info` entries and the secrets the member's accounts store.
fn stored(
    accounts: &[Box<dyn MemberAccount>],
    id: &str,
    human: bool,
) -> anyhow::Result<(BTreeMap<String, String>, BTreeMap<String, String>)> {
    let (mut info, mut values) = (BTreeMap::new(), BTreeMap::new());
    for account in accounts {
        let stored = account.stored(id, human)?;
        info.extend(stored.account_info);
        values.extend(stored.secrets);
    }
    Ok((info, values))
}

/// The member's identity, with the `account_info` its accounts store over
/// its git name and e-mail.
fn identity(input: &PersonSetupInput, account_info: Map<String, Value>) -> Value {
    let human = input.human();
    let mut info = Map::new();
    info.insert("git_user".into(), input.person_name.clone().into());
    info.insert("git_email".into(), input.git_email.clone().into());
    info.extend(account_info);
    let mut identity = json!({
        "person_id": input.person_id,
        "name": input.person_name,
        "person_type": input.person_type,
        "account_info": info,
        "speaking_style": if human { "" } else { input.speaking_style.as_str() },
        "relationships": if human { "" } else { input.relationships.as_str() },
    });
    if !human && !input.character.is_empty() {
        identity["profile"] = json!({"character": input.character});
    }
    identity
}
