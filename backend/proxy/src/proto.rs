use std::io;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
use std::time::Duration;

use tokio::io::{AsyncRead, AsyncReadExt};

pub const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(8);
pub const MAX_HANDSHAKE: usize = 2048;
pub const MAX_HTTP_HEADER: usize = 8192;
pub const PROXY_V2_SIG: &[u8; 12] = b"\r\n\r\n\x00\r\nQUIT\n";

pub struct PrefixedReader<R> {
    inner: R,
    buf: Vec<u8>,
    pos: usize,
}

impl<R: AsyncRead + Unpin> PrefixedReader<R> {
    pub fn new(inner: R) -> Self {
        Self {
            inner,
            buf: Vec::new(),
            pos: 0,
        }
    }

    pub fn prepend(&mut self, bytes: &[u8]) {
        if bytes.is_empty() {
            return;
        }
        let mut combined = Vec::with_capacity(bytes.len() + self.buf.len().saturating_sub(self.pos));
        combined.extend_from_slice(bytes);
        if self.pos < self.buf.len() {
            combined.extend_from_slice(&self.buf[self.pos..]);
        }
        self.buf = combined;
        self.pos = 0;
    }

    pub fn inner_mut(&mut self) -> &mut R {
        &mut self.inner
    }

    pub fn into_parts(self) -> (Vec<u8>, R) {
        let leftover = if self.pos < self.buf.len() {
            self.buf[self.pos..].to_vec()
        } else {
            Vec::new()
        };
        (leftover, self.inner)
    }

    pub async fn read_u8(&mut self) -> io::Result<u8> {
        if self.pos < self.buf.len() {
            let b = self.buf[self.pos];
            self.pos += 1;
            if self.pos >= self.buf.len() {
                self.buf.clear();
                self.pos = 0;
            }
            return Ok(b);
        }
        self.inner.read_u8().await
    }

    pub async fn read_exact_vec(&mut self, mut n: usize) -> io::Result<Vec<u8>> {
        if n == 0 {
            return Ok(Vec::new());
        }
        let mut out = Vec::with_capacity(n);
        if self.pos < self.buf.len() {
            let take = n.min(self.buf.len() - self.pos);
            out.extend_from_slice(&self.buf[self.pos..self.pos + take]);
            self.pos += take;
            n -= take;
            if self.pos >= self.buf.len() {
                self.buf.clear();
                self.pos = 0;
            }
        }
        if n > 0 {
            let start = out.len();
            out.resize(start + n, 0);
            self.inner.read_exact(&mut out[start..]).await?;
        }
        Ok(out)
    }

    pub async fn read_until_double_crlf(
        &mut self,
        initial: &[u8],
        max_len: usize,
    ) -> Result<Vec<u8>, String> {
        let mut out = initial.to_vec();
        if let Some(end) = find_double_crlf(&out) {
            out.truncate(end);
            return Ok(out);
        }
        while self.pos < self.buf.len() {
            out.push(self.buf[self.pos]);
            self.pos += 1;
            if out.len() > max_len {
                return Err("http header too large".to_string());
            }
            if out.ends_with(b"\r\n\r\n") {
                if self.pos >= self.buf.len() {
                    self.buf.clear();
                    self.pos = 0;
                }
                return Ok(out);
            }
        }
        self.buf.clear();
        self.pos = 0;

        let mut chunk = [0u8; 1024];
        loop {
            let n = self
                .inner
                .read(&mut chunk)
                .await
                .map_err(|e| e.to_string())?;
            if n == 0 {
                return Err("unexpected EOF reading HTTP header".to_string());
            }
            let prev_len = out.len();
            if prev_len + n > max_len {
                let take = max_len.saturating_sub(prev_len);
                out.extend_from_slice(&chunk[..take]);
                if let Some(end) = find_double_crlf(&out) {
                    out.truncate(end);
                    return Ok(out);
                }
                return Err("http header too large".to_string());
            }
            out.extend_from_slice(&chunk[..n]);
            let search_from = prev_len.saturating_sub(3);
            if let Some(rel) = find_double_crlf(&out[search_from..]) {
                let end = search_from + rel;
                if end < out.len() {
                    self.prepend(&out[end..]);
                    out.truncate(end);
                }
                return Ok(out);
            }
        }
    }
}

fn find_double_crlf(haystack: &[u8]) -> Option<usize> {
    haystack
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .map(|idx| idx + 4)
}

pub fn parse_varint(buf: &[u8], pos: usize) -> Result<(i32, usize), String> {
    let mut value: u32 = 0;
    let mut p = pos;
    for i in 0..5 {
        if p >= buf.len() {
            return Err("truncated varint".to_string());
        }
        let b = buf[p];
        p += 1;
        value |= ((b & 0x7F) as u32) << (7 * i);
        if (b & 0x80) == 0 {
            return Ok((value as i32, p));
        }
    }
    Err("VarInt too long".to_string())
}

