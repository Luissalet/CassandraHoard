"""The real HTTP, TLS and DNS probes against local servers (no internet)."""

import datetime
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from cassandra_hoard import sites_net as N


class Handler(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        Handler.seen.append({"path": self.path, "ua": self.headers.get("User-Agent", ""), "method": "GET"})
        if self.path == "/redirect":
            self.send_response(301)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/boom":
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/slow":
            time.sleep(1.5)
        body = (b"x" * 2_000_000) if self.path == "/big" else b"<html><body>Hello keyword world</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass


@pytest.fixture
def server():
    Handler.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_http_check_reads_the_home_page_once_with_a_clear_user_agent(server):
    out = N.http_check(server + "/")
    assert out["status"] == 200 and out["error"] is None and out["redirect"] is None and "keyword" in out["body"]
    assert out["latency_ms"] is not None and out["latency_ms"] >= 0
    assert len(Handler.seen) == 1 and "Cassandra's Hoard" in Handler.seen[0]["ua"] and Handler.seen[0]["method"] == "GET"


def test_redirects_are_reported_and_only_followed_for_a_keyword(server):
    plain = N.http_check(server + "/redirect")
    assert plain["status"] == 301 and plain["redirect"] == server + "/" and plain["body"] is None and len(Handler.seen) == 1
    followed = N.http_check(server + "/redirect", follow=True)
    assert followed["status"] == 200 and followed["redirect"] == server + "/" and "keyword" in followed["body"] and len(Handler.seen) == 3


def test_http_errors_are_statuses_not_exceptions(server):
    assert N.http_check(server + "/boom")["status"] == 503


def test_the_body_read_is_capped(server):
    out = N.http_check(server + "/big")
    assert out["status"] == 200 and len(out["body"]) <= N.MAX_BODY


def test_timeout_and_refused_are_classified(server):
    slow = N.http_check(server + "/slow", timeout=0.3)
    assert slow["status"] is None and slow["error"]["kind"] == "timeout" and "Timeout" in slow["error"]["detail"]
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    refused = N.http_check(f"http://127.0.0.1:{port}/", timeout=8)  # Windows takes ~2 s to refuse a loopback port
    assert refused["error"]["kind"] == "refused" and "refused" in refused["error"]["detail"]


def test_error_classification_walks_the_cause_chain():
    request = httpx.Request("GET", "https://example.com/")
    dns = httpx.ConnectError("x", request=request)
    dns.__cause__ = socket.gaierror(11001, "getaddrinfo failed")
    assert N.classify_error(dns, "example.com", 15)["kind"] == "dns"
    tls = httpx.ConnectError("x", request=request)
    inner = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    inner.verify_message = "certificate has expired"
    tls.__cause__ = inner
    out = N.classify_error(tls, "example.com", 15)
    assert out == {"kind": "tls", "detail": "TLS error: certificate has expired"}
    assert N.classify_error(httpx.ReadTimeout("t", request=request), "h", 7)["detail"] == "Timeout: no answer within 7 s"
    reset = httpx.ReadError("x", request=request)
    reset.__cause__ = ConnectionResetError()
    assert N.classify_error(reset, "h", 7)["kind"] == "reset"
    assert N.classify_error(ValueError("odd"), "h", 7)["kind"] == "other"


def test_dns_check_with_literals_and_localhost():
    assert N.dns_check("203.0.113.5") == {"ok": True, "addresses": ["203.0.113.5"], "error": None}
    out = N.dns_check("localhost", 443)
    assert out["ok"] and "127.0.0.1" in out["addresses"] + ["127.0.0.1"] and out["addresses"] == sorted(out["addresses"])


# -- TLS against a local server with generated certificates --------------------------

crypto = pytest.importorskip("cryptography")


def _make(cn, key, issuer_name, issuer_key, *, sans=(), start=-1, end=90, ca=False):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    subject = issuer_name if ca else x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer_name).public_key(key.public_key())
               .serial_number(x509.random_serial_number()).not_valid_before(now + datetime.timedelta(days=start))
               .not_valid_after(now + datetime.timedelta(days=end)).add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if sans:
        builder = builder.add_extension(x509.SubjectAlternativeName(list(sans)), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


class TlsServer:
    def __init__(self, tmp_path, *, sans, start=-1, end=90):
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        ca_key, leaf_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Test Org"), x509.NameAttribute(NameOID.COMMON_NAME, "Test Root")])
        ca = _make("Test Root", ca_key, ca_name, ca_key, ca=True)
        leaf = _make("localhost", leaf_key, ca_name, ca_key, sans=sans, start=start, end=end)
        self.ca_file = tmp_path / "ca.pem"
        self.ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
        chain = tmp_path / "chain.pem"
        chain.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
        key = tmp_path / "key.pem"
        key.write_bytes(leaf_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(str(chain), str(key))
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            try:
                self.context.wrap_socket(conn, server_side=True).close()
            except Exception:  # noqa: BLE001
                conn.close()

    def close(self):
        self.stop.set()
        self.sock.close()


def trust(monkeypatch, ca_file):
    real = ssl.create_default_context

    def context(*args, **kwargs):
        ctx = real(cafile=str(ca_file))
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        return ctx

    monkeypatch.setattr(ssl, "create_default_context", context)


def test_tls_check_reads_a_valid_certificate(tmp_path, monkeypatch):
    from cryptography import x509

    server = TlsServer(tmp_path, sans=[x509.DNSName("localhost")])
    try:
        trust(monkeypatch, server.ca_file)
        out = N.tls_check("localhost", server.port)
    finally:
        server.close()
    assert out["ok"] is True and out["chain_valid"] is True and out["hostname_match"] is True and out["error"] is None
    assert out["days_left"] in (88, 89) and out["issuer"] == "Test Org (Test Root)" and out["fingerprint"] and "localhost" in out["san"]


def test_tls_check_reports_an_expired_certificate_with_its_date(tmp_path, monkeypatch):
    from cryptography import x509

    server = TlsServer(tmp_path, sans=[x509.DNSName("localhost")], start=-30, end=-3)
    try:
        trust(monkeypatch, server.ca_file)
        out = N.tls_check("localhost", server.port)
    finally:
        server.close()
    assert out["ok"] is False and out["error"] == "TLS error: certificate has expired" and out["days_left"] in (-4, -3)
    assert out["issuer"] == "Test Org (Test Root)" and out["not_after"]


def test_tls_check_reports_a_host_name_mismatch_and_an_untrusted_chain(tmp_path, monkeypatch):
    from cryptography import x509

    server = TlsServer(tmp_path, sans=[x509.DNSName("other.example")])
    try:
        with monkeypatch.context() as scoped:
            trust(scoped, server.ca_file)
            mismatch = N.tls_check("localhost", server.port)
    finally:
        server.close()
    assert mismatch["ok"] is False and mismatch["hostname_match"] is False and "mismatch" in mismatch["error"].lower() and mismatch["days_left"] in (88, 89)
    (tmp_path / "second").mkdir()
    server = TlsServer(tmp_path / "second", sans=[x509.DNSName("localhost")])
    try:
        untrusted = N.tls_check("localhost", server.port)  # the test CA is not in the system store
    finally:
        server.close()
    assert untrusted["ok"] is False and untrusted["chain_valid"] is False and untrusted["error"].startswith("TLS error") and untrusted["days_left"] in (88, 89)


def test_tls_check_when_nothing_listens():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    out = N.tls_check("127.0.0.1", port, timeout=8)
    assert out["ok"] is False and out["days_left"] is None and out["error"]
