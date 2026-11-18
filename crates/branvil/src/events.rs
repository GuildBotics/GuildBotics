//! Change notifications for the GUI. A bounded broadcast channel: events only
//! say what changed, and a client that falls behind gets `event: reset` and
//! refetches. Every subscription ends when the service starts shutting down,
//! so the long-lived event streams never hold a graceful shutdown open.

pub use crate::model::MemberKind;
use futures_util::Stream;
use serde::Serialize;
use tokio::sync::{broadcast, watch};

const CAPACITY: usize = 256;

#[derive(Serialize, Clone, Debug)]
pub struct Change {
    pub kind: &'static str,
    pub path: String,
    pub id: String,
    pub actor: Option<MemberKind>,
}

/// What a subscriber receives.
pub enum Notice {
    Change(Change),
    /// Changes were missed; refetch everything.
    Reset,
}

#[derive(Clone)]
pub struct Events {
    tx: broadcast::Sender<Change>,
    closed: watch::Sender<bool>,
}

impl Default for Events {
    fn default() -> Self {
        Self::new()
    }
}

impl Events {
    pub fn new() -> Self {
        let (tx, _) = broadcast::channel(CAPACITY);
        let (closed, _) = watch::channel(false);
        Self { tx, closed }
    }

    pub fn publish(&self, kind: &'static str, path: &str, id: impl Into<String>) {
        self.publish_as(kind, path, id, None);
    }
    pub fn publish_as(
        &self,
        kind: &'static str,
        path: &str,
        id: impl Into<String>,
        actor: Option<MemberKind>,
    ) {
        // No subscribers is not an error.
        let _ = self.tx.send(Change {
            kind,
            path: path.to_string(),
            id: id.into(),
            actor,
        });
    }

    /// Ends every subscription, present and future.
    pub fn close(&self) {
        self.closed.send_replace(true);
    }

    /// The changes published from now on, until [`Events::close`].
    pub fn subscribe(&self) -> impl Stream<Item = Notice> + use<> {
        let state = (self.tx.subscribe(), self.closed.subscribe());
        futures_util::stream::unfold(state, |(mut rx, mut closed)| async move {
            use broadcast::error::RecvError;
            let notice = tokio::select! {
                _ = closed.wait_for(|c| *c) => return None,
                r = rx.recv() => match r {
                    Ok(change) => Notice::Change(change),
                    Err(RecvError::Lagged(_)) => Notice::Reset,
                    Err(RecvError::Closed) => return None,
                },
            };
            Some((notice, (rx, closed)))
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use futures_util::StreamExt;
    #[tokio::test]
    async fn attribution_reset_and_shutdown_are_preserved() {
        let events = Events::new();
        let stream = events.subscribe();
        tokio::pin!(stream);
        for actor in [Some(MemberKind::Owner), Some(MemberKind::Agent), None] {
            events.publish_as("ticket", "org/repo", "1", actor);
            let Some(Notice::Change(change)) = stream.next().await else {
                panic!("missing change")
            };
            assert_eq!(change.actor, actor);
            assert_eq!(change.path, "org/repo");
            assert_eq!(change.id, "1");
        }
        for _ in 0..CAPACITY + 1 {
            events.publish("check", "org/repo", "sha");
        }
        assert!(matches!(stream.next().await, Some(Notice::Reset)));
        for _ in 0..CAPACITY {
            assert!(matches!(stream.next().await, Some(Notice::Change(_))));
        }
        events.close();
        assert!(stream.next().await.is_none());
        assert!(Box::pin(events.subscribe()).next().await.is_none());
    }
}