pub async fn finish_varint<R: AsyncRead + Unpin>(
    first: u8,
    reader: &mut PrefixedReader<R>,
) -> Result<(i32, Vec<u8>), String> {
    let mut value: u32 = (first & 0x7F) as u32;
    let mut raw = Vec::new();
    if (first & 0x80) == 0 {
        return Ok((value as i32, raw));
    }
    for i in 1..5 {
        let b = reader.read_u8().await.map_err(|e| e.to_string())?;
        raw.push(b);
        value |= ((b & 0x7F) as u32) << (7 * i);
        if (b & 0x80) == 0 {
            return Ok((value as i32, raw));
        }
    }
    Err("VarInt too long".to_string())
}

pub async fn read_varint<R: AsyncRead + Unpin>(
    reader: &mut PrefixedReader<R>,
) -> Result<(i32, Vec<u8>), String> {
    let first = reader.read_u8().await.map_err(|e| e.to_string())?;
    let (val, rest) = finish_varint(first, reader).await?;
    let mut raw = Vec::with_capacity(1 + rest.len());
    raw.push(first);
    raw.extend_from_slice(&rest);
    Ok((val, raw))
}

pub async fn read_packet<R: AsyncRead + Unpin>(
    reader: &mut PrefixedReader<R>,
) -> Result<(Vec<u8>, Vec<u8>), String> {
    let (length, raw_len) = read_varint(reader).await?;
    if length <= 0 || (length as usize) > MAX_HANDSHAKE {
        return Err(format!("bad packet length {length}"));
    }
    let body = reader
        .read_exact_vec(length as usize)
        .await
        .map_err(|e| e.to_string())?;
    let mut raw = Vec::with_capacity(raw_len.len() + body.len());
    raw.extend_from_slice(&raw_len);
    raw.extend_from_slice(&body);
    Ok((raw, body))
}

pub fn parse_string(buf: &[u8], pos: usize, max_len: usize) -> Result<(String, usize), String> {
    let (length, pos) = parse_varint(buf, pos)?;
    if length < 0 {
        return Err("bad string".to_string());
    }
    let len = length as usize;
    if len > max_len * 4 || pos.checked_add(len).map_or(true, |end| end > buf.len()) {
        return Err("bad string".to_string());
    }
    let s = String::from_utf8_lossy(&buf[pos..pos + len]).into_owned();
    Ok((s, pos + len))
}

pub fn encode_varint(n: i32) -> Vec<u8> {
    let mut v = n as u32;
    let mut out = Vec::with_capacity(5);
    loop {
        let b = (v & 0x7F) as u8;
        v >>= 7;
        if v != 0 {
            out.push(b | 0x80);
        } else {
            out.push(b);
            return out;
        }
    }
}

pub fn encode_mc_string(s: &str) -> Vec<u8> {
    let bytes = s.as_bytes();
    let mut out = encode_varint(bytes.len() as i32);
    out.extend_from_slice(bytes);
    out
}

pub fn encode_packet(pid: i32, payload: &[u8]) -> Vec<u8> {
    let pid_bytes = encode_varint(pid);
    let body_len = pid_bytes.len() + payload.len();
    let mut out = encode_varint(body_len as i32);
    out.extend_from_slice(&pid_bytes);
    out.extend_from_slice(payload);
    out
}

pub fn normalize_ip(ip: IpAddr) -> IpAddr {
    match ip {
        IpAddr::V6(v6) => match v6.to_ipv4_mapped() {
            Some(v4) => IpAddr::V4(v4),
            None => IpAddr::V6(v6),
        },
        v4 => v4,
    }
}

pub fn is_loopback_ip(ip: IpAddr) -> bool {
    normalize_ip(ip).is_loopback()
}

pub async fn read_inbound_prefix<R: AsyncRead + Unpin>(
    reader: &mut PrefixedReader<R>,
    tcp_peer: SocketAddr,
) -> Result<(u8, SocketAddr, &'static str), String> {
    let first = reader.read_u8().await.map_err(|e| e.to_string())?;
    if first != b'\r' {
        return Ok((first, tcp_peer, "java"));
    }

    let rest = reader
        .read_exact_vec(PROXY_V2_SIG.len() - 1)
        .await
        .map_err(|e| e.to_string())?;
    if rest.as_slice() != &PROXY_V2_SIG[1..] {
        reader.prepend(&rest);
        return Ok((first, tcp_peer, "java"));
    }

    if !is_loopback_ip(tcp_peer.ip()) {
        return Err("PROXY header from a non-loopback peer".to_string());
    }

    let source = read_proxy_v2(reader).await?;
    let next_first = reader.read_u8().await.map_err(|e| e.to_string())?;
    match source {
        Some(addr) => Ok((next_first, addr, "bedrock")),
        None => Ok((next_first, tcp_peer, "java")),
    }
}

