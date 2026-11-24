use std::collections::HashMap;
use std::net::SocketAddr;
#[cfg(unix)]
use std::os::fd::AsRawFd;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use tokio::fs::File;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::tcp::{OwnedReadHalf, OwnedWriteHalf};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::Notify;

use crate::ipc::{ConnOp, HttpDecision, IpcClient, McDecision, OutboundMsg};
use crate::proto::{
    build_proxy_v2_header, encode_mc_string, encode_packet, finish_varint, is_loopback_ip,
    normalize_ip, parse_handshake, parse_login_start, parse_varint, read_inbound_prefix,
    read_packet, PrefixedReader, HANDSHAKE_TIMEOUT, MAX_HANDSHAKE, MAX_HTTP_HEADER,
};

struct ActiveConn {
    track_sid: Option<String>,
    player_lower: Option<String>,
    tcp_loopback: bool,
    raw_fd: i32,
    disconnect: Arc<Notify>,
    sniff_active: Arc<AtomicBool>,
}

pub struct TcpSnapshot {
    pub listening: bool,
    pub error: Option<String>,
    pub active: usize,
    pub total: u64,
    pub bytes_up: u64,
    pub bytes_down: u64,
    pub packs_served: u64,
    pub per_server: HashMap<String, usize>,
    pub pings: HashMap<String, HashMap<String, Option<u32>>>,
}

pub struct TcpState {
    pub listening: AtomicBool,
    pub error: Mutex<Option<String>>,
    pub total: AtomicU64,
    pub bytes_up: AtomicU64,
    pub bytes_down: AtomicU64,
    pub packs_served: AtomicU64,
    next_conn_id: AtomicU64,
    active: Mutex<HashMap<u64, ActiveConn>>,
}

impl TcpState {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            listening: AtomicBool::new(false),
            error: Mutex::new(None),
            total: AtomicU64::new(0),
            bytes_up: AtomicU64::new(0),
            bytes_down: AtomicU64::new(0),
            packs_served: AtomicU64::new(0),
            next_conn_id: AtomicU64::new(1),
            active: Mutex::new(HashMap::new()),
        })
    }

    pub fn set_error(&self, err: Option<String>) {
        *self.error.lock().unwrap() = err;
    }

    pub fn control_conn(&self, conn_id: u64, op: ConnOp) {
        let guard = self.active.lock().unwrap();
        if let Some(conn) = guard.get(&conn_id) {
            match op {
                ConnOp::Disconnect => conn.disconnect.notify_waiters(),
                ConnOp::StopSniff => conn.sniff_active.store(false, Ordering::Relaxed),
            }
        }
    }

    fn insert_active(&self, conn_id: u64, conn: ActiveConn) {
        self.active.lock().unwrap().insert(conn_id, conn);
    }

    fn remove_active(&self, conn_id: u64) {
        self.active.lock().unwrap().remove(&conn_id);
    }

    pub fn snapshot(&self) -> TcpSnapshot {
        let (active, per_server, pings) = {
            let guard = self.active.lock().unwrap();
            let mut per_server: HashMap<String, usize> = HashMap::new();
            let mut pings: HashMap<String, HashMap<String, Option<u32>>> = HashMap::new();
            for conn in guard.values() {
                if let Some(sid) = &conn.track_sid {
                    *per_server.entry(sid.clone()).or_insert(0) += 1;
                    if let Some(player) = &conn.player_lower {
                        let rtt = if conn.tcp_loopback {
                            None
                        } else {
                            tcp_rtt_ms(conn.raw_fd)
                        };
                        pings
                            .entry(sid.clone())
                            .or_default()
                            .insert(player.clone(), rtt);
                    }
                }
            }
            (guard.len(), per_server, pings)
        };
        let error = self.error.lock().unwrap().clone();
        TcpSnapshot {
            listening: self.listening.load(Ordering::Relaxed),
            error,
            active,
            total: self.total.load(Ordering::Relaxed),
            bytes_up: self.bytes_up.load(Ordering::Relaxed),
            bytes_down: self.bytes_down.load(Ordering::Relaxed),
            packs_served: self.packs_served.load(Ordering::Relaxed),
            per_server,
            pings,
        }
    }
}

