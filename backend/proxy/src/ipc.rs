use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::io::{AsyncBufRead, AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::tcp::{OwnedReadHalf as TcpRead, OwnedWriteHalf as TcpWrite};
#[cfg(unix)]
use tokio::net::unix::{OwnedReadHalf as UnixRead, OwnedWriteHalf as UnixWrite};
use tokio::net::TcpStream;
#[cfg(unix)]
use tokio::net::UnixStream;
use tokio::sync::{mpsc, oneshot, Notify};

use crate::tcp::TcpState;
use crate::voice::VoiceState;

const MAX_IPC_LINE: usize = 1024 * 1024;
const IPC_REQUEST_TIMEOUT: Duration = Duration::from_secs(15);
const CTRL_QUEUE_CAP: usize = 1024;
const SNIFF_QUEUE_CAP: usize = 64;

pub enum IpcRead {
    Tcp(BufReader<TcpRead>),
    #[cfg(unix)]
    Unix(BufReader<UnixRead>),
}

enum IpcWrite {
    Tcp(TcpWrite),
    #[cfg(unix)]
    Unix(UnixWrite),
}

impl IpcWrite {
    async fn write_all(&mut self, data: &[u8]) -> std::io::Result<()> {
        match self {
            Self::Tcp(w) => w.write_all(data).await,
            #[cfg(unix)]
            Self::Unix(w) => w.write_all(data).await,
        }
    }
}

async fn read_line_bounded<R: AsyncBufRead + Unpin>(
    reader: &mut R,
    max_len: usize,
) -> std::io::Result<Option<String>> {
    let mut line = Vec::with_capacity(256);
    loop {
        let avail = reader.fill_buf().await?;
        if avail.is_empty() {
            if line.is_empty() {
                return Ok(None);
            }
            return String::from_utf8(line)
                .map(Some)
                .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e));
        }
        if let Some(pos) = avail.iter().position(|&b| b == b'\n') {
            if line.len() + pos + 1 > max_len {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    "IPC line exceeds maximum length",
                ));
            }
            line.extend_from_slice(&avail[..pos]);
            reader.consume(pos + 1);
            if line.last() == Some(&b'\r') {
                line.pop();
            }
            return String::from_utf8(line)
                .map(Some)
                .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e));
        }
        if line.len() + avail.len() > max_len {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "IPC line exceeds maximum length",
            ));
        }
        let n = avail.len();
        line.extend_from_slice(avail);
        reader.consume(n);
    }
}

impl IpcRead {
    async fn read_line(&mut self) -> std::io::Result<Option<String>> {
        match self {
            Self::Tcp(r) => read_line_bounded(r, MAX_IPC_LINE).await,
            #[cfg(unix)]
            Self::Unix(r) => read_line_bounded(r, MAX_IPC_LINE).await,
        }
    }
}

#[derive(Debug, Serialize)]
#[serde(tag = "t", rename_all = "snake_case")]
pub enum OutboundMsg {
    Hello {
        version: u32,
        token: String,
    },
    Ready {
        tcp_listening: bool,
        tcp_error: Option<String>,
        tcp_port: u16,
        voice_listening: bool,
        voice_error: Option<String>,
        voice_port: u16,
    },
    McHandshake {
        id: u64,
        conn_id: u64,
        peer_ip: String,
        peer_port: u16,
        local_ip: String,
        local_port: u16,
        tcp_loopback: bool,
        edition: String,
        protocol: i32,
        hostname: String,
        next_state: i32,
        player: Option<String>,
        player_uuid: Option<String>,
    },
    McUpstreamFailed {
        id: u64,
        conn_id: u64,
    },
    ConnOpened {
        conn_id: u64,
    },
    ConnClosed {
        conn_id: u64,
    },
    SniffChunk {
        conn_id: u64,
        data_b64: String,
    },
    HttpRequest {
        id: u64,
        method: String,
        path: String,
    },
    Telemetry {
        tcp_listening: bool,
        tcp_error: Option<String>,
        active: usize,
        total: u64,
        bytes_up: u64,
        bytes_down: u64,
        packs_served: u64,
        per_server: HashMap<String, usize>,
        pings: HashMap<String, HashMap<String, Option<u32>>>,
        voice_listening: bool,
        voice_error: Option<String>,
        voice_sessions: usize,
        voice_bytes_up: u64,
        voice_bytes_down: u64,
        voice_dropped: u64,
        voice_per_server: HashMap<String, usize>,
    },
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ConnOp {
    Disconnect,
    StopSniff,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "t", rename_all = "snake_case")]
