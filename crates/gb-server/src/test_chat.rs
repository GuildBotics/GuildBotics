//! A local provider with faults at the typed port, shared by migration tests.
use gb_ports::*;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
};

pub struct Faults {
    pub blocking: Mutex<
        Option<(
            String,
            tokio_util::sync::CancellationToken,
            tokio_util::sync::CancellationToken,
        )>,
    >,
    pub inner: Arc<dyn ChatService>,
    pub errors: Mutex<BTreeMap<String, ServiceError>>,
    pub calls: Mutex<Vec<Value>>,
}
pub fn runtime(root: &std::path::Path) -> crate::workflow_runtime::Runtime {
    let native = crate::member::native::tests::fixture(root);
    let host = Arc::new(gb_environment::Host {
        data: native.data.clone(),
        home: root.join("home"),
        package: native.package.clone(),
        identity_root: native.identity_root.clone(),
    });
    crate::workflow_runtime::Runtime::assemble(
        &native,
        host,
        "aiko",
        Default::default(),
        Default::default(),
        Default::default(),
    )
    .unwrap()
    .0
}
pub fn local(root: &std::path::Path) -> Arc<dyn ChatService> {
    runtime(root).chat.unwrap()
}
impl Faults {
    pub fn new(inner: Arc<dyn ChatService>) -> Self {
        Self {
            blocking: Default::default(),
            inner,
            errors: Mutex::new(BTreeMap::new()),
            calls: Mutex::new(vec![]),
        }
    }
    fn fault(&self, operation: &str, channel: &str) -> Result<(), ServiceError> {
        self.errors
            .lock()
            .unwrap()
            .get(&format!("{operation}:{channel}"))
            .cloned()
            .map_or(Ok(()), Err)
    }
    async fn wait(&self, operation: &str) {
        let blocked = self.blocking.lock().unwrap().clone();
        if let Some((name, entered, release)) = blocked
            && name == operation
        {
            entered.cancel();
            release.cancelled().await;
        }
    }
}
impl ChatService for Faults {
    fn get_bot_identity(&self) -> ServiceFuture<'_, ChatIdentity> {
        Box::pin(async {
            self.fault("identity", "")?;
            self.inner.get_bot_identity().await
        })
    }
    fn check_credentials(&self) -> ServiceFuture<'_, Vec<CredentialCheck>> {
        self.inner.check_credentials()
    }
    fn self_user_id(&self, account: &ActorName) -> String {
        self.inner.self_user_id(account)
    }
    fn parse_message_url(&self, url: &str) -> Result<ChatMessageRef, ServiceError> {
        self.inner.parse_message_url(url)
    }
    fn mentioned_user_ids(&self, text: &str) -> Vec<String> {
        self.inner.mentioned_user_ids(text)
    }
    fn list_channel_events<'a>(
        &'a self,
        channel: &'a str,
        query: ChannelEventsQuery,
    ) -> ServiceFuture<'a, ChatEventPage> {
        Box::pin(async move {
            self.wait("channel").await;
            self.calls.lock().unwrap().push(json!({"operation":"channel","channel":channel,"since":query.since,"limit":query.limit,"cursor":query.cursor}));
            self.fault("channel", channel)?;
            self.inner.list_channel_events(channel, query).await
        })
    }
    fn list_thread_events<'a>(
        &'a self,
        channel: &'a str,
        thread: &'a str,
        cursor: Option<String>,
        limit: usize,
    ) -> ServiceFuture<'a, ChatEventPage> {
        Box::pin(async move {
            self.calls.lock().unwrap().push(json!({"operation":"thread","channel":channel,"thread":thread,"cursor":cursor,"limit":limit}));
            self.fault("thread", thread)?;
            self.inner
                .list_thread_events(channel, thread, cursor, limit)
                .await
        })
    }
    fn resolve_channel_id<'a>(&'a self, name: &'a str) -> ServiceFuture<'a, Option<String>> {
        Box::pin(async move {
            self.calls
                .lock()
                .unwrap()
                .push(json!({"operation":"resolve","name":name}));
            self.fault("resolve", name)?;
            self.inner.resolve_channel_id(name).await
        })
    }
    fn post_message<'a>(
        &'a self,
        channel: &'a str,
        text: &'a str,
        thread: Option<&'a str>,
        status: Option<&'a WorkflowStatus>,
    ) -> ServiceFuture<'a, ChatPostResult> {
        Box::pin(async move {
            self.calls
                .lock()
                .unwrap()
                .push(json!({"operation":"post","channel":channel}));
            self.fault("post", channel)?;
            self.inner.post_message(channel, text, thread, status).await
        })
    }
    fn add_reaction<'a>(
        &'a self,
        channel: &'a str,
        message: &'a str,
        reaction: SemanticReaction,
    ) -> ServiceFuture<'a, ()> {
        Box::pin(async move {
            self.fault("reaction", channel)?;
            self.inner.add_reaction(channel, message, reaction).await
        })
    }
    fn normalize_participant_text(&self, text: &str, labels: &BTreeMap<String, String>) -> String {
        self.inner.normalize_participant_text(text, labels)
    }
    fn render_participant_text(&self, text: &str, labels: &BTreeMap<String, String>) -> String {
        self.inner.render_participant_text(text, labels)
    }
}
