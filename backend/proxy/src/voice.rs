use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};

use tokio::net::UdpSocket;
use tokio::sync::Notify;
use tokio::task::JoinHandle;

const MAGIC: u8 = 0xFF;
const IDLE_TIMEOUT_MS: u64 = 90_000;
const CLEANUP_INTERVAL: Duration = Duration::from_secs(30);

struct VoiceRouting {
    servers: HashMap<String, u16>,
    players: HashMap<[u8; 16], String>,
}

impl VoiceRouting {
    fn resolve(&self, uuid: &[u8; 16]) -> Option<(String, u16)> {
        if let Some(sid) = self.players.get(uuid) {
            if let Some(&port) = self.servers.get(sid) {
                return Some((sid.clone(), port));
            }
        }
        if self.servers.len() == 1 {
            if let Some((sid, &port)) = self.servers.iter().next() {
                return Some((sid.clone(), port));
            }
        }
        None
    }
}

struct VoiceSession {
    sid: String,
    target_port: u16,
    upstream: Arc<UdpSocket>,
    last_seen: Arc<AtomicU64>,
    task: JoinHandle<()>,
}

pub struct VoiceSnapshot {
    pub listening: bool,
    pub error: Option<String>,
    pub sessions: usize,
    pub bytes_up: u64,
    pub bytes_down: u64,
    pub dropped: u64,
    pub per_server: HashMap<String, usize>,
}

pub struct VoiceState {
    start: Instant,
    pub listening: AtomicBool,
    pub error: Mutex<Option<String>>,
    pub bytes_up: AtomicU64,
    pub bytes_down: AtomicU64,
    pub dropped: AtomicU64,
    routing: RwLock<VoiceRouting>,
    sessions: Mutex<HashMap<SocketAddr, VoiceSession>>,
}

impl VoiceState {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            start: Instant::now(),
            listening: AtomicBool::new(false),
            error: Mutex::new(None),
            bytes_up: AtomicU64::new(0),
            bytes_down: AtomicU64::new(0),
            dropped: AtomicU64::new(0),
            routing: RwLock::new(VoiceRouting {
                servers: HashMap::new(),
                players: HashMap::new(),
            }),
            sessions: Mutex::new(HashMap::new()),
        })
    }

    pub fn now_ms(&self) -> u64 {
        self.start.elapsed().as_millis() as u64
    }

    pub fn set_error(&self, err: Option<String>) {
        *self.error.lock().unwrap() = err;
    }

    pub fn sync(&self, servers: HashMap<String, u16>, players_hex: HashMap<String, String>) {
        let mut players = HashMap::with_capacity(players_hex.len());
        for (uid_str, sid) in players_hex {
            if let Some(uid) = parse_uuid_bytes(&uid_str) {
                players.insert(uid, sid);
            }
        }
        let mut r = self.routing.write().unwrap();
        r.servers = servers;
        r.players = players;
    }

    pub fn bind(&self, uuid_hex: &str, sid: String) {
        if let Some(uid) = parse_uuid_bytes(uuid_hex) {
            self.routing.write().unwrap().players.insert(uid, sid);
        }
    }

    pub fn snapshot(&self) -> VoiceSnapshot {
        let (sessions, per_server) = {
            let guard = self.sessions.lock().unwrap();
            let mut per_server: HashMap<String, usize> = HashMap::new();
            for s in guard.values() {
                *per_server.entry(s.sid.clone()).or_insert(0) += 1;
            }
            (guard.len(), per_server)
        };
        let error = self.error.lock().unwrap().clone();
        VoiceSnapshot {
            listening: self.listening.load(Ordering::Relaxed),
            error,
            sessions,
            bytes_up: self.bytes_up.load(Ordering::Relaxed),
            bytes_down: self.bytes_down.load(Ordering::Relaxed),
            dropped: self.dropped.load(Ordering::Relaxed),
            per_server,
        }
    }

    pub fn shutdown_all(&self) {
        let drained: Vec<VoiceSession> = {
            let mut guard = self.sessions.lock().unwrap();
            guard.drain().map(|(_, v)| v).collect()
        };
        for s in drained {
            s.task.abort();
        }
    }
}

pub fn parse_uuid_bytes(s: &str) -> Option<[u8; 16]> {
    let mut hex_bytes = [0u8; 32];
    let mut len = 0usize;
    for b in s.bytes() {
        if b == b'-' {
            continue;
        }
        if len >= 32 {
            return None;
        }
        hex_bytes[len] = b;
        len += 1;
    }
    if len != 32 {
        return None;
    }
    let mut out = [0u8; 16];
    for i in 0..16 {
        let hi = hex_nibble(hex_bytes[i * 2])?;
        let lo = hex_nibble(hex_bytes[i * 2 + 1])?;
        out[i] = (hi << 4) | lo;
    }
    Some(out)
}

