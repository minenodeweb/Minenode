# minenode-proxy

High-performance Rust (`tokio`) transport data plane for MineNode:

* **TCP reverse proxy** (`25565/tcp` by default): inbound PROXY protocol v2 parsing (loopback only), Minecraft Handshake / Login Start framing, outbound PROXY protocol v2, bidirectional stream piping, kernel `TCP_INFO` RTT sampling, non-blocking client-to-server burst tee for passive fingerprinting, and direct-from-disk HTTP resource pack streaming.
* **UDP Simple Voice Chat proxy** (`24454/udp` by default): routes datagrams by the 16-byte player UUID prefix (`0xFF` + UUID) completely in Rust with zero IPC calls on the packet hot path.

Compiled during the Docker image build (`Dockerfile` `rust_proxy` stage) to `/app/bin/minenode-proxy` and supervised by `backend/app/proxy.py` over an authenticated newline-delimited JSON IPC channel (Unix domain socket on Linux/macOS, loopback TCP on Windows).
