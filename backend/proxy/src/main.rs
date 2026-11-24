mod ipc;
mod proto;
mod tcp;
mod voice;

use std::env;
use std::process::ExitCode;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Duration;

use tokio::net::{TcpListener, UdpSocket};
use tokio::sync::Notify;

use crate::ipc::{IpcClient, OutboundMsg};
use crate::tcp::TcpState;
use crate::voice::VoiceState;

fn parse_port_hint(bind: &str, default: u16) -> u16 {
    bind.rsplit(':')
        .next()
        .and_then(|p| p.parse::<u16>().ok())
        .unwrap_or(default)
}

#[cfg(unix)]
async fn wait_signal() {
    use tokio::signal::unix::{signal, SignalKind};
    let mut term = signal(SignalKind::terminate()).ok();
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {}
        _ = async {
            if let Some(ref mut sig) = term {
                sig.recv().await;
            } else {
                std::future::pending::<()>().await;
            }
        } => {}
    }
}

#[cfg(not(unix))]
async fn wait_signal() {
    let _ = tokio::signal::ctrl_c().await;
}

#[tokio::main]
async fn main() -> ExitCode {
    let ipc_addr = match env::var("VARDEN_IPC_ADDR") {
        Ok(v) if !v.is_empty() => v,
        _ => {
            eprintln!("[varden-proxy] VARDEN_IPC_ADDR is required");
            return ExitCode::from(2);
        }
    };
    let ipc_token = match env::var("VARDEN_IPC_TOKEN") {
        Ok(v) if !v.is_empty() => v,
        _ => {
            eprintln!("[varden-proxy] VARDEN_IPC_TOKEN is required");
            return ExitCode::from(2);
        }
    };
    let tcp_bind = env::var("VARDEN_TCP_BIND").unwrap_or_else(|_| "0.0.0.0:25565".to_string());
    let udp_bind = env::var("VARDEN_UDP_BIND").unwrap_or_else(|_| "0.0.0.0:24454".to_string());

    let shutdown = Arc::new(Notify::new());
    let (ipc, ipc_read) = match IpcClient::connect(&ipc_addr, &ipc_token, shutdown.clone()).await {
        Ok(pair) => pair,
        Err(e) => {
            eprintln!("[varden-proxy] IPC connection failed: {e}");
            return ExitCode::from(1);
        }
    };

    let tcp_state = TcpState::new();
    let voice_state = VoiceState::new();

    let (tcp_listener, tcp_port) = match TcpListener::bind(&tcp_bind).await {
        Ok(l) => {
            let port = l.local_addr().map(|a| a.port()).unwrap_or(parse_port_hint(&tcp_bind, 25565));
            tcp_state.listening.store(true, Ordering::Relaxed);
            (Some(l), port)
        }
        Err(e) => {
            eprintln!("[varden-proxy] TCP bind {tcp_bind} failed: {e}");
            tcp_state.set_error(Some(e.to_string()));
            (None, parse_port_hint(&tcp_bind, 25565))
        }
    };

    let (udp_socket, voice_port) = match UdpSocket::bind(&udp_bind).await {
        Ok(s) => {
            let port = s.local_addr().map(|a| a.port()).unwrap_or(parse_port_hint(&udp_bind, 24454));
            voice_state.listening.store(true, Ordering::Relaxed);
            (Some(s), port)
        }
        Err(e) => {
            eprintln!("[varden-proxy] UDP bind {udp_bind} failed: {e}");
            voice_state.set_error(Some(e.to_string()));
            (None, parse_port_hint(&udp_bind, 24454))
        }
    };

    let ready_msg = OutboundMsg::Ready {
        tcp_listening: tcp_state.listening.load(Ordering::Relaxed),
        tcp_error: tcp_state.error.lock().unwrap().clone(),
        tcp_port,
        voice_listening: voice_state.listening.load(Ordering::Relaxed),
        voice_error: voice_state.error.lock().unwrap().clone(),
        voice_port,
    };
    if ipc.notify(&ready_msg).await.is_err() {
        return ExitCode::from(1);
    }

    let sig_shutdown = shutdown.clone();
    tokio::spawn(async move {
        wait_signal().await;
        sig_shutdown.notify_waiters();
    });

    if let Some(listener) = tcp_listener {
        let st = tcp_state.clone();
        let ic = ipc.clone();
        let sd = shutdown.clone();
        tokio::spawn(async move {
            tcp::run_tcp_server(listener, st, ic, sd).await;
        });
    }

    if let Some(socket) = udp_socket {
        let vs = voice_state.clone();
        let sd = shutdown.clone();
        tokio::spawn(async move {
            voice::run_udp_server(socket, vs, sd).await;
        });

        let vs_jan = voice_state.clone();
        let sd_jan = shutdown.clone();
        tokio::spawn(async move {
            voice::run_janitor(vs_jan, sd_jan).await;
        });
    }

    let tel_tcp = tcp_state.clone();
    let tel_voice = voice_state.clone();
    let tel_ipc = ipc.clone();
    let tel_sd = shutdown.clone();
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(Duration::from_secs(1));
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            tokio::select! {
                _ = tel_sd.notified() => break,
                _ = ticker.tick() => {
                    let ts = tel_tcp.snapshot();
                    let vs = tel_voice.snapshot();
                    let msg = OutboundMsg::Telemetry {
                        tcp_listening: ts.listening,
                        tcp_error: ts.error,
                        active: ts.active,
                        total: ts.total,
                        bytes_up: ts.bytes_up,
                        bytes_down: ts.bytes_down,
                        packs_served: ts.packs_served,
                        per_server: ts.per_server,
                        pings: ts.pings,
                        voice_listening: vs.listening,
                        voice_error: vs.error,
                        voice_sessions: vs.sessions,
                        voice_bytes_up: vs.bytes_up,
                        voice_bytes_down: vs.bytes_down,
                        voice_dropped: vs.dropped,
                        voice_per_server: vs.per_server,
                    };
                    if tel_ipc.notify(&msg).await.is_err() {
                        break;
                    }
                }
            }
        }
    });

    let reader_ipc = ipc.clone();
    let reader_tcp = tcp_state.clone();
    let reader_voice = voice_state.clone();
    tokio::select! {
        _ = reader_ipc.run_reader(ipc_read, reader_tcp, reader_voice) => {}
        _ = shutdown.notified() => {}
    }

    shutdown.notify_waiters();
    voice_state.shutdown_all();
    tokio::time::sleep(Duration::from_millis(50)).await;
    ExitCode::SUCCESS
}
