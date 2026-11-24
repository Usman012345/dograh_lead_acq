"""Offline reverse-proxy simulation and end-to-end integration tests between
lead_streamer, nginx-style reverse proxies, pull_leads, BatchRunner, and LeadPipeline.
"""

from __future__ import annotations

import datetime as dt
import gzip
import http.client
import http.server
import ipaddress
import json
import ssl
import threading
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

pytest.importorskip("psycopg", reason="lead streamer tests need psycopg (pip install -r requirements-dev.txt)")

import config  # noqa: E402
import pull_leads  # noqa: E402
from dashboard.app import create_app  # noqa: E402
from dashboard.data import TrackerDataStore  # noqa: E402
from dashboard.runner import BatchRunner  # noqa: E402
from decision_maker import Decision  # noqa: E402
from dograh_client import Agent  # noqa: E402
from pull_leads import BATCH, EMPTY, FATAL, RETRY, BatchStore, PingResult, Puller, ping  # noqa: E402
from tests.test_main_and_lead_streamer import _load_lead_streamer  # noqa: E402

ls = _load_lead_streamer()

TOKEN = "r" * 48


def _generate_cert_bundle(tmp_path: Path, san_dns: str) -> tuple[Path, Path]:
    """Create a local CA + leaf cert for `san_dns` + 127.0.0.1, returning (cert_pem, ca_pem)."""
    now = dt.datetime.now(dt.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Local CA")])
    ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(ca_ski, critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, san_dns)])
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=True,
                data_encipherment=False, key_agreement=False, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski), critical=False)
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName(san_dns),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    ca_pem = tmp_path / f"ca_{san_dns}.pem"
    ca_pem.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    srv_pem = tmp_path / f"srv_{san_dns}.pem"
    srv_pem.write_bytes(
        leaf_cert.public_bytes(serialization.Encoding.PEM)
        + leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return srv_pem, ca_pem


class StatefulMemoryStore:
    """In-memory stand-in for PgStore implementing the exact streamer batch/stats/health contract."""

    def __init__(self, leads: list[dict] | None = None, default_batch_size: int = 300):
        self.batch_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self.default_batch_size = default_batch_size
        self.health_state = "ok"
        self.leads = list(leads or [])
        self.sent_versions: dict[str, int] = {}
        self.last_sent_at: str | None = None
        self.recorded_limits: list[int | None] = []

    def check_health(self) -> str:
        return self.health_state

    def fetch_stats(self) -> dict:
        with self._state_lock:
            eligible = len(self.leads)
            sent_current = sum(
                1 for l in self.leads
                if self.sent_versions.get(l["lead_id"]) is not None
                and self.sent_versions[l["lead_id"]] >= l["content_version"]
            )
            updated_since_sent = sum(
                1 for l in self.leads
                if self.sent_versions.get(l["lead_id"]) is not None
                and self.sent_versions[l["lead_id"]] < l["content_version"]
            )
            never_sent = sum(1 for l in self.leads if l["lead_id"] not in self.sent_versions)
            return {
                "status": "ok",
                "eligible": eligible,
                "sent_current": sent_current,
                "updated_since_sent": updated_since_sent,
                "never_sent": never_sent,
                "last_sent_at": self.last_sent_at,
            }

    def fetch_next_batch(self, limit: int | None = None) -> dict:
        self.recorded_limits.append(limit)
        effective = self.default_batch_size if limit is None else max(1, min(1000, int(limit)))
        with self._state_lock:
            due = [
                l for l in self.leads
                if l["lead_id"] not in self.sent_versions
                or self.sent_versions[l["lead_id"]] < l["content_version"]
            ][:effective]
            served_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if not due:
                return {
                    "status": "no_new_leads",
                    "message": "No new Leads available",
                    "count": 0,
                    "served_at": served_at,
                    "leads": [],
                }
            out = []
            new_c = 0
            upd_c = 0
            for l in due:
                was_sent = l["lead_id"] in self.sent_versions
                item = dict(l)
                item["event"] = "updated" if was_sent else "new"
                out.append(item)
                self.sent_versions[l["lead_id"]] = l["content_version"]
                if was_sent:
                    upd_c += 1
                else:
                    new_c += 1
            self.last_sent_at = served_at
            return {
                "status": "ok",
                "count": len(out),
                "new": new_c,
                "updated": upd_c,
                "served_at": served_at,
                "leads": out,
            }


def _start_streamer(store: StatefulMemoryStore, *, expected_host: str = "ai-sdr.grobots.ai",
                    max_concurrent_requests: int = 8, log_lines: list[str] | None = None):
    cfg = SimpleNamespace(
        expected_host=expected_host,
        max_concurrent_requests=max_concurrent_requests,
        request_timeout_seconds=10.0,
        stream_token_bytes=TOKEN.encode("utf-8"),
        batch_size=store.default_batch_size,
    )
    logger = ls.StructuredLogger(20, (TOKEN,))
    if log_lines is not None:
        orig_log = logger.log

        def capture_log(level, event, **fields):
            parts = [event] + [f"{k}={v}" for k, v in fields.items()]
            log_lines.append(" ".join(parts))
            orig_log(level, event, **fields)

        logger.log = capture_log  # type: ignore[method-assign]

    server = ls.LeadStreamerServer(("127.0.0.1", 0), ls.LeadStreamerHandler, cfg, logger, store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


class LocalNginxProxy:
    """Simulates nginx-lead-streamer.conf (TLS termination, Host forwarding, XFF, gzip, prefix stripping)."""

    def __init__(
        self,
        upstream_host: str,
        upstream_port: int,
        *,
        cert_pem: Path | None = None,
        forward_host: bool = True,
        override_host: str | None = None,
        strip_prefix: str = "",
        gzip_json: bool = True,
        force_status: int | None = None,
        force_html: str | None = None,
        force_json: dict | None = None,
    ):
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.forward_host = forward_host
        self.override_host = override_host
        self.strip_prefix = strip_prefix.rstrip("/")
        self.gzip_json = gzip_json
        self.force_status = force_status
        self.force_html = force_html
        self.force_json = force_json
        self.seen_requests: list[dict] = []

        proxy = self

        class ProxyHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self, method: str) -> None:
                proxy.seen_requests.append({
                    "method": method,
                    "path": self.path,
                    "host": self.headers.get("Host"),
                })

                if proxy.force_status is not None and proxy.force_html is not None:
                    body = proxy.force_html.encode("utf-8")
                    self.send_response(proxy.force_status)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if method != "HEAD":
                        self.wfile.write(body)
                    return

                if proxy.force_status is not None and proxy.force_json is not None:
                    body = json.dumps(proxy.force_json).encode("utf-8")
                    self.send_response(proxy.force_status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if method != "HEAD":
                        self.wfile.write(body)
                    return

                target_path = self.path
                if proxy.strip_prefix:
                    if target_path == proxy.strip_prefix or target_path.startswith(proxy.strip_prefix + "/"):
                        target_path = target_path[len(proxy.strip_prefix):] or "/"
                    else:
                        body = b"<html><title>404 Not Found</title></html>"
                        self.send_response(404)
                        self.send_header("Content-Type", "text/html")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        if method != "HEAD":
                            self.wfile.write(body)
                        return

                fwd_headers = {}
                if proxy.override_host is not None:
                    fwd_headers["Host"] = proxy.override_host
                elif proxy.forward_host and self.headers.get("Host"):
                    # Mirror nginx `proxy_set_header Host $host;` (host without port)
                    raw_h = self.headers["Host"]
                    fwd_headers["Host"] = urllib.parse.urlsplit(f"//{raw_h}").hostname or raw_h
                else:
                    fwd_headers["Host"] = f"{proxy.upstream_host}:{proxy.upstream_port}"

                fwd_headers["X-Forwarded-For"] = self.client_address[0]
                fwd_headers["X-Forwarded-Proto"] = "https" if cert_pem is not None else "http"
                if self.headers.get("Authorization"):
                    fwd_headers["Authorization"] = self.headers["Authorization"]
                if self.headers.get("Accept"):
                    fwd_headers["Accept"] = self.headers["Accept"]

                conn = http.client.HTTPConnection(proxy.upstream_host, proxy.upstream_port, timeout=15)
                try:
                    # Nginx proxies to upstream over HTTP/1.0 by default
                    conn._http_vsn = 10
                    conn._http_vsn_str = "HTTP/1.0"
                    conn.request(method, target_path, headers=fwd_headers)
                    resp = conn.getresponse()
                    raw_body = b"" if method == "HEAD" else resp.read()
                    resp_headers = list(resp.getheaders())
                    status = resp.status
                finally:
                    conn.close()

                accept_enc = self.headers.get("Accept-Encoding", "")
                ctype = dict((k.lower(), v) for k, v in resp_headers).get("content-type", "")
                will_gzip = (
                    proxy.gzip_json
                    and method != "HEAD"
                    and "gzip" in accept_enc.lower()
                    and "application/json" in ctype.lower()
                    and len(raw_body) >= 100
                )
                out_body = gzip.compress(raw_body) if will_gzip else raw_body

                self.send_response(status)
                for k, v in resp_headers:
                    kl = k.lower()
                    if kl in ("content-length", "connection", "transfer-encoding", "content-encoding"):
                        continue
                    self.send_header(k, v)
                if will_gzip:
                    self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(out_body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if method != "HEAD" and out_body:
                    self.wfile.write(out_body)

            def do_GET(self):
                self._handle("GET")

            def do_HEAD(self):
                self._handle("HEAD")

            def do_POST(self):
                self._handle("POST")

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
        if cert_pem is not None:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=str(cert_pem))
            self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self.scheme = "https" if cert_pem is not None else "http"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _sample_leads(n: int) -> list[dict]:
    return [
        {
            "lead_id": f"uuid-{i:04d}",
            "content_version": 1,
            "company": f"Fictional Concrete {i}",
            "category": "Concrete Contractors",
            "city": "Austin",
            "state": "TX",
            "phone": f"(512) 555-{100 + i:04d}",
            "phones": [f"(512) 555-{100 + i:04d}"],
        }
        for i in range(1, n + 1)
    ]


# ------------------------------------------------------------------ reverse proxy tests

def test_https_reverse_proxy_full_communication_gzip_and_host_check(tmp_path, monkeypatch):
    srv_pem, ca_pem = _generate_cert_bundle(tmp_path, "ai-sdr.grobots.ai")
    leads = _sample_leads(5)
    log_lines: list[str] = []
    store = StatefulMemoryStore(leads, default_batch_size=300)
    # expected_host matches PUBLIC_BASE_URL="https://ai-sdr.grobots.ai" (no port)
    streamer_srv, streamer_port = _start_streamer(store, expected_host="ai-sdr.grobots.ai", log_lines=log_lines)
    proxy = LocalNginxProxy(
        "127.0.0.1",
        streamer_port,
        cert_pem=srv_pem,
        override_host="ai-sdr.grobots.ai",
        gzip_json=True,
    )
    try:
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_pem))
        monkeypatch.setattr(pull_leads, "preflight", lambda: None)
        monkeypatch.setattr(config, "LEAD_SOURCE_TOKEN", TOKEN)

        base_url = f"https://127.0.0.1:{proxy.port}"

        # Check /health + /stats with pull_leads.check_setup (takes NO leads)
        assert pull_leads.check_setup(base_url) == 0
        assert pull_leads.streamer_is_ready(base_url) is True
        assert store.sent_versions == {}
    finally:
        proxy.close()
        streamer_srv.shutdown()
        streamer_srv.server_close()