async fn read_proxy_v2<R: AsyncRead + Unpin>(
    reader: &mut PrefixedReader<R>,
) -> Result<Option<SocketAddr>, String> {
    let head = reader.read_exact_vec(4).await.map_err(|e| e.to_string())?;
    let ver_cmd = head[0];
    let fam_proto = head[1];
    let length = u16::from_be_bytes([head[2], head[3]]) as usize;
    if (ver_cmd >> 4) != 2 {
        return Err("unsupported PROXY protocol version".to_string());
    }
    let payload = reader
        .read_exact_vec(length)
        .await
        .map_err(|e| e.to_string())?;
    let cmd = ver_cmd & 0x0F;
    if cmd == 0 {
        return Ok(None);
    }
    if cmd != 1 {
        return Err("invalid PROXY protocol command".to_string());
    }

    let family = fam_proto >> 4;
    if family == 1 && payload.len() >= 12 {
        let ip = Ipv4Addr::new(payload[0], payload[1], payload[2], payload[3]);
        let port = u16::from_be_bytes([payload[8], payload[9]]);
        return Ok(Some(SocketAddr::new(IpAddr::V4(ip), port)));
    }
    if family == 2 && payload.len() >= 36 {
        let octets: [u8; 16] = payload[0..16].try_into().map_err(|_| "bad ipv6 len")?;
        let ip = normalize_ip(IpAddr::V6(Ipv6Addr::from(octets)));
        let port = u16::from_be_bytes([payload[32], payload[33]]);
        return Ok(Some(SocketAddr::new(ip, port)));
    }
    Ok(None)
}

pub fn build_proxy_v2_header(src: SocketAddr, dst: SocketAddr) -> Vec<u8> {
    let s_ip = normalize_ip(src.ip());
    let d_ip = normalize_ip(dst.ip());

    let mut out = Vec::with_capacity(52);
    out.extend_from_slice(PROXY_V2_SIG);

    match (s_ip, d_ip) {
        (IpAddr::V4(s4), IpAddr::V4(d4)) => {
            out.extend_from_slice(&[0x21, 0x11]);
            out.extend_from_slice(&(12u16).to_be_bytes());
            out.extend_from_slice(&s4.octets());
            out.extend_from_slice(&d4.octets());
            out.extend_from_slice(&src.port().to_be_bytes());
            out.extend_from_slice(&dst.port().to_be_bytes());
        }
        (s, d) => {
            let s6 = match s {
                IpAddr::V4(v4) => v4.to_ipv6_mapped(),
                IpAddr::V6(v6) => v6,
            };
            let d6 = match d {
                IpAddr::V4(v4) => v4.to_ipv6_mapped(),
                IpAddr::V6(v6) => v6,
            };
            out.extend_from_slice(&[0x21, 0x21]);
            out.extend_from_slice(&(36u16).to_be_bytes());
            out.extend_from_slice(&s6.octets());
            out.extend_from_slice(&d6.octets());
            out.extend_from_slice(&src.port().to_be_bytes());
            out.extend_from_slice(&dst.port().to_be_bytes());
        }
    }
    out
}

pub fn parse_handshake(body: &[u8]) -> Result<(i32, String, i32), String> {
    let (pid, pos) = parse_varint(body, 0)?;
    if pid != 0 {
        return Err("not a handshake".to_string());
    }
    let (protocol, pos) = parse_varint(body, pos)?;
    let (address, pos) = parse_string(body, pos, 255)?;
    if pos + 2 > body.len() {
        return Err("truncated handshake port".to_string());
    }
    let (next_state, _) = parse_varint(body, pos + 2)?;
    let hostname = address
        .split('\0')
        .next()
        .unwrap_or("")
        .trim()
        .trim_end_matches('.')
        .to_lowercase();
    Ok((protocol, hostname, next_state))
}

pub fn parse_login_start(body: &[u8], protocol: i32) -> (Option<String>, Option<String>) {
    let Ok((lpid, lpos)) = parse_varint(body, 0) else {
        return (None, None);
    };
    if lpid != 0 {
        return (None, None);
    }
    let Ok((player, lpos)) = parse_string(body, lpos, 16) else {
        return (None, None);
    };
    let uuid = if protocol >= 764 && body.len() >= lpos + 16 {
        Some(hex_lower(&body[lpos..lpos + 16]))
    } else if protocol >= 761 && body.len() >= lpos + 17 && body[lpos] == 1 {
        Some(hex_lower(&body[lpos + 1..lpos + 17]))
    } else {
        None
    };
    (Some(player), uuid)
}

