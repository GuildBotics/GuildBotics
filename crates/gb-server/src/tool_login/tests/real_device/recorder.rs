//! Loopback HTTP, HTTPS and CONNECT endpoints. A tunnel terminates here.

use std::{
    io,
    sync::{Arc, Mutex},
};

use axum::body::Bytes;
use hyper::{Request, Response, StatusCode, body::Incoming, service::service_fn};
use hyper_util::rt::TokioIo;
use tokio::{net::TcpListener, task::JoinHandle};
use tokio_rustls::{LazyConfigAcceptor, rustls};

pub const HOST: &str = "host.microsandbox.internal";

#[derive(Clone, Debug)]
pub struct Seen {
    pub kind: &'static str,
    pub host: String,
    pub method: String,
    pub path: String,
    pub authorization: String,
    pub headers: hyper::HeaderMap,
}

type Reply = Response<axum::body::Body>;
type Answer = dyn Fn(&Seen) -> Option<Reply> + Send + Sync;

#[derive(Clone)]
struct State {
    seen: Arc<Mutex<Vec<Seen>>>,
    answer: Arc<Answer>,
    authority: Arc<TestAuthority>,
}

pub struct Recorder {
    pub http: u16,
    pub tls: u16,
    pub ca: Vec<u8>,
    state: State,
    tasks: [JoinHandle<()>; 2],
}

impl Drop for Recorder {
    fn drop(&mut self) {
        for task in &self.tasks {
            task.abort();
        }
    }
}

impl Recorder {
    pub async fn start(answer: impl Fn(&Seen) -> Option<Reply> + Send + Sync + 'static) -> Self {
        let (authority, ca) = TestAuthority::new();
        let state = State {
            seen: Arc::default(),
            answer: Arc::new(answer),
            authority: Arc::new(authority),
        };
        let plain = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let secure = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        Self {
            http: plain.local_addr().unwrap().port(),
            tls: secure.local_addr().unwrap().port(),
            ca,
            tasks: [
                tokio::spawn(listen(plain, state.clone(), false)),
                tokio::spawn(listen(secure, state.clone(), true)),
            ],
            state,
        }
    }

    pub fn url(&self, tls: bool) -> String {
        if tls {
            format!("https://{HOST}:{}", self.tls)
        } else {
            format!("http://{HOST}:{}", self.http)
        }
    }

    pub fn seen(&self) -> Vec<Seen> {
        self.state.seen.lock().unwrap().clone()
    }
}

pub fn reply(status: u16, content_type: &str, body: impl Into<Bytes>) -> Reply {
    Response::builder()
        .status(status)
        .header("content-type", content_type)
        .body(axum::body::Body::from(body.into()))
        .unwrap()
}

struct TestAuthority {
    issuer: rcgen::Issuer<'static, rcgen::KeyPair>,
    not_before: time::OffsetDateTime,
    not_after: time::OffsetDateTime,
}

impl TestAuthority {
    fn new() -> (Self, Vec<u8>) {
        let now = time::OffsetDateTime::now_utc();
        let mut ca = rcgen::CertificateParams::default();
        ca.distinguished_name
            .push(rcgen::DnType::CommonName, "CLI contract test CA");
        ca.not_before = now - time::Duration::minutes(5);
        ca.not_after = now + time::Duration::days(1);
        ca.is_ca = rcgen::IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
        ca.key_usages.push(rcgen::KeyUsagePurpose::KeyCertSign);
        let key = rcgen::KeyPair::generate().unwrap();
        let pem = ca.self_signed(&key).unwrap().pem().into_bytes();
        let authority = Self {
            not_before: ca.not_before,
            not_after: ca.not_after,
            issuer: rcgen::Issuer::new(ca, key),
        };
        (authority, pem)
    }

    fn config(&self, name: &str) -> Option<rustls::ServerConfig> {
        let mut leaf = rcgen::CertificateParams::new(vec![name.to_owned()]).ok()?;
        leaf.not_before = self.not_before;
        leaf.not_after = self.not_after;
        leaf.extended_key_usages
            .push(rcgen::ExtendedKeyUsagePurpose::ServerAuth);
        leaf.use_authority_key_identifier_extension = true;
        let key = rcgen::KeyPair::generate().ok()?;
        let cert = leaf.signed_by(&key, &self.issuer).ok()?;
        let key = rustls::pki_types::PrivatePkcs8KeyDer::from(key.serialize_der()).into();
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        rustls::ServerConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .ok()?
            .with_no_client_auth()
            .with_single_cert(vec![cert.der().clone()], key)
            .ok()
    }
}

async fn listen(listener: TcpListener, state: State, tls: bool) {
    let mut connections = tokio::task::JoinSet::new();
    loop {
        tokio::select! {
            connection = listener.accept() => {
                let Ok((socket, _)) = connection else { return };
                let state = state.clone();
                connections.spawn(async move {
                    if tls {
                        secure(socket, state, None).await;
                    } else {
                        let _ = hyper::server::conn::http1::Builder::new()
                            .serve_connection(TokioIo::new(socket), service_fn(move |req| {
                                let state = state.clone();
                                async move { Ok::<_, io::Error>(dispatch(req, state, None)) }
                            }))
                            .with_upgrades()
                            .await;
                    }
                });
            }
            _ = connections.join_next(), if !connections.is_empty() => {}
        }
    }
}