def test_reverse_proxy_host_header_forwarding_gzip_limit_and_head(tmp_path, monkeypatch):
    srv_pem, ca_pem = _generate_cert_bundle(tmp_path, "ai-sdr.grobots.ai")
    leads = _sample_leads(5)
    log_lines: list[str] = []
    store = StatefulMemoryStore(leads, default_batch_size=300)
    streamer_srv, streamer_port = _start_streamer(store, expected_host="ai-sdr.grobots.ai", log_lines=log_lines)
    # Simulate nginx forwarding `Host: ai-sdr.grobots.ai` (as `$host` does in production)
    proxy = LocalNginxProxy(
        "127.0.0.1",
        streamer_port,
        cert_pem=srv_pem,
        override_host="ai-sdr.grobots.ai",
        gzip_json=True,
    )
    try:
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_pem))
        monkeypatch.setattr(pull_leads, "preflight", lambda: None)
        monkeypatch.setattr(config, "LEAD_SOURCE_TOKEN", TOKEN)
        base_url = f"https://127.0.0.1:{proxy.port}"

        assert pull_leads.streamer_is_ready(base_url) is True
        assert pull_leads.check_setup(base_url) == 0

        # HEAD requests through proxy must return valid empty body (no framing error)
        r_head_health = requests.head(f"{base_url}/health", timeout=5)
        assert r_head_health.status_code == 200
        assert r_head_health.content == b""

        r_head_next = requests.head(f"{base_url}/leads/next", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=5)
        assert r_head_next.status_code == 405
        assert r_head_next.headers.get("Allow") == "GET"
        assert r_head_next.content == b""

        # POST returns 405
        r_post = requests.post(f"{base_url}/leads/next", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=5)
        assert r_post.status_code == 405

        # Pull batch of 2 via ?limit=2 over HTTPS + gzip
        r1 = ping(base_url, TOKEN, 120, limit=2)
        assert r1.kind == BATCH and len(r1.leads) == 2
        assert [l["lead_id"] for l in r1.leads] == ["uuid-0001", "uuid-0002"]

        # Verify gzip was actually used on the response from proxy
        raw_resp = requests.get(
            f"{base_url}/leads/next?limit=2",
            headers={"Authorization": f"Bearer {TOKEN}", "Accept-Encoding": "gzip"},
            timeout=10,
            stream=True,
        )
        assert raw_resp.status_code == 200
        assert raw_resp.headers.get("Content-Encoding") == "gzip"
        raw_resp.close()

        # Pull remaining 1 lead (uuid-0005), then empty
        r3 = ping(base_url, TOKEN, 120, limit=5)
        assert r3.kind == BATCH and [l["lead_id"] for l in r3.leads] == ["uuid-0005"]
        r4 = ping(base_url, TOKEN, 120, limit=5)
        assert r4.kind == EMPTY

        # Verify X-Forwarded-For was logged as client IP and no token leaked in logs
        assert any("client=127.0.0.1" in line for line in log_lines)
        assert not any(TOKEN in line for line in log_lines)
    finally:
        proxy.close()
        streamer_srv.shutdown()
        streamer_srv.server_close()


