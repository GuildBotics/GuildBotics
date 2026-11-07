//! Record tracing events without sending them to event subscribers.
use crate::events::EventBus;
use serde_json::{Map, Value};
use std::sync::{Arc, LazyLock, RwLock, Weak};
use tracing_subscriber::{Layer, layer::Context, registry::LookupSpan};

/// Filtering log events must not discard the spans carrying correlation.
pub struct LogFilter(tracing_subscriber::EnvFilter);
impl LogFilter {
    pub fn new(filter: tracing_subscriber::EnvFilter) -> Self {
        Self(filter)
    }
}
impl<S> Layer<S> for LogFilter
where
    S: tracing::Subscriber + for<'a> LookupSpan<'a>,
{
    fn enabled(&self, metadata: &tracing::Metadata<'_>, context: Context<'_, S>) -> bool {
        metadata.is_span() || self.0.enabled(metadata, context)
    }
    fn register_callsite(
        &self,
        metadata: &'static tracing::Metadata<'static>,
    ) -> tracing::subscriber::Interest {
        let interest =
            <tracing_subscriber::EnvFilter as Layer<S>>::register_callsite(&self.0, metadata);
        if metadata.is_span() {
            tracing::subscriber::Interest::always()
        } else {
            interest
        }
    }
    fn on_new_span(
        &self,
        attributes: &tracing::span::Attributes<'_>,
        id: &tracing::span::Id,
        context: Context<'_, S>,
    ) {
        self.0.on_new_span(attributes, id, context);
    }
    fn on_record(
        &self,
        id: &tracing::span::Id,
        values: &tracing::span::Record<'_>,
        context: Context<'_, S>,
    ) {
        self.0.on_record(id, values, context);
    }
    fn on_enter(&self, id: &tracing::span::Id, context: Context<'_, S>) {
        self.0.on_enter(id, context);
    }
    fn on_exit(&self, id: &tracing::span::Id, context: Context<'_, S>) {
        self.0.on_exit(id, context);
    }
    fn on_close(&self, id: tracing::span::Id, context: Context<'_, S>) {
        self.0.on_close(id, context);
    }
}

static BUS: LazyLock<RwLock<Weak<EventBus>>> = LazyLock::new(RwLock::default);
type Reload = Arc<dyn Fn(Option<&str>) + Send + Sync>;
static RELOAD: LazyLock<RwLock<Option<Reload>>> = LazyLock::new(RwLock::default);
thread_local! {static RECORDING:std::cell::Cell<bool> = const {std::cell::Cell::new(false)};}

pub(crate) fn attach(bus: &Arc<EventBus>) {
    *BUS.write().unwrap_or_else(|p| p.into_inner()) = Arc::downgrade(bus);
}
pub fn install_reload(reload: Reload) {
    *RELOAD.write().unwrap_or_else(|p| p.into_inner()) = Some(reload);
}
pub(crate) fn level(level: Option<&str>) {
    if let Some(reload) = RELOAD.read().unwrap_or_else(|p| p.into_inner()).as_ref() {
        reload(level);
    }
}

