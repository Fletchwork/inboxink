"""safe_fetch: public addresses only, and the connection goes to the address that was checked."""
import io, socket, ssl, urllib.error

import pytest

from inboxink import safety

PUBLIC, PRIVATE = "93.184.216.34", "127.0.0.1"


def addrinfo(*ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]


class FakeSock:
    """Just enough socket for http.client: records what was sent, replays a canned response."""

    def __init__(self, response):
        self.sent = b""
        self._response = response

    def sendall(self, data):
        self.sent += data

    def makefile(self, *a, **k):
        return io.BytesIO(self._response)

    def settimeout(self, t):
        pass

    def close(self):
        pass


def ok(body=b"hello"):
    return b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body)


def redirect(to):
    return f"HTTP/1.1 302 Found\r\nLocation: {to}\r\nContent-Length: 0\r\n\r\n".encode()


@pytest.fixture
def net(monkeypatch):
    """Replace DNS and sockets. net.dns maps host -> list of answer lists (one per lookup); the last repeats."""
    class Net:
        dns, lookups, connects, socks, responses = {}, [], [], [], []

    n = Net()
    n.dns, n.lookups, n.connects, n.socks, n.responses = {}, [], [], [], []

    def getaddrinfo(host, port, *a, **k):
        n.lookups.append(host)
        answers = n.dns[host]
        seen = n.lookups.count(host)
        return addrinfo(*answers[min(seen, len(answers)) - 1])

    def create_connection(addr, timeout=None, *a, **k):
        n.connects.append(addr)
        sock = FakeSock(n.responses.pop(0))
        n.socks.append(sock)
        return sock

    monkeypatch.setattr(safety.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(safety.socket, "create_connection", create_connection)
    return n


def test_refuses_local_and_non_http():
    for url in ("file:///etc/hosts", "ftp://example.com/x", "http://127.0.0.1:8765/", "http://192.168.4.1/",
                "http://169.254.169.254/latest", "http://[::1]/"):
        assert not safety.public_http(url), url
        with pytest.raises(ValueError):
            safety.safe_fetch(url)


def test_private_and_mixed_answers_are_rejected(net):
    net.dns = {"internal.example": [["10.0.0.5"]], "mixed.example": [[PUBLIC, "192.168.1.9"]]}
    for host in net.dns:
        assert not safety.public_http(f"http://{host}/")
        with pytest.raises(ValueError, match="non-public"):
            safety.safe_fetch(f"http://{host}/")
    assert net.connects == []


def test_ipv6_ranges_that_embed_an_ipv4_address_are_rejected(net):
    # NAT64, 6to4 and Teredo can deliver to a private IPv4 address; IPv4-mapped is that address in disguise.
    net.dns = {"nat64.example": [["64:ff9b::a00:5"]], "nat64-local.example": [["64:ff9b:1::a00:5"]],
               "6to4.example": [["2002:c0a8:101::1"]], "teredo.example": [["2001:0:4136:e378:8000:63bf:3fff:fdd2"]],
               "mapped.example": [["::ffff:10.0.0.5"]]}
    for host in net.dns:
        assert not safety.public_http(f"http://{host}/"), host
        with pytest.raises(ValueError, match="non-public"):
            safety.safe_fetch(f"http://{host}/")
    assert net.connects == []


def test_connects_to_the_validated_ip_even_if_dns_changes_its_answer(net):
    # DNS rebinding: first answer public, every later answer loopback.
    net.dns = {"rebind.example": [[PUBLIC], [PRIVATE]]}
    net.responses = [ok(b"page")]
    body, final = safety.safe_fetch("http://rebind.example/path?q=1")
    assert body == b"page" and final == "http://rebind.example/path?q=1"
    assert net.connects == [(PUBLIC, 80)]  # never 127.0.0.1
    assert net.lookups == ["rebind.example"]  # resolved once
    sent = net.socks[0].sent
    assert sent.startswith(b"GET /path?q=1 HTTP/1.1\r\n") and b"Host: rebind.example\r\n" in sent


def test_each_redirect_hop_is_resolved_and_checked_again(net):
    net.dns = {"a.example": [[PUBLIC]], "b.example": [["10.1.2.3"]]}
    net.responses = [redirect("http://b.example/x")]
    with pytest.raises(ValueError, match="non-public"):
        safety.safe_fetch("http://a.example/")
    assert net.connects == [(PUBLIC, 80)]  # the private hop was never connected to


def test_redirect_hop_pins_its_own_ip(net):
    net.dns = {"a.example": [[PUBLIC]], "b.example": [["93.184.216.35"], [PRIVATE]]}
    net.responses = [redirect("http://b.example/done"), ok(b"end")]
    body, final = safety.safe_fetch("http://a.example/")
    assert body == b"end" and final == "http://b.example/done"
    assert net.connects == [(PUBLIC, 80), ("93.184.216.35", 80)]


def test_follow_false_returns_location_without_fetching_it(net):
    net.dns = {"a.example": [[PUBLIC]]}
    net.responses = [redirect("http://elsewhere.example/t")]
    assert safety.safe_fetch("http://a.example/", follow=False) == (b"", "http://elsewhere.example/t")


def test_https_uses_the_hostname_for_sni_and_the_ip_for_the_socket(net, monkeypatch):
    net.dns = {"secure.example": [[PUBLIC], [PRIVATE]]}
    net.responses = [ok(b"tls")]
    seen = {}

    class Ctx:  # HTTPSConnection reads these in __init__ on Python 3.11
        verify_mode = ssl.CERT_REQUIRED
        check_hostname = True

        def wrap_socket(self, sock, server_hostname=None):
            seen["hostname"] = server_hostname
            return sock

    monkeypatch.setattr(safety, "_ssl_context", lambda: Ctx())
    body, _ = safety.safe_fetch("https://secure.example/")
    assert body == b"tls"
    assert net.connects == [(PUBLIC, 443)]
    assert seen["hostname"] == "secure.example"  # certificate validation and SNI use the name
    assert b"Host: secure.example\r\n" in net.socks[0].sent


def test_falls_back_to_the_next_validated_ip_only(net, monkeypatch):
    net.dns = {"dual.example": [["2606:2800:220:1:248:1893:25c8:1946", PUBLIC]]}
    calls = []
    real_responses = [ok(b"v4")]

    def create_connection(addr, timeout=None, *a, **k):
        calls.append(addr)
        if ":" in addr[0]:
            raise OSError("no route")
        return FakeSock(real_responses.pop(0))

    monkeypatch.setattr(safety.socket, "create_connection", create_connection)
    monkeypatch.setattr(safety.socket, "getaddrinfo", lambda host, port, *a, **k: [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 0, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 0))])
    body, _ = safety.safe_fetch("http://dual.example/")
    assert body == b"v4" and [c[0] for c in calls] == ["2606:2800:220:1:248:1893:25c8:1946", PUBLIC]


def test_byte_cap_and_http_errors(net):
    net.dns = {"a.example": [[PUBLIC]]}
    net.responses = [ok(b"x" * 100)]
    with pytest.raises(ValueError, match="byte cap"):
        safety.safe_fetch("http://a.example/", max_bytes=10)
    net.responses = [b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"]
    with pytest.raises(urllib.error.HTTPError):
        safety.safe_fetch("http://a.example/")