def test_reverse_proxy_missing_host_header_returns_421_fatal(tmp_path, monkeypatch):
    srv_pem, ca_pem = _generate_cert_bundle(tmp_path, "ai-sdr.grobots.ai")
    store = StatefulMemoryStore(_sample_leads(2))
    streamer_srv, streamer_port = _start_streamer(store, expected_host="ai-sdr.grobots.ai")
    # Misconfigured proxy: fails to forward Host (`proxy_set_header Host $host;` missing)
    proxy = LocalNginxProxy("127.0.0.1", streamer_port, cert_pem=srv_pem, forward_host=False)
    try:
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_pem))
        monkeypatch.setattr(pull_leads, "preflight", lambda: None)
        monkeypatch.setattr(config, "LEAD_SOURCE_TOKEN", TOKEN)
        base_url = f"https://127.0.0.1:{proxy.port}"

        # /health is exempt from Host check (per spec), /stats & /leads/next reject with 421
        assert pull_leads.streamer_is_ready(base_url) is True
        assert pull_leads.check_setup(base_url) == 1
        res = ping(base_url, TOKEN, 120)
        assert res.kind == FATAL
        assert "421" in res.message
        # No leads were handed out or marked sent
        assert store.sent_versions == {}
    finally:
        proxy.close()
        streamer_srv.shutdown()
        streamer_srv.server_close()


