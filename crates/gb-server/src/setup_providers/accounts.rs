//! The Branvil member account and the connection shared by all workspaces.
use super::*;
use branvil::api::{DataApi, MemberApi, MemberKind, members};
use gb_base::shared_writes::SharedWrites;
use gb_config::secrets::{DeviceKey, Scope, SecretStore};

pub(crate) fn message(e: branvil::api::AppError, lang: &str) -> String {
    if e.message.starts_with("integrations.branvil.") {
        crate::i18n::t(lang, &e.message, &[])
    } else {
        e.to_string()
    }
}
pub(crate) fn refused(e: branvil::api::AppError) -> SetupError {
    if e.message.starts_with("integrations.branvil.") {
        SetupError::new("branvil", &e.message)
    } else {
        SetupError::text("branvil", e.to_string())
    }
}
fn unavailable() -> SetupError {
    SetupError::new("branvil", "integrations.branvil.api.not_connected")
}
impl Registry {
    pub(crate) async fn admin(&self) -> Result<Option<(DataApi, MemberApi)>, SetupError> {
        let Some(api) = &self.0 else { return Ok(None) };
        let path = api
            .identity_root
            .join(".guildbotics/config/connections/branvil.yml");
        let stored = match std::fs::read_to_string(path) {
            Ok(text) => gb_config::yaml_value(&text).map_err(|_| unavailable())?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(_) => return Err(unavailable()),
        };
        if stored["base_url"].as_str().unwrap_or("").is_empty() {
            return Ok(None);
        }
        let data = api.branvil.get().cloned().ok_or_else(unavailable)?;
        if stored["base_url"] != data.base_url() {
            return Err(unavailable());
        }
        let mut writes = SharedWrites::new(api.identity_root.clone(), None);
        writes.timeout = api.write_timeout;
        let store = SecretStore::new(
            &writes,
            Scope::Identity,
            DeviceKey {
                machine_root: api.data.parent().unwrap_or(&api.data).to_path_buf(),
                keychain: &*api.keychain,
            },
        )
        .map_err(|_| unavailable())?;
        let token = store
            .get("BRANVIL_ADMIN_TOKEN")
            .map_err(|_| unavailable())?
            .filter(|s| !s.is_empty())
            .ok_or_else(unavailable)?;
        let admin = data.authenticate(token).await.map_err(|e| {
            if e.status.as_u16() == 401 {
                SetupError::new("branvil", "integrations.branvil.api.admin_refused")
            } else {
                refused(e)
            }
        })?;
        if admin.identity().await.map_err(refused)?.kind != MemberKind::Owner {
            return Err(SetupError::new(
                "branvil",
                "integrations.branvil.api.admin_refused",
            ));
        }
        Ok(Some((data, admin)))
    }
    pub(super) async fn remove_branvil(&self, member: &SavedMember) -> Result<(), SetupError> {
        let name = member
            .account_info
            .get("branvil_username")
            .map(String::as_str)
            .unwrap_or("");
        if member.human || name.is_empty() {
            return Ok(());
        }
        let Some((_, admin)) = self.admin().await? else {
            return Ok(());
        };
        let mut cursor = None;
        let mut ids = Vec::new();
        loop {
            let page = admin
                .page(format!("setup-tokens:{name}"), Some(100), cursor.as_deref())
                .map_err(refused)?;
            let page = match admin.members_list_tokens(name.into(), page).await {
                Ok(page) => page,
                Err(e) if e.status.as_u16() == 404 => return Ok(()),
                Err(e) => return Err(refused(e)),
            };
            ids.extend(
                page.items
                    .into_iter()
                    .filter(|t| t.label == "guildbotics")
                    .map(|t| t.id),
            );
            cursor = page.next_cursor;
            if cursor.is_none() {
                break;
            }
        }
        for id in ids {
            delete_token(&admin, name, &id).await?;
        }
        Ok(())
    }
}
pub(super) async fn picture(
    admin: &MemberApi,
    name: &str,
    member: &SavedMember,
) -> Result<(), SetupError> {
    if name.is_empty() {
        return Ok(());
    }
    if let Some(path) = &member.avatar {
        let bytes = std::fs::read(path).map_err(|_| {
            SetupError::new("avatar_save_failed", "app_api.errors.avatar_save_failed")
        })?;
        admin
            .members_set_avatar(name.into(), Some(bytes))
            .await
            .map_err(refused)?;
    }
    Ok(())
}
async fn delete_token(admin: &MemberApi, name: &str, id: &str) -> Result<(), SetupError> {
    match admin.members_revoke_token(name.into(), id.into()).await {
        Ok(()) => Ok(()),
        Err(e) if e.status.as_u16() == 404 => Ok(()),
        Err(e) => Err(refused(e)),
    }
}
struct Issued {
    name: String,
    id: String,
    token: String,
    replaced: String,
}
#[derive(Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct BranvilAccount {
    #[serde(default)]
    pub username: String,
    #[serde(default)]
    reissue: bool,
    #[serde(skip)]
    pub registry: Registry,
    #[serde(skip)]
    issued: Option<Issued>,
}
impl MemberAccount for BranvilAccount {
    fn prepare<'a>(&'a mut self, member: &'a SavedMember) -> Hook<'a> {
        Box::pin(async move {
            let Some((data, admin)) = self.registry.admin().await? else {
                return Ok(());
            };
            if member.human {
                if self.username.is_empty() {
                    self.username = admin.identity().await.map_err(refused)?.name;
                }
                picture(&admin, &self.username, member).await?;
                return Ok(());
            }
            if self.username.is_empty() {
                self.username = member.person_id.clone();
            }
            let found = match admin.members_get(self.username.clone()).await {
                Ok(found) => found,
                Err(e) if e.status.as_u16() == 404 => admin
                    .members_create(members::NewMember {
                        name: self.username.clone(),
                        display_name: Some(member.name.clone()),
                        kind: MemberKind::Agent,
                    })
                    .await
                    .map_err(refused)?,
                Err(e) => return Err(refused(e)),
            };
            if found.kind != MemberKind::Agent {
                return Err(
                    SetupError::new("branvil", "integrations.branvil.setup.not_agent")
                        .with("name", &self.username),
                );
            }
            let accepted = if member.secret("BRANVIL_TOKEN").is_empty() {
                false
            } else {
                match data
                    .authenticate(member.secret("BRANVIL_TOKEN").into())
                    .await
                {
                    Ok(m) => m
                        .identity()
                        .await
                        .map_err(refused)?
                        .name
                        .eq_ignore_ascii_case(&self.username),
                    Err(e) if e.status.as_u16() == 401 => false,
                    Err(e) => return Err(refused(e)),
                }
            };
            if self.reissue || !accepted {
                let issued = admin
                    .members_issue_token(
                        self.username.clone(),
                        members::NewToken {
                            label: "guildbotics".into(),
                            replace: false,
                        },
                    )
                    .await
                    .map_err(refused)?;
                self.issued = Some(Issued {
                    name: self.username.clone(),
                    token: issued.token,
                    id: issued.id,
                    replaced: member.secret("BRANVIL_TOKEN_ID").into(),
                });
            }
            picture(&admin, &self.username, member).await?;
            Ok(())
        })
    }
    fn stored(&self, _: &str, _: bool) -> Result<StoredAccount, SetupError> {
        let mut stored = StoredAccount::default();
        if !self.username.is_empty() {
            stored
                .account_info
                .insert("branvil_username".into(), self.username.clone());
        }
        if let Some(issued) = &self.issued {
            stored
                .secrets
                .insert("BRANVIL_TOKEN".into(), issued.token.clone());
            stored
                .secrets
                .insert("BRANVIL_TOKEN_ID".into(), issued.id.clone());
        }
        Ok(stored)
    }
    fn settled(self: Box<Self>, saved: bool) -> std::pin::Pin<Box<dyn Future<Output = ()> + Send>> {
        Box::pin(async move {
            if let Some(issued) = self.issued {
                let id = if saved { &issued.replaced } else { &issued.id };
                if !id.is_empty() {
                    let result = async {
                        let (_, admin) = self.registry.admin().await?.ok_or_else(unavailable)?;
                        delete_token(&admin, &issued.name, id).await
                    }
                    .await;
                    if result.is_err() {
                        tracing::warn!("Branvil could not revoke a setup token");
                    }
                }
            }
        })
    }
}
