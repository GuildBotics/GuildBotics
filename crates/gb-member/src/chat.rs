//! The member's chat output, independent of the configured provider.
use crate::{MemberError, Result, WriteGuard};
use gb_ports::*;
use serde_json::{Value, json};
use std::collections::BTreeMap;

pub struct Chat<'a> {
    pub service: &'a dyn ChatService,
    pub service_name: &'a str,
    pub person_id: &'a str,
    pub guard: &'a dyn WriteGuard,
    pub participant_labels: BTreeMap<String, String>,
}
#[derive(Default)]
pub struct Reference<'a> {
    pub channel_id: Option<&'a str>,
    pub channel_name: Option<&'a str>,
    pub message_id: Option<&'a str>,
    pub message_url: Option<&'a str>,
}
impl Chat<'_> {
    pub async fn identity(&self) -> Result<ChatIdentityOutput> {
        let identity = self.service.get_bot_identity().await?;
        Ok(ChatIdentityOutput {
            service: self.service_name.into(),
            user_id: identity.user_id,
            display_name: identity.display_name,
            person_id: self.person_id.into(),
        })
    }
    pub async fn check_credentials(&self) -> Result<Value> {
        let checks = self.service.check_credentials().await?;
        let status = if checks.iter().any(|c| c.status == CredentialStatus::Failed) {
            "failed"
        } else if !checks.is_empty()
            && checks
                .iter()
                .all(|c| c.status == CredentialStatus::Unconfigured)
        {
            "unconfigured"
        } else {
            "ok"
        };
        Ok(
            json!({"service":self.service_name,"status":status,"credentials":checks.into_iter().map(|c| {
            let mut result = json!({"name":c.name,"status":c.status});
            if !c.error.is_empty() { result["error"] = c.error.into(); }
            if !c.error_args.is_empty() {result["error_args"]=serde_json::to_value(c.error_args).expect("string map");}
            result
        }).collect::<Vec<_>>()}),
        )
    }
    pub async fn resolve_channel(&self, name: &str) -> Result<ChatChannelOutput> {
        Ok(ChatChannelOutput {
            service: self.service_name.into(),
            channel_name: name.into(),
            channel_id: self
                .service
                .resolve_channel_id(name)
                .await?
                .unwrap_or_default(),
        })
    }
    async fn channel(&self, reference: &Reference<'_>) -> Result<String> {
        if let Some(id) = reference.channel_id.filter(|id| !id.is_empty()) {
            return Ok(id.into());
        }
        let name = reference
            .channel_name
            .filter(|n| !n.is_empty())
            .ok_or(MemberError::Capability("cli.member.chat_channel_required"))?;
        self.service
            .resolve_channel_id(name)
            .await?
            .filter(|id| !id.is_empty())
            .ok_or_else(|| MemberError::CapabilityArgs {
                key: "cli.member.chat_channel_missing",
                args: [("name".into(), name.into())].into(),
            })
    }
    async fn reference(&self, reference: &Reference<'_>) -> Result<ChatMessageRef> {
        if let Some(url) = reference.message_url.filter(|url| !url.is_empty()) {
            return Ok(self.service.parse_message_url(url)?);
        }
        let id = reference
            .message_id
            .filter(|id| !id.is_empty())
            .ok_or(MemberError::Capability("cli.member.chat_message_required"))?;
        Ok(ChatMessageRef {
            channel_id: self.channel(reference).await?,
            message_id: id.into(),
            thread_id: id.into(),
        })
    }
    pub async fn inspect_channel(
        &self,
        reference: &Reference<'_>,
        query: ChannelEventsQuery,
    ) -> Result<Value> {
        let channel = self.channel(reference).await?;
        let since = query.since.map(|d| d.to_rfc3339()).unwrap_or_default();
        let until = query.until.map(|d| d.to_rfc3339()).unwrap_or_default();
        let page = self.service.list_channel_events(&channel, query).await?;
        Ok(
            json!({"service":self.service_name,"mode":"channel","channel_id":channel,
            "channel_name":reference.channel_name.unwrap_or(""),"since":since,"until":until,
            "next_cursor":page.cursor.unwrap_or_default(),"messages":events(page.events)}),
        )
    }
    pub async fn inspect_thread(&self, reference: &Reference<'_>, limit: usize) -> Result<Value> {
        let target = self.reference(reference).await?;
        let page = self
            .service
            .list_thread_events(&target.channel_id, &target.thread_id, None, limit)
            .await?;
        Ok(
            json!({"service":self.service_name,"mode":"thread","channel_id":target.channel_id,
            "channel_name":reference.channel_name.unwrap_or(""),"thread_id":target.thread_id,
            "next_cursor":page.cursor.unwrap_or_default(),"messages":events(page.events)}),
        )
    }
    pub async fn post(
        &self,
        reference: &Reference<'_>,
        body: &str,
        reply: bool,
    ) -> Result<ChatPostOutput> {
        let (channel, thread) = if reply {
            let r = self.reference(reference).await?;
            (r.channel_id, Some(r.thread_id))
        } else {
            (self.channel(reference).await?, None)
        };
        let rendered = self
            .service
            .render_participant_text(body, &self.participant_labels);
        self.guard.ensure_current()?;
        let result = self
            .service
            .post_message(&channel, &rendered, thread.as_deref(), None)
            .await?;
        Ok(ChatPostOutput {
            service: self.service_name.into(),
            channel_id: result.channel_id,
            message_id: result.message_id,
            thread_id: result.thread_id,
            occurred_at: result.occurred_at.to_rfc3339(),
            text: rendered,
            posted: true,
        })
    }
    pub async fn react(
        &self,
        reference: &Reference<'_>,
        reaction: SemanticReaction,
    ) -> Result<ChatReactionOutput> {
        let target = self.reference(reference).await?;
        self.guard.ensure_current()?;
        self.service
            .add_reaction(&target.channel_id, &target.message_id, reaction)
            .await?;
        Ok(ChatReactionOutput {
            service: self.service_name.into(),
            channel_id: target.channel_id,
            message_id: target.message_id,
            reaction,
            reacted: true,
        })
    }
}
fn events(mut events: Vec<ChatEvent>) -> Vec<Value> {
    events.sort_by_key(|e| e.message.position());
    events.iter().map(ChatEvent::payload).collect()
}
