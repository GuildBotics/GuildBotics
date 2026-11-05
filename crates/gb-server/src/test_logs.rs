//! Capture synchronous diagnostics without installing a process-wide subscriber.
use std::sync::{Arc, Mutex};
use tracing::{
    Subscriber,
    span::{Attributes, Id, Record},
};
struct Logs(Arc<Mutex<Vec<String>>>);
impl Subscriber for Logs {
    fn enabled(&self, _: &tracing::Metadata<'_>) -> bool {
        true
    }
    fn new_span(&self, _: &Attributes<'_>) -> Id {
        Id::from_u64(1)
    }
    fn record(&self, _: &Id, _: &Record<'_>) {}
    fn record_follows_from(&self, _: &Id, _: &Id) {}
    fn enter(&self, _: &Id) {}
    fn exit(&self, _: &Id) {}
    fn event(&self, event: &tracing::Event<'_>) {
        struct Message(String);
        impl tracing::field::Visit for Message {
            fn record_debug(&mut self, field: &tracing::field::Field, value: &dyn std::fmt::Debug) {
                if field.name() == "message" {
                    self.0 = format!("{value:?}");
                }
            }
        }
        let mut message = Message(String::new());
        event.record(&mut message);
        self.0.lock().unwrap().push(message.0);
    }
}
pub fn capture<T>(apply: impl FnOnce() -> T) -> (T, Vec<String>) {
    let logs = Arc::new(Mutex::new(vec![]));
    let result = tracing::subscriber::with_default(Logs(logs.clone()), apply);
    let messages = logs.lock().unwrap().clone();
    (result, messages)
}