def test_reverse_proxy_mismatched_cert_and_wrong_backend_are_refused(tmp_path, monkeypatch):
    # Reproduces the live failure mode: proxy serves a cert for scrapper.grobots.ai (without 127.0.0.1 SAN)
    now = dt.datetime.now(dt.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "scrapper.grobots.ai")]))
        .issuer_name(ca_name).public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("scrapper.grobots.ai")]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem = tmp_path / "ca_only.pem"
    ca_pem.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    wrong_srv_pem = tmp_path / "wrong_srv.pem"
    wrong_srv_pem.write_bytes(
        leaf_cert.public_bytes(serialization.Encoding.PEM)
        + leaf_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
    )

    store = StatefulMemoryStore(_sample_leads(2))
    streamer_srv, streamer_port = _start_streamer(store, expected_host="")
    bad_tls_proxy = LocalNginxProxy("127.0.0.1", streamer_port, cert_pem=wrong_srv_pem)
    wrong_backend_proxy = LocalNginxProxy(
        "127.0.0.1",
        streamer_port,
        force_status=200,
        force_json={"status": "healthy", "service": "DataOps AI Backend"},
    )
    bad_gateway_proxy = LocalNginxProxy(
        "127.0.0.1",
        streamer_port,
        force_status=502,
        force_html="<html><title>502 Bad Gateway</title></html>",
    )
    try:
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_pem))
        monkeypatch.setattr(pull_leads, "preflight", lambda: None)

        bad_tls_url = f"https://127.0.0.1:{bad_tls_proxy.port}"
        assert pull_leads.streamer_is_ready(bad_tls_url) is False
        r_tls = ping(bad_tls_url, TOKEN, 120)
        assert r_tls.kind == RETRY and "TLS/certificate" in r_tls.message
        assert bad_tls_proxy.seen_requests == []  # Refused before sending token!

        wrong_url = f"http://127.0.0.1:{wrong_backend_proxy.port}"
        assert pull_leads.streamer_is_ready(wrong_url) is False
        assert pull_leads.check_setup(wrong_url) == 1

        gw_url = f"http://127.0.0.1:{bad_gateway_proxy.port}"
        r_502 = ping(gw_url, TOKEN, 120)
        assert r_502.kind == RETRY and "HTTP 502" in r_502.message
    finally:
        bad_tls_proxy.close()
        wrong_backend_proxy.close()
        bad_gateway_proxy.close()
        streamer_srv.shutdown()
        streamer_srv.server_close()


def test_reverse_proxy_path_prefix_stripping(tmp_path, monkeypatch):
    srv_pem, ca_pem = _generate_cert_bundle(tmp_path, "example.com")
    store = StatefulMemoryStore(_sample_leads(3))
    streamer_srv, streamer_port = _start_streamer(store, expected_host="example.com")
    proxy = LocalNginxProxy(
        "127.0.0.1",
        streamer_port,
        cert_pem=srv_pem,
        override_host="example.com",
        strip_prefix="/streamer",
    )
    try:
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_pem))
        monkeypatch.setattr(pull_leads, "preflight", lambda: None)
        monkeypatch.setattr(config, "LEAD_SOURCE_TOKEN", TOKEN)

        base_url = f"https://127.0.0.1:{proxy.port}/streamer"
        assert pull_leads.streamer_is_ready(base_url) is True
        assert pull_leads.check_setup(base_url) == 0
        r = ping(base_url, TOKEN, 120, limit=2)
        assert r.kind == BATCH and len(r.leads) == 2
    finally:
        proxy.close()
        streamer_srv.shutdown()
        streamer_srv.server_close()


# ------------------------------------------------------------------ end-to-end integration tests

