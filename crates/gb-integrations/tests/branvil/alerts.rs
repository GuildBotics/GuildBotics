//! Frozen dependency-alert dispatch through the provider and upstream domain.
use super::*;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};

#[derive(Default)]
struct Answers {
    alerts: Vec<Value>,
    failure: Option<u16>,
    head: String,
    requests: Vec<String>,
}
pub(super) struct Host {
    address: std::net::SocketAddr,
    answers: Arc<Mutex<Answers>>,
    stopped: Arc<AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
}
impl Host {
    pub(super) fn configure(data: &Path) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let answers = Arc::new(Mutex::new(Answers::default()));
        let stopped = Arc::new(AtomicBool::new(false));
        let (shared, done) = (answers.clone(), stopped.clone());
        let thread = std::thread::spawn(move || {
            for stream in listener.incoming() {
                if done.load(Ordering::SeqCst) {
                    break;
                }
                let mut stream = stream.unwrap();
                stream
                    .set_read_timeout(Some(std::time::Duration::from_secs(5)))
                    .unwrap();
                let mut bytes = Vec::new();
                let mut byte = [0];
                while !bytes.ends_with(b"\r\n\r\n") {
                    if stream.read(&mut byte).unwrap_or(0) == 0 {
                        break;
                    }
                    bytes.push(byte[0]);
                }
                let request = String::from_utf8_lossy(&bytes);
                let path = request.split_whitespace().nth(1).unwrap_or("/");
                let mut a = shared.lock().unwrap();
                let mut status = 200;
                let mut link = String::new();
                let body = if path == "/user" {
                    json!({"login":"fixture-reader"})
                } else if path.contains("/git/ref/heads/") {
                    json!({"object":{"sha":a.head}})
                } else if path.contains("/dependabot/alerts") {
                    assert!(
                        request
                            .to_ascii_lowercase()
                            .contains("authorization: bearer fixture-token"),
                        "{request}"
                    );
                    a.requests.push(path.into());
                    if let Some(code) = a.failure {
                        status = code;
                        json!({"message":"fixture upstream read failed"})
                    } else {
                        let url = url::Url::parse(&format!("http://{address}{path}")).unwrap();
                        if let Some(id) = url
                            .path()
                            .strip_prefix("/repos/acme/demo/dependabot/alerts/")
                        {
                            a.alerts
                                .iter()
                                .find(|v| v["number"].as_u64() == id.parse().ok())
                                .cloned()
                                .unwrap_or_else(|| {
                                    status = 404;
                                    json!({"message":"Not Found"})
                                })
                        } else {
                            let q: std::collections::BTreeMap<_, _> =
                                url.query_pairs().into_owned().collect();
                            let states = q.get("state").unwrap().split(',').collect::<Vec<_>>();
                            let limit: usize = q["per_page"].parse().unwrap();
                            let start: usize =
                                q.get("after").map(|s| s.parse().unwrap()).unwrap_or(0);
                            let found = a
                                .alerts
                                .iter()
                                .filter(|v| states.contains(&v["state"].as_str().unwrap()))
                                .cloned()
                                .collect::<Vec<_>>();
                            if start + limit < found.len() {
                                link = format!(
                                    "Link: <http://{address}/repos/acme/demo/dependabot/alerts?after={}>; rel=\"next\"\r\n",
                                    start + limit
                                );
                            }
                            json!(
                                found
                                    .into_iter()
                                    .skip(start)
                                    .take(limit)
                                    .collect::<Vec<_>>()
                            )
                        }
                    }
                } else {
                    json!({})
                };
                let body = body.to_string();
                write!(stream,"HTTP/1.1 {status} Fixture\r\nContent-Type: application/json\r\nContent-Length: {}\r\n{link}Connection: close\r\n\r\n{body}",body.len()).unwrap();
            }
        });
        let path = data.join("branvil.toml");
        let config = std::fs::read_to_string(&path)
            .unwrap()
            .replace("https://api.github.com", &format!("http://{address}"))
            .replace("https://github.com", &format!("http://{address}"));
        std::fs::write(path, config).unwrap();
        Self {
            address,
            answers,
            stopped,
            thread: Some(thread),
        }
    }
}
impl Drop for Host {
    fn drop(&mut self) {
        self.stopped.store(true, Ordering::SeqCst);
        let _ = TcpStream::connect(self.address);
        self.thread.take().unwrap().join().unwrap();
    }
}
async fn read(
    c: &dyn CodeHostingService,
    parameters: Value,
    cursor: &str,
) -> Result<ReadPage, ServiceError> {
    c.read(Resource::DependencyAlerts, REPO, Some(&parameters), cursor)
        .await
}
async fn write(base: &str, token: &str, path: &str, body: Option<Value>) {
    let client = reqwest::Client::builder()
        .no_proxy()
        .timeout(std::time::Duration::from_secs(5))
        .build()
        .unwrap();
    let request = match body {
        Some(body) => client.put(format!("{base}/api/v1{path}")).json(&body),
        None => client.delete(format!("{base}/api/v1{path}")),
    };
    let response = request.bearer_auth(token).send().await.unwrap();
    assert!(
        response.status().is_success(),
        "{}",
        response.text().await.unwrap()
    );
}
fn expected_alert(number: u64, state: &str, patched: Option<&str>) -> Value {
    json!({"id":number.to_string(),"state":state,"url":format!("https://example.invalid/alerts/{number}"),"package":"fixture","ecosystem":"pip","manifest_path":"requirements.txt","severity":"high","identifiers":[{"type":"GHSA","value":format!("GHSA-{number}")}],"summary":"Fixture advisory","description":"Fixture description","affected_versions":"< 2.0","patched_version":patched,"created_at":"2026-01-01T00:00:00.000Z","updated_at":"2026-01-02T00:00:00.000Z"})
}
async fn state_pages(host: &Host, c: &dyn CodeHostingService) {
    host.answers.lock().unwrap().alerts = [(1,"open"),(2,"fixed"),(3,"open"),(4,"auto_dismissed"),(5,"open")].into_iter().map(|(number,state)| json!({"number":number,"state":state,"html_url":format!("https://example.invalid/alerts/{number}"),"dependency":{"package":{"name":"fixture","ecosystem":"pip"},"manifest_path":"requirements.txt"},"security_advisory":{"identifiers":[{"type":"GHSA","value":format!("GHSA-{number}")}],"summary":"Fixture advisory","description":"Fixture description"},"security_vulnerability":{"severity":"high","vulnerable_version_range":"< 2.0","first_patched_version":if number==5 {Value::Null}else{json!({"identifier":"2.0"})}},"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-02T00:00:00Z"})).collect();
    let p = json!({"state":"open","page_size":2});
    let first = read(c, p.clone(), "").await.unwrap();
    assert_eq!(
        first.items().iter().map(|v| &v["id"]).collect::<Vec<_>>(),
        [&json!("1"), &json!("3")]
    );
    assert_eq!(first.items()[0], expected_alert(1, "open", Some("2.0")));
    assert_eq!(first.items()[0]["package"], "fixture");
    assert_eq!(first.items()[0]["patched_version"], "2.0");
    let rest = read(c, p, first.continuation().unwrap()).await.unwrap();
    assert_eq!(rest.items()[0]["id"], "5");
    assert_eq!(rest.items()[0]["patched_version"], Value::Null);
    assert_eq!(rest.continuation(), None);
    for (state, id) in [("resolved", "2"), ("dismissed", "4")] {
        let page = read(c, json!({"state":state}), "").await.unwrap();
        assert_eq!(page.items().len(), 1);
        assert_eq!(page.items()[0]["id"], id);
        assert_eq!(page.items()[0]["state"], state);
    }
    assert!(
        read(
            c,
            json!({"state":"resolved","page_size":2}),
            first.continuation().unwrap()
        )
        .await
        .is_err()
    );
}
async fn single_id(c: &dyn CodeHostingService) {
    let page = read(c, json!({"id":"4"}), "").await.unwrap();
    assert_eq!(page.items().len(), 1);
    assert_eq!(page.items()[0], expected_alert(4, "dismissed", Some("2.0")));
    assert_eq!(page.items()[0]["id"], "4");
    assert_eq!(page.items()[0]["state"], "dismissed");
    assert_eq!(page.continuation(), None);
    let missing = read(c, json!({"id":"99"}), "").await.unwrap_err();
    assert_eq!(
        missing.translation_key(),
        "integrations.branvil.api.not_found"
    );
    assert!(missing.is_not_found());
    assert!(!missing.translation_args()["message"].is_empty());
}
async fn empty_and_unsupported(host: &Host, c: &dyn CodeHostingService, base: &str, token: &str) {
    host.answers.lock().unwrap().alerts.clear();
    let page = read(c, json!({}), "").await.unwrap();
    assert!(page.items().is_empty());
    assert_eq!(page.continuation(), None);
    write(base, token, "/repos/acme%2Fdemo/upstream", None).await;
    let unsupported = read(c, json!({}), "").await.unwrap_err();
    assert_eq!(
        unsupported.translation_key(),
        "integrations.branvil.api.unsupported"
    );
    assert!(unsupported.is_unsupported());
    assert!(!unsupported.translation_args()["message"].is_empty());
}
async fn failures(host: &Host, c: &dyn CodeHostingService) {
    for status in [401, 403, 404, 429, 500] {
        host.answers.lock().unwrap().failure = Some(status);
        for query in [json!({}), json!({"id":"1"})] {
            let result = read(c, query, "").await;
            let error = result.unwrap_err();
            assert_eq!(error.is_unavailable(), status == 429 || status == 500);
            if status == 404 {
                assert_eq!(
                    error.translation_key(),
                    "integrations.branvil.api.not_found"
                );
                assert!(error.is_not_found());
                assert!(!error.translation_args()["message"].is_empty());
            } else {
                assert_eq!(error.translation_key(), "integrations.branvil.api.refused");
                let args = error.translation_args();
                assert_eq!(
                    args["status"],
                    if status == 401 || status == 403 {
                        "409"
                    } else {
                        "503"
                    }
                );
                assert_eq!(
                    args["code"],
                    if status == 401 || status == 403 {
                        "upstream_refused"
                    } else {
                        "unavailable"
                    }
                );
                assert!(!args["message"].is_empty());
                assert!(
                    ["upstream_dependency_alert", "upstream_dependency_alerts"]
                        .contains(&args["operation"].as_str())
                );
            }
        }
    }
    host.answers.lock().unwrap().failure = None;
}
async fn member_alert_command_shows_the_upstreams_alerts(c: &dyn CodeHostingService) {
    struct Services<'a>(&'a dyn CodeHostingService);
    impl gb_member::repository::RepositoryServices for Services<'_> {
        fn code(&self) -> gb_member::Result<&dyn CodeHostingService> {
            Ok(self.0)
        }
        fn tickets(&self) -> gb_member::Result<&dyn TicketService> {
            panic!("alert is a code resource")
        }
    }
    struct Current;
    impl gb_member::WriteGuard for Current {
        fn ensure_current(&self) -> gb_member::Result<()> {
            Ok(())
        }
    }
    struct Control;
    impl gb_member::session::Control for Control {
        fn command<'a>(
            &'a self,
            _: &'a gb_member::command::Parsed,
            _: &'a str,
        ) -> gb_member::git::Pending<'a, Value> {
            panic!("not a completion")
        }
        fn record(&self, _: &str, read_only: bool, _: &Value) -> gb_member::Result<()> {
            assert!(read_only);
            Ok(())
        }
    }
    let services = Services(c);
    let roster = std::collections::BTreeMap::new();
    let root = Path::new("/unused-alert-root");
    let session = gb_member::session::Session {
        person_id: "aiko",
        person_type: "agent",
        delegated_to: None,
        workflow: true,
        repository: gb_member::repository::Repository {
            services: &services,
            guard: &Current,
            roster: &roster,
        },
        chat: None,
        memory: None,
        git: None,
        context: json!({}),
        control: &Control,
        git_root: root,
    };
    let arguments = [
        "repo-info",
        "read",
        "--person",
        "aiko",
        "--resource",
        "dependency_alerts",
        "--subject",
        REPO,
        "--params",
        r#"{"state":"open","page_size":30}"#,
    ]
    .map(str::to_owned);
    let answer = session.execute(&arguments, "").await.unwrap();
    let expected = json!({"repo":REPO,"alerts":answer.payload["items"],"continuation":answer.payload["continuation"]});
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let recipe: Value = serde_json::from_str(
        &std::fs::read_to_string(repo.join("guildbotics/templates/environment/recipe.json"))
            .unwrap(),
    )
    .unwrap();
    let python = Path::new(recipe["venv"].as_str().unwrap()).join("bin/python");
    let mut child = Command::new(python)
        .arg(repo.join("tests/guildbotics/guest/alert_command_fixture.py"))
        .env("PYTHONPATH", &repo)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(serde_json::to_string(&answer.payload).unwrap().as_bytes())
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let shown: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(shown, expected);
    assert!(
        shown["alerts"]
            .as_array()
            .unwrap()
            .iter()
            .any(|v| v["patched_version"].is_null())
    );
}
pub(super) async fn contract(
    host: &Host,
    c: &dyn CodeHostingService,
    base: &str,
    token: &str,
    head: &str,
) {
    host.answers.lock().unwrap().head = head.into();
    write(
        base,
        token,
        "/members/owner/accounts/github",
        Some(json!({"token":"fixture-token"})),
    )
    .await;
    write(
        base,
        token,
        "/repos/acme%2Fdemo/upstream",
        Some(json!({"provider":"github","repository":REPO})),
    )
    .await;
    state_pages(host, c).await;
    member_alert_command_shows_the_upstreams_alerts(c).await;
    single_id(c).await;
    failures(host, c).await;
    empty_and_unsupported(host, c, base, token).await;
}