enum InboundCommand {
    ConnControl {
        conn_id: u64,
        op: ConnOp,
    },
    VoiceSync {
        servers: HashMap<String, u16>,
        players: HashMap<String, String>,
    },
    VoiceBind {
        uuid: String,
        sid: String,
    },
    Shutdown,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case")]
pub enum McDecision {
    Forward {
        target_port: u16,
        send_proxy_v2: bool,
        track_sid: Option<String>,
        player: Option<String>,
        timeout_secs: Option<u64>,
        sniff_seconds: u64,
        sniff_max_bytes: usize,
    },
    Status {
        json: String,
    },
    Disconnect {
        json: String,
    },
    Close,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case")]
pub enum HttpDecision {
    File {
        status: String,
        headers: Vec<(String, String)>,
        path: String,
        #[allow(dead_code)]
        size: u64,
    },
    Body {
        status: String,
        headers: Vec<(String, String)>,
        body_b64: String,
    },
}

pub struct IpcClient {
    ctrl_tx: mpsc::Sender<String>,
    sniff_tx: mpsc::Sender<String>,
    next_id: AtomicU64,
    pending: Mutex<HashMap<u64, oneshot::Sender<Value>>>,
    shutdown: Arc<Notify>,
}

impl IpcClient {
    pub async fn connect(
        addr: &str,
        token: &str,
        shutdown: Arc<Notify>,
    ) -> Result<(Arc<Self>, IpcRead), String> {
        let (read, mut write) = if let Some(path) = addr.strip_prefix("unix:") {
            #[cfg(unix)]
            {
                let stream = UnixStream::connect(path)
                    .await
                    .map_err(|e| format!("unix connect {path}: {e}"))?;
                let (r, w) = stream.into_split();
                (IpcRead::Unix(BufReader::new(r)), IpcWrite::Unix(w))
            }
            #[cfg(not(unix))]
            {
                let _ = path;
                return Err("Unix sockets are not supported on this platform".to_string());
            }
        } else if let Some(host_port) = addr.strip_prefix("tcp:") {
            let stream = TcpStream::connect(host_port)
                .await
                .map_err(|e| format!("tcp connect {host_port}: {e}"))?;
            let _ = stream.set_nodelay(true);
            let (r, w) = stream.into_split();
            (IpcRead::Tcp(BufReader::new(r)), IpcWrite::Tcp(w))
        } else {
            return Err(format!("Unsupported IPC address scheme: {addr}"));
        };

        let hello = OutboundMsg::Hello {
            version: 1,
            token: token.to_string(),
        };
        let mut hello_line = serde_json::to_string(&hello).map_err(|e| e.to_string())?;
        hello_line.push('\n');
        write
            .write_all(hello_line.as_bytes())
            .await
            .map_err(|e| format!("failed to send hello: {e}"))?;

        let (ctrl_tx, mut ctrl_rx) = mpsc::channel::<String>(CTRL_QUEUE_CAP);
        let (sniff_tx, mut sniff_rx) = mpsc::channel::<String>(SNIFF_QUEUE_CAP);
        let writer_shutdown = shutdown.clone();

        tokio::spawn(async move {
            loop {
                let line = tokio::select! {
                    biased;
                    msg = ctrl_rx.recv() => match msg {
                        Some(m) => m,
                        None => break,
                    },
                    msg = sniff_rx.recv() => match msg {
                        Some(m) => m,
                        None => match ctrl_rx.recv().await {
                            Some(m) => m,
                            None => break,
                        }
                    },
                };
                if write.write_all(line.as_bytes()).await.is_err() {
                    writer_shutdown.notify_waiters();
                    break;
                }
            }
        });

        let client = Arc::new(Self {
            ctrl_tx,
            sniff_tx,
            next_id: AtomicU64::new(1),
            pending: Mutex::new(HashMap::new()),
            shutdown,
        });
        Ok((client, read))
    }