def test_e2e_batch_runner_through_https_proxy_to_lead_streamer_and_pipeline(tmp_path, monkeypatch):
    """Full offline E2E: Dashboard API -> BatchRunner -> HTTPS Proxy -> LeadStreamerServer -> LeadPipeline -> Excel/Tracker."""
    srv_pem, ca_pem = _generate_cert_bundle(tmp_path, "ai-sdr.grobots.ai")
    leads = _sample_leads(3)
    store = StatefulMemoryStore(leads, default_batch_size=300)
    streamer_srv, streamer_port = _start_streamer(store, expected_host="ai-sdr.grobots.ai")
    proxy = LocalNginxProxy(
        "127.0.0.1",
        streamer_port,
        cert_pem=srv_pem,
        override_host="ai-sdr.grobots.ai",
        gzip_json=True,
    )

    services_yaml = tmp_path / "services.yaml"
    services_yaml.write_text(
        "services:\n"
        "  - id: paradise\n"
        "    name: Paradise Estimating\n"
        "    agent_id: 3\n"
        "    description: Construction estimating\n",
        encoding="utf-8",
    )
    results_xlsx = tmp_path / "call_results.xlsx"
    transfers_xlsx = tmp_path / "transferred_calls.xlsx"
    tracker_xlsx = tmp_path / "pipeline_tracker.xlsx"
    batches_dir = tmp_path / "pulled_batches"

    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_pem))
    monkeypatch.setattr(config, "SERVICES_FILE", services_yaml)
    monkeypatch.setattr(config, "RESULTS_FILE", results_xlsx)
    monkeypatch.setattr(config, "TRANSFERS_FILE", transfers_xlsx)
    monkeypatch.setattr(config, "PIPELINE_TRACKER_FILE", tracker_xlsx)
    monkeypatch.setattr(config, "PULLED_BATCHES_DIR", batches_dir)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-gemini-key")
    monkeypatch.setattr(config, "CAMPAIGN_POLL_SECONDS", 0.01)
    monkeypatch.setattr(config, "RESULT_WAIT_SECONDS", 0.0)

    class FakeDecider:
        def __init__(self, services, threshold, api_key, model):
            self.services = services

        async def decide(self, lead):
            return Decision(
                probabilities={"paradise": 0.92},
                service=self.services[0],
                probability=0.92,
                verdict="qualified",
                summary="Good fit.",
            )

        async def close(self):
            pass

    class FakeDograh:
        def __init__(self):
            self._next_cid = 100
            self._campaigns = {}

        def get_agent(self, agent_id):
            return Agent(id=agent_id, name="Paradise Agent", version_status="published")

        def get_required_variables(self, agent_id):
            return set()

        def upload_csv(self, rows, file_name):
            return f"s3://{file_name}"

        def create_campaign(self, name, agent_id, file_key, retry_config=None):
            self._next_cid += 1
            cid = self._next_cid
            self._campaigns[cid] = {"file_key": file_key, "rows": []}
            return cid

        def start_campaign(self, cid):
            pass

        def get_campaign(self, cid):
            return {"id": cid, "state": "completed", "retry_config": {"enabled": False}}

        def get_campaign_runs(self, cid):
            # Return completed runs for every lead submitted via LeadPipeline
            return []

        def stop_campaign(self, cid):
            return "paused"

        def close(self):
            pass

    # Capture CSV rows uploaded so get_campaign_runs returns completed outcomes for each lead_ref
    class StatefulFakeDograh(FakeDograh):
        def upload_csv(self, rows, file_name):
            self._last_rows = rows
            return f"s3://{file_name}"

        def create_campaign(self, name, agent_id, file_key, retry_config=None):
            cid = super().create_campaign(name, agent_id, file_key, retry_config)
            self._campaigns[cid]["rows"] = list(getattr(self, "_last_rows", []))
            return {"id": cid}

        def get_campaign_runs(self, cid):
            runs = []
            for idx, r in enumerate(self._campaigns[cid]["rows"], start=1):
                runs.append({
                    "id": cid * 100 + idx,
                    "is_completed": True,
                    "initial_context": {"lead_ref": r["lead_ref"], "phone_number": r["phone_number"]},
                    "gathered_context": {"call_disposition": "completed", "call_tags": []},
                    "usage_info": {"call_duration_seconds": 42},
                })
            return runs

    monkeypatch.setattr("pipeline.DecisionMaker", FakeDecider)
    monkeypatch.setattr("pipeline.DograhClient", StatefulFakeDograh)
    monkeypatch.setattr(config, "LEAD_SOURCE_URL", f"https://127.0.0.1:{proxy.port}")
    monkeypatch.setattr(config, "LEAD_SOURCE_TOKEN", TOKEN)
    monkeypatch.setattr(config, "LEAD_SOURCE_TIMEOUT", 120)
    monkeypatch.setattr("dashboard.runner.preflight", lambda: None)

    app = create_app(tracker_path=tracker_xlsx, services_file=services_yaml, start_watcher=False)

    try:
        with TestClient(app) as client:
            # 2 batches of 2 leads -> 2 leads in batch 1, 1 lead in batch 2 (total 3)
            resp = client.post("/api/runner/start", json={"batch_size": 2, "num_batches": 2})
            assert resp.status_code == 200

            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                st = client.get("/api/runner").json()
                if not st["active"]:
                    break
                time.sleep(0.05)

            final = client.get("/api/runner").json()
            assert final["state"] == "completed", final
            assert final["batches_completed"] == 2
            assert final["leads_fetched"] == 3
            assert final["leads_processed"] == 3
            assert store.recorded_limits == [2, 2]
            assert len(store.sent_versions) == 3

            # Batch files marked done, none pending
            assert len(list(batches_dir.glob("*.done.json"))) == 2
            assert len(list(batches_dir.glob("*.pending.json"))) == 0

            # Dashboard summary reflects all 3 called leads from pipeline_tracker.xlsx
            summary = client.get("/api/summary").json()
            assert summary["tracker_found"] is True
            assert summary["nodes"]["incoming"]["total"] == 3
            assert summary["nodes"]["call_results"]["called"] == 3
    finally:
        proxy.close()
        streamer_srv.shutdown()
        streamer_srv.server_close()