#[derive(Default)]
pub struct DiagnosticLayer {
    bus: Option<Arc<EventBus>>,
}
impl DiagnosticLayer {
    pub fn for_bus(bus: Arc<EventBus>) -> Self {
        Self { bus: Some(bus) }
    }
}
#[derive(Default)]
struct Fields {
    message: String,
    correlation: Option<Map<String, Value>>,
}
impl tracing::field::Visit for Fields {
    fn record_debug(&mut self, field: &tracing::field::Field, value: &dyn std::fmt::Debug) {
        if field.name() == "message" {
            self.message = format!("{value:?}");
        }
    }
    fn record_str(&mut self, field: &tracing::field::Field, value: &str) {
        match field.name() {
            "message" => self.message = value.into(),
            "diagnostics" => self.correlation = serde_json::from_str(value).ok(),
            _ => {}
        }
    }
}
impl<S> Layer<S> for DiagnosticLayer
where
    S: tracing::Subscriber + for<'a> LookupSpan<'a>,
{
    fn on_new_span(
        &self,
        attrs: &tracing::span::Attributes<'_>,
        id: &tracing::span::Id,
        ctx: Context<'_, S>,
    ) {
        let mut fields = Fields::default();
        attrs.record(&mut fields);
        if let (Some(span), Some(fields)) = (ctx.span(id), fields.correlation) {
            span.extensions_mut().insert(fields);
        }
    }
    fn on_event(&self, event: &tracing::Event<'_>, ctx: Context<'_, S>) {
        if RECORDING.with(|r| r.replace(true)) {
            return;
        }
        struct Reset;
        impl Drop for Reset {
            fn drop(&mut self) {
                RECORDING.with(|r| r.set(false));
            }
        }
        let _reset = Reset;
        let bus = self
            .bus
            .clone()
            .or_else(|| BUS.read().unwrap_or_else(|p| p.into_inner()).upgrade());
        let Some(bus) = bus else {
            return;
        };
        let mut fields = Fields::default();
        event.record(&mut fields);
        let correlation = ctx.event_scope(event).and_then(|mut scope| {
            scope.find_map(|span| span.extensions().get::<Map<String, Value>>().cloned())
        });
        let level = if *event.metadata().level() == tracing::Level::WARN {
            "WARNING"
        } else {
            event.metadata().level().as_str()
        };
        bus.record_log(level, &fields.message, correlation);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use gb_observability::{Diagnostics, Trace};
    use tracing_subscriber::prelude::*;
    #[tokio::test]
    async fn logs_are_recorded_in_the_current_trace_or_system_and_never_streamed() {
        let root = tempfile::tempdir().unwrap();
        let store = Diagnostics::open(root.path());
        store.start_system_session("test").unwrap();
        let bus = Arc::new(EventBus::default());
        bus.record_in(Some(store.clone()));
        let mut subscriber = bus.subscribe();
        let tracing = tracing_subscriber::registry().with(DiagnosticLayer::for_bus(bus.clone()));
        let dispatch = tracing::Dispatch::new(tracing);
        tracing::dispatcher::with_default(&dispatch, || tracing::warn!("disk almost full"));
        let trace = Trace::resume(&store, "manual", "trace-42").unwrap();
        use tracing::instrument::WithSubscriber as _;
        let result = trace
            .run_command("test", "aiko", async {
                tracing::error!("command failed");
                Ok::<_, TestFailure>(())
            })
            .with_subscriber(dispatch)
            .await;
        assert!(result.is_ok());
        let global = store.latest_system_records(200).unwrap();
        let logs = global["records"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|r| r["kind"] == "log")
            .collect::<Vec<_>>();
        assert_eq!(logs.len(), 1);
        assert_eq!(logs[0]["level"], "WARNING");
        assert_eq!(logs[0]["message"], "disk almost full");
        assert!(logs[0]["trace_id"].is_null());
        let detail = store.trace_detail("trace-42").unwrap();
        let logs = detail["records"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|r| r["kind"] == "log")
            .collect::<Vec<_>>();
        assert_eq!(logs.len(), 1);
        assert_eq!(logs[0]["message"], "command failed");
        assert_eq!(logs[0]["level"], "ERROR");
        assert_eq!(logs[0]["trace_id"], "trace-42");
        assert!(bus.snapshot().is_empty());
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(200), subscriber.next())
                .await
                .is_err()
        );
        store.finish_system_session().unwrap();
    }
    struct TestFailure;
    impl gb_observability::Failure for TestFailure {
        fn error_type(&self) -> String {
            "RuntimeError".into()
        }
    }

    #[tokio::test]
    async fn restrictive_event_filters_preserve_trace_correlation() {
        for filter in ["error", "gb_server=warn"] {
            let root = tempfile::tempdir().unwrap();
            let store = Diagnostics::open(root.path());
            store.start_system_session("filtered").unwrap();
            let bus = Arc::new(EventBus::default());
            bus.record_in(Some(store.clone()));
            let subscriber = tracing_subscriber::registry()
                .with(LogFilter::new(tracing_subscriber::EnvFilter::new(filter)))
                .with(DiagnosticLayer::for_bus(bus));
            let trace = Trace::open(&store, "manual");
            use tracing::instrument::WithSubscriber as _;
            trace
                .run_command("check", "alice", async {
                    tracing::debug!(target:"gb_server::test","must be filtered");
                    tracing::error!(target:"gb_server::test","scoped failure");
                    Ok::<_, TestFailure>(())
                })
                .with_subscriber(subscriber)
                .await
                .unwrap_or_else(|_| panic!("the test command succeeds"));
            let detail = store.trace_detail(trace.id()).unwrap();
            let logs = detail["records"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|r| r["kind"] == "log")
                .collect::<Vec<_>>();
            assert_eq!(logs.len(), 1);
            assert_eq!(logs[0]["message"], "scoped failure");
            assert_eq!(logs[0]["trace_id"], trace.id());
            let global = store.latest_system_records(200).unwrap();
            assert!(
                global["records"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .all(|r| r["kind"] != "log")
            );
            store.finish_system_session().unwrap();
        }
    }
}
