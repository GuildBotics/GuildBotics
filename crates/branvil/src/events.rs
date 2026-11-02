//! Change notifications for the GUI. A bounded broadcast channel: events only
//! say what changed, and a client that falls behind gets `event: reset` and
//! refetches. Every subscription ends when the service starts shutting down,
//! so the long-lived event streams never hold a graceful shutdown open.

use futures_util::Stream;
use serde::Serialize;
use tokio::sync::{broadcast, watch};

const CAPACITY: usize = 256;

#[derive(Serialize, Clone, Debug)]
pub struct Change {
    pub kind: &'static str,
    pub path: String,
    pub id: String,
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
        // No subscribers is not an error.
        let _ = self.tx.send(Change {
            kind,
            path: path.to_string(),
            id: id.into(),
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