    pub fn next_request_id(&self) -> u64 {
        self.next_id.fetch_add(1, Ordering::Relaxed)
    }

    pub async fn notify(&self, msg: &OutboundMsg) -> Result<(), String> {
        let mut line = serde_json::to_string(msg).map_err(|e| e.to_string())?;
        line.push('\n');
        self.ctrl_tx
            .send(line)
            .await
            .map_err(|_| "IPC channel closed".to_string())
    }

    pub fn try_sniff(&self, conn_id: u64, data_b64: String) -> bool {
        let msg = OutboundMsg::SniffChunk { conn_id, data_b64 };
        let Ok(mut line) = serde_json::to_string(&msg) else {
            return false;
        };
        line.push('\n');
        self.sniff_tx.try_send(line).is_ok()
    }

    pub async fn request<T: DeserializeOwned>(
        &self,
        id: u64,
        msg: &OutboundMsg,
    ) -> Result<T, String> {
        let mut line = serde_json::to_string(msg).map_err(|e| e.to_string())?;
        line.push('\n');

        let (tx, rx) = oneshot::channel();
        {
            let mut guard = self.pending.lock().unwrap();
            guard.insert(id, tx);
        }

        if self.ctrl_tx.send(line).await.is_err() {
            let mut guard = self.pending.lock().unwrap();
            guard.remove(&id);
            return Err("IPC writer closed".to_string());
        }

        let val = match tokio::time::timeout(IPC_REQUEST_TIMEOUT, rx).await {
            Ok(Ok(v)) => v,
            Ok(Err(_)) => return Err("IPC response dropped".to_string()),
            Err(_) => {
                let mut guard = self.pending.lock().unwrap();
                guard.remove(&id);
                return Err("IPC request timed out".to_string());
            }
        };

        serde_json::from_value::<T>(val).map_err(|e| format!("invalid IPC response payload: {e}"))
    }

    pub async fn run_reader(
        self: &Arc<Self>,
        mut reader: IpcRead,
        tcp: Arc<TcpState>,
        voice: Arc<VoiceState>,
    ) {
        loop {
            match reader.read_line().await {
                Ok(Some(line)) => {
                    if line.is_empty() {
                        continue;
                    }
                    let Ok(val) = serde_json::from_str::<Value>(&line) else {
                        eprintln!("[minenode-proxy] ignoring malformed IPC JSON");
                        continue;
                    };
                    if val.get("t").and_then(Value::as_str) == Some("resp") {
                        if let Some(id) = val.get("id").and_then(Value::as_u64) {
                            let tx = {
                                let mut guard = self.pending.lock().unwrap();
                                guard.remove(&id)
                            };
                            if let Some(tx) = tx {
                                let _ = tx.send(val);
                            }
                        }
                        continue;
                    }
                    match serde_json::from_value::<InboundCommand>(val) {
                        Ok(InboundCommand::ConnControl { conn_id, op }) => {
                            tcp.control_conn(conn_id, op);
                        }
                        Ok(InboundCommand::VoiceSync { servers, players }) => {
                            voice.sync(servers, players);
                        }
                        Ok(InboundCommand::VoiceBind { uuid, sid }) => {
                            voice.bind(&uuid, sid);
                        }
                        Ok(InboundCommand::Shutdown) => {
                            self.shutdown.notify_waiters();
                            break;
                        }
                        Err(e) => {
                            eprintln!("[minenode-proxy] unknown IPC command: {e}");
                        }
                    }
                }
                Ok(None) | Err(_) => {
                    // Parent IPC stream dropped or errored: exit to avoid orphaned sockets.
                    self.shutdown.notify_waiters();
                    break;
                }
            }
        }
        // Fail any outstanding requests immediately on disconnect.
        let mut guard = self.pending.lock().unwrap();
        guard.clear();
    }
}