fn hex_nibble(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

pub async fn run_udp_server(
    socket: UdpSocket,
    state: Arc<VoiceState>,
    shutdown: Arc<Notify>,
) {
    let public_sock = Arc::new(socket);
    let mut buf = [0u8; 65535];

    loop {
        let (n, addr) = tokio::select! {
            _ = shutdown.notified() => break,
            res = public_sock.recv_from(&mut buf) => match res {
                Ok(pair) => pair,
                Err(e) => {
                    eprintln!("[varden-proxy] UDP recv error: {e}");
                    tokio::time::sleep(Duration::from_millis(10)).await;
                    continue;
                }
            }
        };

        state.bytes_up.fetch_add(n as u64, Ordering::Relaxed);

        let existing = {
            let guard = state.sessions.lock().unwrap();
            guard.get(&addr).map(|s| {
                (
                    s.sid.clone(),
                    s.target_port,
                    s.upstream.clone(),
                    s.last_seen.clone(),
                )
            })
        };

        if let Some((sid, port, upstream, last_seen)) = existing {
            let still_valid = {
                let r = state.routing.read().unwrap();
                r.servers.get(&sid).copied() == Some(port)
            };
            if still_valid {
                last_seen.store(state.now_ms(), Ordering::Relaxed);
                let _ = upstream.send(&buf[..n]).await;
                continue;
            }
            let removed = state.sessions.lock().unwrap().remove(&addr);
            if let Some(s) = removed {
                s.task.abort();
            }
        }

        if n < 17 || buf[0] != MAGIC {
            state.dropped.fetch_add(1, Ordering::Relaxed);
            continue;
        }
        let mut uuid = [0u8; 16];
        uuid.copy_from_slice(&buf[1..17]);
        let target = {
            let r = state.routing.read().unwrap();
            r.resolve(&uuid)
        };
        let Some((sid, target_port)) = target else {
            state.dropped.fetch_add(1, Ordering::Relaxed);
            continue;
        };

        let Ok(up) = UdpSocket::bind(("127.0.0.1", 0)).await else {
            state.dropped.fetch_add(1, Ordering::Relaxed);
            continue;
        };
        if up.connect(("127.0.0.1", target_port)).await.is_err() {
            state.dropped.fetch_add(1, Ordering::Relaxed);
            continue;
        }

        let upstream = Arc::new(up);
        let last_seen = Arc::new(AtomicU64::new(state.now_ms()));

        let up_rx = upstream.clone();
        let pub_tx = public_sock.clone();
        let st_bg = state.clone();
        let ls_bg = last_seen.clone();
        let task = tokio::spawn(async move {
            let mut rbuf = [0u8; 65535];
            loop {
                let Ok(rn) = up_rx.recv(&mut rbuf).await else {
                    break;
                };
                ls_bg.store(st_bg.now_ms(), Ordering::Relaxed);
                if pub_tx.send_to(&rbuf[..rn], addr).await.is_ok() {
                    st_bg.bytes_down.fetch_add(rn as u64, Ordering::Relaxed);
                }
            }
        });

        let stale = {
            let mut guard = state.sessions.lock().unwrap();
            guard.insert(
                addr,
                VoiceSession {
                    sid,
                    target_port,
                    upstream: upstream.clone(),
                    last_seen,
                    task,
                },
            )
        };
        if let Some(old) = stale {
            old.task.abort();
        }
        let _ = upstream.send(&buf[..n]).await;
    }

    state.shutdown_all();
}

pub async fn run_janitor(state: Arc<VoiceState>, shutdown: Arc<Notify>) {
    let mut ticker = tokio::time::interval(CLEANUP_INTERVAL);
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        tokio::select! {
            _ = shutdown.notified() => break,
            _ = ticker.tick() => {
                let now = state.now_ms();
                let servers = {
                    let r = state.routing.read().unwrap();
                    r.servers.clone()
                };
                let expired: Vec<VoiceSession> = {
                    let mut guard = state.sessions.lock().unwrap();
                    let keys: Vec<SocketAddr> = guard
                        .iter()
                        .filter_map(|(&addr, s)| {
                            let last = s.last_seen.load(Ordering::Relaxed);
                            let idle = now.saturating_sub(last) > IDLE_TIMEOUT_MS;
                            let missing = servers.get(&s.sid).copied() != Some(s.target_port);
                            if idle || missing {
                                Some(addr)
                            } else {
                                None
                            }
                        })
                        .collect();
                    keys.into_iter().filter_map(|k| guard.remove(&k)).collect()
                };
                for s in expired {
                    s.task.abort();
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_uuid_variants() {
        let dashed = "550e8400-e29b-41d4-a716-446655440000";
        let plain = "550e8400e29b41d4a716446655440000";
        let a = parse_uuid_bytes(dashed).unwrap();
        let b = parse_uuid_bytes(plain).unwrap();
        assert_eq!(a, b);
        assert_eq!(a[0], 0x55);
        assert_eq!(a[15], 0x00);
        assert!(parse_uuid_bytes("bad").is_none());
    }
}