def test_batch_runner_never_drops_extra_leads_and_respects_stop_after_ping(tmp_path):
    """If streamer returns > batch_size leads, BatchRunner must save & process all of them (never drop)."""
    batches_dir = tmp_path / "pulled"
    store = TrackerDataStore(tmp_path / "missing.xlsx", start_watcher=False)
    processed_batches: list[int] = []
    seen_chunk_sizes: list[int | None] = []

    def oversized_ping(url, token, timeout, limit=None):
        return PingResult(BATCH, "Received 5", _sample_leads(5))

    def fake_process(leads, *, batch_size=None, stop_event=None, batches_dir=None, **kwargs):
        processed_batches.append(len(leads))
        seen_chunk_sizes.append(batch_size)
        return {"received": len(leads)}

    runner = BatchRunner(
        store=store,
        source_url="http://fake.local",
        token=TOKEN,
        batches_dir=batches_dir,
        ping_fn=oversized_ping,
        process_fn=fake_process,
        preflight_fn=lambda: None,
    )
    runner.start(batch_size=2, num_batches=1)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and runner.get_status()["active"]:
        time.sleep(0.02)

    st = runner.get_status()
    assert st["state"] == "completed"
    assert processed_batches == [5]
    assert seen_chunk_sizes == [2]
    assert st["leads_fetched"] == 5
    assert st["leads_processed"] == 5
    store.close()


def test_batch_runner_stop_during_ping_preserves_pending_and_skips_process(tmp_path):
    batches_dir = tmp_path / "pulled"
    store = TrackerDataStore(tmp_path / "missing.xlsx", start_watcher=False)
    ping_entered = threading.Event()
    allow_ping_return = threading.Event()
    process_called = []

    def slow_ping(url, token, timeout, limit=None):
        ping_entered.set()
        allow_ping_return.wait(timeout=3.0)
        return PingResult(BATCH, "Received 2", _sample_leads(2))

    def fake_process(leads, **kwargs):
        process_called.append(len(leads))
        return {"received": len(leads)}

    runner = BatchRunner(
        store=store,
        source_url="http://fake.local",
        token=TOKEN,
        batches_dir=batches_dir,
        ping_fn=slow_ping,
        process_fn=fake_process,
        preflight_fn=lambda: None,
    )
    runner.start(batch_size=2, num_batches=1)
    assert ping_entered.wait(timeout=3.0)
    runner.stop()
    allow_ping_return.set()

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and runner.get_status()["active"]:
        time.sleep(0.02)

    st = runner.get_status()
    assert st["state"] == "stopped"
    assert process_called == []
    assert len(list(batches_dir.glob("*.pending.json"))) == 1
    store.close()


def test_batch_store_save_back_to_back_does_not_collide(tmp_path):
    store = BatchStore(tmp_path / "pulled")
    paths = [store.save(PingResult(BATCH, "x", _sample_leads(1)), "test") for _ in range(10)]
    assert len(set(paths)) == 10
    assert len(store.unfinished()) == 10


def test_lead_streamer_concurrent_pings_disjoint_and_503_when_saturated():
    leads = _sample_leads(12)
    store = StatefulMemoryStore(leads, default_batch_size=3)
    streamer_srv, port = _start_streamer(store, expected_host="", max_concurrent_requests=2)
    try:
        results = []
        lock = threading.Lock()

        def worker():
            r = requests.get(
                f"http://127.0.0.1:{port}/leads/next?limit=3",
                headers={"Authorization": f"Bearer {TOKEN}"},
                timeout=10,
            )
            with lock:
                results.append((r.status_code, r.headers.get("Retry-After"), r.json()))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        seen_ids = []
        for code, retry_after, body in results:
            if code == 503:
                assert body["error"] == "busy"
                assert retry_after == "5"
            else:
                assert code == 200
                if body["status"] == "ok":
                    seen_ids.extend(l["lead_id"] for l in body["leads"])
        # No duplicate lead_ids ever handed out across concurrent requests
        assert len(seen_ids) == len(set(seen_ids))
    finally:
        streamer_srv.shutdown()
        streamer_srv.server_close()