async fn secure<S>(stream: S, state: State, tunnel: Option<String>)
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send + 'static,
{
    let acceptor = LazyConfigAcceptor::new(rustls::server::Acceptor::default(), stream);
    let Ok(hello) = acceptor.await else { return };
    let name = hello.client_hello().server_name().map(str::to_owned);
    let certificate = name.as_deref().or(tunnel.as_deref()).unwrap_or(HOST);
    let Some(config) = state.authority.config(certificate) else {
        return;
    };
    let destination = tunnel.or(name).unwrap_or_else(|| HOST.into());
    let Ok(socket) = hello.into_stream(Arc::new(config)).await else {
        return;
    };
    let _ = hyper::server::conn::http1::Builder::new()
        .serve_connection(
            TokioIo::new(socket),
            service_fn(move |req| {
                let state = state.clone();
                let destination = destination.clone();
                async move { Ok::<_, io::Error>(dispatch(req, state, Some(&destination))) }
            }),
        )
        .await;
}

fn dispatch(mut request: Request<Incoming>, state: State, tls_host: Option<&str>) -> Reply {
    let connect = request.method() == hyper::Method::CONNECT;
    let host = tls_host
        .or_else(|| request.uri().host())
        .map(str::to_owned)
        .or_else(|| {
            request
                .headers()
                .get("host")?
                .to_str()
                .ok()?
                .parse::<hyper::http::uri::Authority>()
                .ok()
                .map(|authority| authority.host().to_owned())
        })
        .unwrap_or_default();
    let seen = Seen {
        kind: if connect {
            "connect"
        } else if tls_host.is_some() {
            "tls"
        } else {
            "http"
        },
        host,
        method: request.method().to_string(),
        path: request.uri().path().to_owned(),
        authorization: request
            .headers()
            .get("authorization")
            .and_then(|value| value.to_str().ok())
            .unwrap_or_default()
            .to_owned(),
        headers: request.headers().clone(),
    };
    state.seen.lock().unwrap().push(seen.clone());
    if connect {
        if tls_host.is_some() {
            return reply(405, "text/plain", "");
        }
        let upgrade = hyper::upgrade::on(&mut request);
        tokio::spawn(async move {
            if let Ok(stream) = upgrade.await {
                secure(TokioIo::new(stream), state, Some(seen.host)).await;
            }
        });
        return Response::builder()
            .status(StatusCode::OK)
            .body(axum::body::Body::empty())
            .unwrap();
    }
    (state.answer)(&seen).unwrap_or_else(|| {
        reply(
            403,
            "application/json",
            r#"{"error":{"message":"SYNTHETIC_UPSTREAM_REACHED"}}"#,
        )
    })
}

#[tokio::test]
async fn the_recorder_terminates_proxy_connect_locally() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    let recorder = Recorder::start(|_| None).await;
    let mut stream = tokio::net::TcpStream::connect(("127.0.0.1", recorder.http))
        .await
        .unwrap();
    stream
        .write_all(b"CONNECT provider.invalid:443 HTTP/1.1\r\nHost: provider.invalid:443\r\n\r\n")
        .await
        .unwrap();
    let mut head = Vec::new();
    while !head.ends_with(b"\r\n\r\n") {
        head.push(stream.read_u8().await.unwrap());
    }
    assert!(head.starts_with(b"HTTP/1.1 200"));
    let mut roots = rustls::RootCertStore::empty();
    use rustls::pki_types::pem::PemObject;
    roots
        .add(rustls::pki_types::CertificateDer::from_pem_slice(&recorder.ca).unwrap())
        .unwrap();
    let mut client = rustls::ClientConfig::builder_with_provider(Arc::new(
        rustls::crypto::ring::default_provider(),
    ))
    .with_safe_default_protocol_versions()
    .unwrap()
    .with_root_certificates(roots)
    .with_no_client_auth();
    client.enable_sni = false;
    let mut stream = tokio_rustls::TlsConnector::from(Arc::new(client))
        .connect("provider.invalid".try_into().unwrap(), stream)
        .await
        .unwrap();
    stream.write_all(b"GET /profile HTTP/1.1\r\nHost: host.microsandbox.internal\r\nAuthorization: Bearer synthetic\r\nConnection: close\r\n\r\n").await.unwrap();
    let mut response = Vec::new();
    stream.read_to_end(&mut response).await.unwrap();
    assert!(response.starts_with(b"HTTP/1.1 403"));
    let seen = recorder.seen();
    assert_eq!(seen.len(), 2);
    assert_eq!(seen[0].kind, "connect");
    assert_eq!(seen[1].kind, "tls");
    assert_eq!(seen[1].host, "provider.invalid");
    assert_eq!(seen[1].path, "/profile");
    assert_eq!(seen[1].authorization, "Bearer synthetic");
}