#[cfg(target_os = "linux")]
pub fn tcp_rtt_ms(fd: i32) -> Option<u32> {
    if fd < 0 {
        return None;
    }
    let mut buf = [0u8; 104];
    let mut len = buf.len() as libc::socklen_t;
    let rc = unsafe {
        libc::getsockopt(
            fd,
            libc::IPPROTO_TCP,
            libc::TCP_INFO,
            buf.as_mut_ptr() as *mut libc::c_void,
            &mut len,
        )
    };
    if rc == 0 && (len as usize) >= 72 {
        let rtt_us = u32::from_ne_bytes([buf[68], buf[69], buf[70], buf[71]]);
        if rtt_us > 0 {
            let ms = ((rtt_us as f64) / 1000.0).round() as u32;
            return Some(ms.max(1));
        }
    }
    None
}

#[cfg(target_os = "macos")]
pub fn tcp_rtt_ms(fd: i32) -> Option<u32> {
    if fd < 0 {
        return None;
    }
    const TCP_CONNECTION_INFO: libc::c_int = 0x106;
    let mut buf = [0u8; 64];
    let mut len = buf.len() as libc::socklen_t;
    let rc = unsafe {
        libc::getsockopt(
            fd,
            libc::IPPROTO_TCP,
            TCP_CONNECTION_INFO,
            buf.as_mut_ptr() as *mut libc::c_void,
            &mut len,
        )
    };
    if rc == 0 && (len as usize) >= 48 {
        let rtt = u32::from_ne_bytes([buf[44], buf[45], buf[46], buf[47]]);
        if rtt > 0 {
            return Some(rtt.max(1));
        }
    }
    None
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
pub fn tcp_rtt_ms(_fd: i32) -> Option<u32> {
    None
}

pub async fn run_tcp_server(
    listener: TcpListener,
    state: Arc<TcpState>,
    ipc: Arc<IpcClient>,
    shutdown: Arc<Notify>,
) {
    loop {
        tokio::select! {
            _ = shutdown.notified() => break,
            res = listener.accept() => match res {
                Ok((stream, peer)) => {
                    let st = state.clone();
                    let ic = ipc.clone();
                    let sd = shutdown.clone();
                    tokio::spawn(async move {
                        handle_client(stream, peer, st, ic, sd).await;
                    });
                }
                Err(e) => {
                    eprintln!("[varden-proxy] TCP accept error: {e}");
                    tokio::time::sleep(Duration::from_millis(25)).await;
                }
            }
        }
    }
}

enum InitialRoute {
    Close,
    Http {
        method: String,
        path: String,
    },
    Mc {
        effective_peer: SocketAddr,
        edition: &'static str,
        protocol: i32,
        hostname: String,
        next_state: i32,
        player: Option<String>,
        player_uuid: Option<String>,
        buffered: Vec<u8>,
    },
}

async fn read_initial_route(
    reader: &mut PrefixedReader<TcpStream>,
    tcp_peer: SocketAddr,
) -> Result<InitialRoute, String> {
    let (first, effective_peer, edition) = read_inbound_prefix(reader, tcp_peer).await?;
    if first == 0xFE {
        return Ok(InitialRoute::Close);
    }

    let (raw_hs, body) = if first == b'G' || first == b'H' {
        let more = reader
            .read_exact_vec(3)
            .await
            .map_err(|e| e.to_string())?;
        let prefix4 = [first, more[0], more[1], more[2]];
        if &prefix4 == b"GET " || &prefix4 == b"HEAD" {
            let head = reader
                .read_until_double_crlf(&prefix4, MAX_HTTP_HEADER)
                .await?;
            let first_line_bytes = head
                .split(|&b| b == b'\r' || b == b'\n')
                .next()
                .unwrap_or(&[]);
            let first_line = String::from_utf8_lossy(first_line_bytes);
            let mut parts = first_line.split(' ');
            let method = parts.next().unwrap_or("").to_string();
            let path = parts.next().unwrap_or("/").to_string();
            return Ok(InitialRoute::Http { method, path });
        }
        let length = first as usize;
        let rest = reader
            .read_exact_vec(length - 3)
            .await
            .map_err(|e| e.to_string())?;
        let mut body = Vec::with_capacity(length);
        body.extend_from_slice(&more);
        body.extend_from_slice(&rest);
        let mut raw_hs = Vec::with_capacity(1 + length);
        raw_hs.push(first);
        raw_hs.extend_from_slice(&body);
        (raw_hs, body)
    } else {
        let (length, raw_rest) = finish_varint(first, reader).await?;
        if length <= 0 || (length as usize) > MAX_HANDSHAKE {
            return Err("bad handshake length".to_string());
        }
        let body = reader
            .read_exact_vec(length as usize)
            .await
            .map_err(|e| e.to_string())?;
        let mut raw_hs = Vec::with_capacity(1 + raw_rest.len() + body.len());
        raw_hs.push(first);
        raw_hs.extend_from_slice(&raw_rest);
        raw_hs.extend_from_slice(&body);
        (raw_hs, body)
    };

    let (protocol, hostname, next_state) = parse_handshake(&body)?;
    let mut buffered = raw_hs;
    let mut player = None;
    let mut player_uuid = None;
    if next_state == 2 || next_state == 3 {
        let (raw_login, login_body) = read_packet(reader).await?;
        buffered.extend_from_slice(&raw_login);
        let (p, u) = parse_login_start(&login_body, protocol);
        player = p;
        player_uuid = u;
    }

    Ok(InitialRoute::Mc {
        effective_peer,
        edition,
        protocol,
        hostname,
        next_state,
        player,
        player_uuid,
        buffered,
    })
}

async fn handle_client(
    client: TcpStream,
    tcp_peer: SocketAddr,
    state: Arc<TcpState>,
    ipc: Arc<IpcClient>,
    shutdown: Arc<Notify>,
) {
    let _ = client.set_nodelay(true);
    state.total.fetch_add(1, Ordering::Relaxed);
    let tcp_local = client
        .local_addr()
        .unwrap_or_else(|_| SocketAddr::from(([0, 0, 0, 0], 25565)));
    let tcp_loopback = is_loopback_ip(tcp_peer.ip());
    let conn_id = state.next_conn_id.fetch_add(1, Ordering::Relaxed);

    let mut reader = PrefixedReader::new(client);
    let route = match tokio::time::timeout(
        HANDSHAKE_TIMEOUT,
        read_initial_route(&mut reader, tcp_peer),
    )
    .await
    {
        Ok(Ok(r)) => r,
        _ => return,
    };

    match route {
        InitialRoute::Close => {}
        InitialRoute::Http { method, path } => {
            serve_http(&mut reader, &method, &path, &state, &ipc, &shutdown).await;
        }
        InitialRoute::Mc {
            effective_peer,
            edition,
            protocol,
            hostname,
            next_state,
            player,
            player_uuid,
            buffered,
        } => {
            let req_id = ipc.next_request_id();
            let norm_peer_ip = normalize_ip(effective_peer.ip()).to_string();
            let norm_local_ip = normalize_ip(tcp_local.ip()).to_string();
            let msg = OutboundMsg::McHandshake {
                id: req_id,
                conn_id,
                peer_ip: norm_peer_ip,
                peer_port: effective_peer.port(),
                local_ip: norm_local_ip,
                local_port: tcp_local.port(),
                tcp_loopback,
                edition: edition.to_string(),
                protocol,
                hostname,
                next_state,
                player,
                player_uuid,
            };
            let Ok(decision) = ipc.request::<McDecision>(req_id, &msg).await else {
                return;
            };
            match decision {
                McDecision::Forward {
                    target_port,
                    send_proxy_v2,
                    track_sid,
                    player,
                    timeout_secs,
                    sniff_seconds,
                    sniff_max_bytes,
                } => {
                    let conn_res = tokio::time::timeout(
                        Duration::from_secs(5),
                        TcpStream::connect(("127.0.0.1", target_port)),
                    )
                    .await;
                    let mut upstream = match conn_res {
                        Ok(Ok(s)) => s,
                        _ => {
                            let fid = ipc.next_request_id();
                            if let Ok(fallback) = ipc
                                .request::<McDecision>(
                                    fid,
                                    &OutboundMsg::McUpstreamFailed { id: fid, conn_id },
                                )
                                .await
                            {
                                handle_non_forward_decision(fallback, &mut reader).await;
                            }
                            return;
                        }
                    };
                    let _ = upstream.set_nodelay(true);

                    if send_proxy_v2 {
                        let hdr = build_proxy_v2_header(effective_peer, tcp_local);
                        if upstream.write_all(&hdr).await.is_err() {
                            let _ = ipc.notify(&OutboundMsg::ConnClosed { conn_id }).await;
                            return;
                        }
                    }

                    let (leftover, client) = reader.into_parts();
                    if upstream.write_all(&buffered).await.is_err()
                        || (!leftover.is_empty() && upstream.write_all(&leftover).await.is_err())
                        || upstream.flush().await.is_err()
                    {
                        let _ = ipc.notify(&OutboundMsg::ConnClosed { conn_id }).await;
                        return;
                    }

                    #[cfg(unix)]
                    let raw_fd = client.as_raw_fd();
                    #[cfg(not(unix))]
                    let raw_fd: i32 = -1;

                    let disconnect = Arc::new(Notify::new());
                    let sniff_active =
                        Arc::new(AtomicBool::new(sniff_seconds > 0 && sniff_max_bytes > 0));
                    state.insert_active(
                        conn_id,
                        ActiveConn {
                            track_sid,
                            player_lower: player.map(|p| p.to_lowercase()),
                            tcp_loopback,
                            raw_fd,
                            disconnect: disconnect.clone(),
                            sniff_active: sniff_active.clone(),
                        },
                    );

                    if ipc.notify(&OutboundMsg::ConnOpened { conn_id }).await.is_err() {
                        state.remove_active(conn_id);
                        return;
                    }

                    pipe_streams(
                        client,
                        upstream,
                        conn_id,
                        state,
                        ipc,
                        disconnect,
                        sniff_active,
                        timeout_secs,
                        sniff_seconds,
                        sniff_max_bytes,
                        shutdown,
                    )
                    .await;
                }
                other => {
                    handle_non_forward_decision(other, &mut reader).await;
                }
            }
        }
    }
}

async fn handle_non_forward_decision(
    decision: McDecision,
    reader: &mut PrefixedReader<TcpStream>,
) {
    match decision {
        McDecision::Status { json } => {
            if tokio::time::timeout(HANDSHAKE_TIMEOUT, read_packet(reader))
                .await
                .is_ok_and(|r| r.is_ok())
            {
                let resp = encode_packet(0, &encode_mc_string(&json));
                if reader.inner_mut().write_all(&resp).await.is_ok()
                    && reader.inner_mut().flush().await.is_ok()
                {
                    if let Ok(Ok((_, ping_body))) =
                        tokio::time::timeout(Duration::from_secs(3), read_packet(reader)).await
                    {
                        if let Ok((pid, pos)) = parse_varint(&ping_body, 0) {
                            if pid == 1 && ping_body.len() >= pos + 8 {
                                let pong = encode_packet(1, &ping_body[pos..pos + 8]);
                                let _ = reader.inner_mut().write_all(&pong).await;
                                let _ = reader.inner_mut().flush().await;
                            }
                        }
                    }
                }
            }
            let _ = reader.inner_mut().shutdown().await;
        }
        McDecision::Disconnect { json } => {
            let pkt = encode_packet(0, &encode_mc_string(&json));
            let _ = reader.inner_mut().write_all(&pkt).await;
            let _ = reader.inner_mut().flush().await;
            let _ = reader.inner_mut().shutdown().await;
        }
        McDecision::Close | McDecision::Forward { .. } => {
            let _ = reader.inner_mut().shutdown().await;
        }
    }
}

async fn serve_http(
    reader: &mut PrefixedReader<TcpStream>,
    method: &str,
    path: &str,
    state: &TcpState,
    ipc: &IpcClient,
    shutdown: &Notify,
) {
    let req_id = ipc.next_request_id();
    let msg = OutboundMsg::HttpRequest {
        id: req_id,
        method: method.to_string(),
        path: path.to_string(),
    };
    let Ok(decision) = ipc.request::<HttpDecision>(req_id, &msg).await else {
        return;
    };

    let sock = reader.inner_mut();
    match decision {
        HttpDecision::File {
            status,
            headers,
            path: file_path,
            ..
        } => {
            let mut file = if method != "HEAD" {
                match File::open(&file_path).await {
                    Ok(f) => Some(f),
                    Err(_) => return,
                }
            } else {
                None
            };
            let head = build_http_head(&status, &headers);
            if sock.write_all(head.as_bytes()).await.is_err() {
                return;
            }
            if let Some(ref mut f) = file {
                let mut buf = [0u8; 65536];
                loop {
                    let read_res = tokio::select! {
                        _ = shutdown.notified() => return,
                        r = f.read(&mut buf) => r,
                    };
                    let n = match read_res {
                        Ok(0) => break,
                        Ok(n) => n,
                        Err(_) => return,
                    };
                    if sock.write_all(&buf[..n]).await.is_err() {
                        return;
                    }
                    state.bytes_down.fetch_add(n as u64, Ordering::Relaxed);
                }
            }
            if sock.flush().await.is_ok() {
                state.packs_served.fetch_add(1, Ordering::Relaxed);
            }
            let _ = sock.shutdown().await;
        }
        HttpDecision::Body {
            status,
            headers,
            body_b64,
        } => {
            let head = build_http_head(&status, &headers);
            if sock.write_all(head.as_bytes()).await.is_err() {
                return;
            }
            if method != "HEAD" {
                if let Ok(body) = STANDARD.decode(&body_b64) {
                    if sock.write_all(&body).await.is_err() {
                        return;
                    }
                }
            }
            let _ = sock.flush().await;
            let _ = sock.shutdown().await;
        }
    }
}

fn build_http_head(status: &str, headers: &[(String, String)]) -> String {
    let mut head = format!("HTTP/1.1 {status}\r\n");
    for (k, v) in headers {
        head.push_str(k);
        head.push_str(": ");
        head.push_str(v);
        head.push_str("\r\n");
    }
    head.push_str("Connection: close\r\nServer: Varden\r\n\r\n");
    head
}

#[allow(clippy::too_many_arguments)]
async fn pipe_streams(
    client: TcpStream,
    upstream: TcpStream,
    conn_id: u64,
    state: Arc<TcpState>,
    ipc: Arc<IpcClient>,
    disconnect: Arc<Notify>,
    sniff_active: Arc<AtomicBool>,
    timeout_secs: Option<u64>,
    sniff_seconds: u64,
    sniff_max_bytes: usize,
    shutdown: Arc<Notify>,
) {
    let (mut c_r, mut c_w) = client.into_split();
    let (mut s_r, mut s_w) = upstream.into_split();

    {
        let c2s = copy_c2s(
            &mut c_r,
            &mut s_w,
            &state,
            &ipc,
            conn_id,
            &sniff_active,
            sniff_seconds,
            sniff_max_bytes,
        );
        let s2c = copy_s2c(&mut s_r, &mut c_w, &state);
        tokio::pin!(c2s);
        tokio::pin!(s2c);

        let lobby_sleep = async {
            match timeout_secs {
                Some(secs) => tokio::time::sleep(Duration::from_secs(secs)).await,
                None => std::future::pending::<()>().await,
            }
        };
        tokio::pin!(lobby_sleep);

        tokio::select! {
            _ = &mut c2s => {
                tokio::select! {
                    _ = tokio::time::timeout(Duration::from_secs(5), &mut s2c) => {}
                    _ = disconnect.notified() => {}
                    _ = &mut lobby_sleep => {}
                    _ = shutdown.notified() => {}
                }
            }
            _ = &mut s2c => {
                tokio::select! {
                    _ = tokio::time::timeout(Duration::from_secs(5), &mut c2s) => {}
                    _ = disconnect.notified() => {}
                    _ = &mut lobby_sleep => {}
                    _ = shutdown.notified() => {}
                }
            }
            _ = disconnect.notified() => {}
            _ = &mut lobby_sleep => {}
            _ = shutdown.notified() => {}
        }
    }

    // Remove from active BEFORE dropping socket halves so raw_fd cannot close while inspected.
    state.remove_active(conn_id);
    drop(c_r);
    drop(c_w);
    drop(s_r);
    drop(s_w);

    let _ = ipc.notify(&OutboundMsg::ConnClosed { conn_id }).await;
}

#[allow(clippy::too_many_arguments)]
async fn copy_c2s(
    src: &mut OwnedReadHalf,
    dst: &mut OwnedWriteHalf,
    state: &TcpState,
    ipc: &IpcClient,
    conn_id: u64,
    sniff_active: &AtomicBool,
    sniff_seconds: u64,
    sniff_max_bytes: usize,
) {
    let mut buf = [0u8; 65536];
    let sniff_deadline = Instant::now() + Duration::from_secs(sniff_seconds);
    let mut sniff_remaining = sniff_max_bytes;

    loop {
        let Ok(n) = src.read(&mut buf).await else {
            break;
        };
        if n == 0 {
            break;
        }
        state.bytes_up.fetch_add(n as u64, Ordering::Relaxed);
        if sniff_active.load(Ordering::Relaxed) {
            if Instant::now() <= sniff_deadline && sniff_remaining > 0 {
                let take = n.min(sniff_remaining);
                sniff_remaining -= take;
                let _ = ipc.try_sniff(conn_id, STANDARD.encode(&buf[..take]));
                if sniff_remaining == 0 {
                    sniff_active.store(false, Ordering::Relaxed);
                }
            } else {
                sniff_active.store(false, Ordering::Relaxed);
            }
        }
        if dst.write_all(&buf[..n]).await.is_err() {
            break;
        }
    }
    let _ = dst.shutdown().await;
}

async fn copy_s2c(src: &mut OwnedReadHalf, dst: &mut OwnedWriteHalf, state: &TcpState) {
    let mut buf = [0u8; 65536];
    loop {
        let Ok(n) = src.read(&mut buf).await else {
            break;
        };
        if n == 0 {
            break;
        }
        state.bytes_down.fetch_add(n as u64, Ordering::Relaxed);
        if dst.write_all(&buf[..n]).await.is_err() {
            break;
        }
    }
    let _ = dst.shutdown().await;
}