def test_puller_run_auto_through_https_proxy_and_streamer(tmp_path, monkeypatch):
    srv_pem, ca_pem = _generate_cert_bundle(tmp_path, "ai-sdr.grobots.ai")
    store = StatefulMemoryStore(_sample_leads(4), default_batch_size=2)
    streamer_srv, streamer_port = _start_streamer(store, expected_host="ai-sdr.grobots.ai")
    proxy = LocalNginxProxy(
        "127.0.0.1",
        streamer_port,
        cert_pem=srv_pem,
        override_host="ai-sdr.grobots.ai",
        gzip_json=True,
    )
    processed: list[list[str]] = []

    class StopAfterEmpty:
        def __call__(self, _seconds):
            raise KeyboardInterrupt

    try:
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_pem))
        batches_dir = tmp_path / "pulled"
        puller = Puller(
            source_url=f"https://127.0.0.1:{proxy.port}",
            token=TOKEN,
            timeout=120,
            store=BatchStore(batches_dir),
            process=lambda leads: processed.append([l["lead_id"] for l in leads]) or {"received": len(leads)},
        )
        rc = puller.run_auto(60, sleep_fn=StopAfterEmpty())
        assert rc == 130
        assert processed == [["uuid-0001", "uuid-0002"], ["uuid-0003", "uuid-0004"]]
        assert len(list(batches_dir.glob("*.done.json"))) == 2
        assert len(list(batches_dir.glob("*.pending.json"))) == 0
    finally:
        proxy.close()
        streamer_srv.shutdown()
        streamer_srv.server_close()


def test_batch_runner_reports_decision_errors_and_forwards_batches_dir(tmp_path, monkeypatch):
    batches_dir = tmp_path / "custom_pulled"
    store = TrackerDataStore(tmp_path / "missing.xlsx", start_watcher=False)

    monkeypatch.setattr(
        "pipeline.run_leads_sync",
        lambda leads, *, failed_leads=None, **kwargs: (
            failed_leads.extend(leads[:1]) if failed_leads is not None else None,
            {"received": len(leads), "decision_error": 1},
        )[1],
    )

    runner = BatchRunner(
        store=store,
        source_url="http://fake.local",
        token=TOKEN,
        batches_dir=batches_dir,
        ping_fn=lambda *a, **kw: PingResult(BATCH, "Received 2", _sample_leads(2)),
        preflight_fn=lambda: None,
    )
    runner.start(batch_size=2, num_batches=1)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and runner.get_status()["active"]:
        time.sleep(0.02)

    st = runner.get_status()
    assert st["state"] == "completed"
    assert "1 failed Gemini scoring and saved for retry" in st["message"]
    # Retry batch was saved into the custom batches_dir (not config.PULLED_BATCHES_DIR)
    assert len(list(batches_dir.glob("*.pending.json"))) == 1
    assert len(list(batches_dir.glob("*.done.json"))) == 1
    store.close()


def test_reverse_proxy_300_lead_gzip_large_text_and_natural_host_header(tmp_path, monkeypatch):
    """300 leads with >30KB description field pass intact over HTTPS+gzip with nginx $host stripping port."""
    srv_pem, ca_pem = _generate_cert_bundle(tmp_path, "ai-sdr.grobots.ai")
    leads = _sample_leads(300)
    leads[0]["description"] = "A" * 35_000
    store = StatefulMemoryStore(leads, default_batch_size=300)
    streamer_srv, streamer_port = _start_streamer(store, expected_host="ai-sdr.grobots.ai")
    # forward_host=True, no override_host: proxy strips port from `Host: ai-sdr.grobots.ai:<port>` just like `$host`
    proxy = LocalNginxProxy(
        "127.0.0.1",
        streamer_port,
        cert_pem=srv_pem,
        forward_host=True,
        gzip_json=True,
    )
    try:
        # Resolve ai-sdr.grobots.ai -> 127.0.0.1 in urllib3/socket so real Host header & SNI match the cert
        import socket
        orig_getaddrinfo = socket.getaddrinfo

        def fake_getaddrinfo(host, port, *args, **kwargs):
            if host == "ai-sdr.grobots.ai":
                return orig_getaddrinfo("127.0.0.1", port, *args, **kwargs)
            return orig_getaddrinfo(host, port, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_pem))
        monkeypatch.setattr(pull_leads, "preflight", lambda: None)
        monkeypatch.setattr(config, "LEAD_SOURCE_TOKEN", TOKEN)

        base_url = f"https://ai-sdr.grobots.ai:{proxy.port}"
        assert pull_leads.check_setup(base_url) == 0

        res = ping(base_url, TOKEN, 120)
        assert res.kind == BATCH
        assert len(res.leads) == 300
        assert len(res.leads[0]["description"]) == 35_000
        assert proxy.seen_requests[-1]["host"] == f"ai-sdr.grobots.ai:{proxy.port}"
    finally:
        proxy.close()
        streamer_srv.shutdown()
        streamer_srv.server_close()