pub fn hex_lower(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for &b in bytes {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 0x0F) as usize] as char);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn varint_roundtrip_and_negative() {
        for v in [0, 1, 127, 128, 767, 25565, -1, i32::MAX] {
            let enc = encode_varint(v);
            let (dec, next) = parse_varint(&enc, 0).unwrap();
            assert_eq!(dec, v);
            assert_eq!(next, enc.len());
        }
    }

    #[test]
    fn handshake_and_login_start_parse() {
        let mut hs = Vec::new();
        hs.extend_from_slice(&encode_varint(769));
        hs.extend_from_slice(&encode_mc_string("Survival.Example.COM.\0FML3\0"));
        hs.extend_from_slice(&25565u16.to_be_bytes());
        hs.extend_from_slice(&encode_varint(2));
        let pkt = encode_packet(0, &hs);
        let (len, p) = parse_varint(&pkt, 0).unwrap();
        let (proto, host, state) = parse_handshake(&pkt[p..p + len as usize]).unwrap();
        assert_eq!(proto, 769);
        assert_eq!(host, "survival.example.com");
        assert_eq!(state, 2);

        let mut login_payload = encode_mc_string("Steve");
        login_payload.extend_from_slice(&[0xab; 16]);
        let mut login_body = encode_varint(0);
        login_body.extend_from_slice(&login_payload);
        let (name, uid) = parse_login_start(&login_body, 769);
        assert_eq!(name.as_deref(), Some("Steve"));
        assert_eq!(uid.as_deref(), Some("abababababababababababababababab"));

        let bad_payload = encode_mc_string(&"a".repeat(70));
        let mut bad_body = encode_varint(0);
        bad_body.extend_from_slice(&bad_payload);
        assert_eq!(parse_login_start(&bad_body, 769), (None, None));
    }

    #[tokio::test]
    async fn prefixed_reader_and_proxy_v2_inbound_outbound() {
        let src: SocketAddr = "203.0.113.7:12345".parse().unwrap();
        let dst: SocketAddr = "127.0.0.1:25565".parse().unwrap();
        let mut hdr = build_proxy_v2_header(src, dst);
        hdr.push(0x10);
        let mut r = PrefixedReader::new(hdr.as_slice());
        let (first, peer, edition) = read_inbound_prefix(&mut r, "127.0.0.1:50000".parse().unwrap())
            .await
            .unwrap();
        assert_eq!(first, 0x10);
        assert_eq!(peer, src);
        assert_eq!(edition, "bedrock");

        // IPv4-mapped IPv6 source unmaps cleanly and loopback check covers ::ffff:127.0.0.1
        assert!(is_loopback_ip("::ffff:127.0.0.1".parse().unwrap()));
        assert!(!is_loopback_ip("203.0.113.7".parse().unwrap()));

        let mut v6_payload = Vec::new();
        v6_payload.extend_from_slice(&Ipv4Addr::new(203, 0, 113, 7).to_ipv6_mapped().octets());
        v6_payload.extend_from_slice(&Ipv6Addr::LOCALHOST.octets());
        v6_payload.extend_from_slice(&12345u16.to_be_bytes());
        v6_payload.extend_from_slice(&25565u16.to_be_bytes());
        let mut ipv6_hdr = PROXY_V2_SIG.to_vec();
        ipv6_hdr.extend_from_slice(&[0x21, 0x21]);
        ipv6_hdr.extend_from_slice(&36u16.to_be_bytes());
        ipv6_hdr.extend_from_slice(&v6_payload);
        ipv6_hdr.push(0x20);
        let mut r6 = PrefixedReader::new(ipv6_hdr.as_slice());
        let (f6, p6, e6) = read_inbound_prefix(&mut r6, "[::1]:50000".parse().unwrap())
            .await
            .unwrap();
        assert_eq!(f6, 0x20);
        assert_eq!(p6, src);
        assert_eq!(e6, "bedrock");

        // Non-loopback peer sending PROXY v2 is rejected
        let mut r_bad = PrefixedReader::new(hdr.as_slice());
        assert!(read_inbound_prefix(&mut r_bad, "198.51.100.9:50000".parse().unwrap())
            .await
            .is_err());

        // Handshake starting with 0x0d replays remaining bytes
        let hs = b"\rabcdefghijk";
        let mut r_hs = PrefixedReader::new(hs.as_slice());
        let (f_hs, _, e_hs) = read_inbound_prefix(&mut r_hs, "198.51.100.9:50000".parse().unwrap())
            .await
            .unwrap();
        assert_eq!(f_hs, b'\r');
        assert_eq!(e_hs, "java");
        assert_eq!(r_hs.read_exact_vec(11).await.unwrap(), b"abcdefghijk");
    }
}