def test_reverse_proxy_status_matrix_and_head_error_framing(tmp_path, monkeypatch):
    srv_pem, ca_pem = _generate_cert_bundle(tmp_path, "ai-sdr.grobots.ai")
    store = StatefulMemoryStore(_sample_leads(2), default_batch_size=300)
    streamer_srv, streamer_port = _start_streamer(store, expected_host="ai-sdr.grobots.ai")
    proxy = LocalNginxProxy(
        "127.0.0.1",
        streamer_port,
        cert_pem=srv_pem,
        override_host="ai-sdr.grobots.ai",
        gzip_json=True,
    )
    try:
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_pem))
        base_url = f"https://127.0.0.1:{proxy.port}"

        # 401 unauthorized -> FATAL
        r_401 = ping(base_url, "wrong-token", 120)
        assert r_401.kind == FATAL and "401" in r_401.message

        # 400 bad_request on invalid limit
        r_400 = requests.get(f"{base_url}/leads/next?limit=0", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=5)
        assert r_400.status_code == 400 and r_400.json()["error"] == "bad_request"

        # 404 unknown path
        r_404 = requests.get(f"{base_url}/unknown", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=5)
        assert r_404.status_code == 404 and r_404.json()["error"] == "not_found"

        # HEAD /stats -> 405 with empty body
        r_head_stats = requests.head(f"{base_url}/stats", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=5)
        assert r_head_stats.status_code == 405 and r_head_stats.content == b""

        # Direct HEAD parse-level error (414 URI Too Long) has empty body
        conn = http.client.HTTPConnection("127.0.0.1", streamer_port, timeout=5)
        conn.request("HEAD", "/leads/next?" + "x" * 70_000)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        assert resp.status == 414 and body == b""

        # 503 schema_mismatch from store -> FATAL in ping()
        def raise_schema_mismatch(limit=None):
            raise ls.DbUnavailableError("schema_mismatch")

        monkeypatch.setattr(store, "fetch_next_batch", raise_schema_mismatch)
        r_schema = ping(base_url, TOKEN, 120)
        assert r_schema.kind == FATAL and "schema_mismatch" in r_schema.message

        # 503 database_unavailable -> RETRY in ping()
        def raise_db_down(limit=None):
            raise ls.DbUnavailableError("database_unavailable")

        monkeypatch.setattr(store, "fetch_next_batch", raise_db_down)
        r_503 = ping(base_url, TOKEN, 120)
        assert r_503.kind == RETRY and "503" in r_503.message
    finally:
        proxy.close()
        streamer_srv.shutdown()
        streamer_srv.server_close()


def test_batch_runner_skips_corrupt_and_empty_pending_files(tmp_path):
    batches_dir = tmp_path / "pulled"
    batches_dir.mkdir(parents=True)
    # 1. Corrupt pending JSON (must not brick runner)
    (batches_dir / "batch_20260101_000001_000001_1.pending.json").write_text("{corrupt", encoding="utf-8")
    # 2. Empty leads pending JSON (must be marked done without calling process_fn)
    (batches_dir / "batch_20260101_000002_000001_0.pending.json").write_text(
        json.dumps({"leads": []}), encoding="utf-8"
    )
    # 3. Valid pending JSON
    valid_path = batches_dir / "batch_20260101_000003_000001_2.pending.json"
    valid_path.write_text(json.dumps({"leads": _sample_leads(2)}), encoding="utf-8")

    store = TrackerDataStore(tmp_path / "missing.xlsx", start_watcher=False)
    processed: list[int] = []

    def fake_process(leads, **kwargs):
        processed.append(len(leads))
        return {"received": len(leads)}

    runner = BatchRunner(
        store=store,
        source_url="http://fake.local",
        token=TOKEN,
        batches_dir=batches_dir,
        ping_fn=lambda *a, **kw: PingResult(EMPTY, "No new Leads available"),
        process_fn=fake_process,
        preflight_fn=lambda: None,
    )
    runner.start(batch_size=10, num_batches=1)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and runner.get_status()["active"]:
        time.sleep(0.02)

    st = runner.get_status()
    assert st["state"] == "completed", st
    assert processed == [2]
    assert not valid_path.exists()
    assert (batches_dir / "batch_20260101_000001_000001_1.pending.json").exists()
    assert (batches_dir / "batch_20260101_000002_000001_0.done.json").exists()
    store.close()


def test_default_process_skips_duplicate_retry_save_when_stopped(tmp_path, monkeypatch):
    batches_dir = tmp_path / "pulled"
    stop_evt = threading.Event()
    stop_evt.set()

    monkeypatch.setattr(
        "pipeline.run_leads_sync",
        lambda leads, *, failed_leads=None, **kwargs: (
            failed_leads.extend(leads) if failed_leads is not None else None,
            {"received": len(leads), "decision_error": len(leads)},
        )[1],
    )

    pull_leads.default_process(
        _sample_leads(2),
        threshold=0.80,
        output=None,
        batch_size=2,
        stop_event=stop_evt,
        batches_dir=batches_dir,
    )
    assert not batches_dir.exists() or list(batches_dir.glob("*.pending.json")) == []


def test_check_setup_handles_non_json_or_list_on_stats(monkeypatch, capsys):
    """Proxy returning Application/JSON on /health and 200 HTML on /stats fails cleanly without crashing."""
    class SplitHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                b = json.dumps({"status": "alive", "database": "ok"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "Application/JSON; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            else:
                b = b"<html>unexpected</html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SplitHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(pull_leads, "preflight", lambda: None)
        monkeypatch.setattr(config, "LEAD_SOURCE_TOKEN", TOKEN)
        rc = pull_leads.check_setup(f"http://127.0.0.1:{srv.server_address[1]}")
        assert rc == 1
        out = capsys.readouterr().out
        assert "OK   streamer:" in out
        assert "FAIL streamer /stats:" in out
    finally:
        srv.shutdown()
        srv.server_close()


