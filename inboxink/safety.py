"""Guards for handling untrusted newsletter HTML on a machine inside a home or office network.

safe_fetch: http(s) only, public addresses only, byte cap, total deadline. A hostname is resolved
exactly once per hop, every answer is checked, and the connection goes to the address that was
checked (not to a second DNS lookup, which a hostile DNS server could answer differently: DNS
rebinding). TLS still validates the certificate against the original hostname. Redirects are
followed by hand, so each hop is resolved, checked and pinned again.
"""
import http.client, ipaddress, re, socket, ssl, time, urllib.error
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

UA = {"User-Agent": "Mozilla/5.0"}
REDIRECTS = (301, 302, 303, 307, 308)
# IPv6 ranges that carry an IPv4 address inside them: NAT64 and 6to4 gateways would deliver the request
# to that embedded address (possibly a private one) and Teredo tunnels hide it. Refused outright.
EMBEDDED_V4 = tuple(ipaddress.ip_network(n) for n in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32"))


def _ssl_context():
    return ssl.create_default_context()


def _resolve_public(host, port):
    """Resolve `host` once. Returns its addresses (in DNS order) only if every one is public;
    raises ValueError otherwise."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise ValueError(f"cannot resolve {host}: {e}") from None
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped  # ::ffff:10.0.0.1 is the private address, however it is written
        if not ip.is_global or ip.is_multicast or (ip.version == 6 and any(ip in n for n in EMBEDDED_V4)):
            raise ValueError(f"refused non-public address for {host}")
        if str(ip) not in ips:
            ips.append(str(ip))
    if not ips:
        raise ValueError(f"no address for {host}")
    return ips


def _split(url):
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError(f"refused non-http(s) URL: {url[:80]}")
    return u, u.hostname, u.port or (443 if u.scheme == "https" else 80)


def public_http(url):
    """True only for http(s) URLs whose host resolves exclusively to public addresses."""
    try:
        _, host, port = _split(url)
        _resolve_public(host, port)
    except ValueError:
        return False
    return True


def _connect_pinned(ips, port, timeout):
    last = OSError("no address to connect to")
    for ip in ips:  # all already validated; a later one is only a fallback if this one will not connect
        try:
            return socket.create_connection((ip, port), timeout)
        except OSError as e:
            last = e
    raise last


class _PinnedHTTP(http.client.HTTPConnection):
    """Connects to the pre-validated addresses; the Host header still carries the original name."""

    def __init__(self, host, port, ips, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ips = ips

    def connect(self):
        self.sock = _connect_pinned(self._ips, self.port, self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    """As _PinnedHTTP, with TLS: SNI and certificate validation use the original hostname."""

    def __init__(self, host, port, ips, timeout):
        super().__init__(host, port, timeout=timeout, context=_ssl_context())
        self._ips = ips

    def connect(self):
        sock = _connect_pinned(self._ips, self.port, self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def safe_fetch(url, max_bytes=8_000_000, deadline_s=20, max_hops=4, follow=True):
    """Returns (bytes, final_url). With follow=False returns (b"", Location) for a redirect."""
    end = time.monotonic() + deadline_s
    for _ in range(max_hops + 1):
        u, host, port = _split(url)
        ips = _resolve_public(host, port)
        conn = (_PinnedHTTPS if u.scheme == "https" else _PinnedHTTP)(host, port, ips, 10)
        try:
            target = (u.path or "/") + (f"?{u.query}" if u.query else "")
            conn.request("GET", target, headers={**UA, "Accept-Encoding": "identity", "Connection": "close"})
            r = conn.getresponse()
            if r.status in REDIRECTS and r.getheader("Location"):
                nxt = urljoin(url, r.getheader("Location"))
                if not follow:
                    return b"", nxt
                url = nxt
                continue
            if not 200 <= r.status < 300:
                raise urllib.error.HTTPError(url, r.status, r.reason, r.msg, None)
            buf = bytearray()
            while len(buf) <= max_bytes:
                if time.monotonic() > end:
                    raise TimeoutError("fetch deadline exceeded")
                chunk = r.read(65536)
                if not chunk:
                    return bytes(buf), url
                buf += chunk
            raise ValueError("response over byte cap")
        finally:
            conn.close()
    raise ValueError("too many redirects")


# --- link hygiene -------------------------------------------------------------------------

# Param names that identify the subscriber or the send (ESP and marketing-automation conventions).
TOKEN_PARAMS = re.compile(
    r"^(utm_.*|mc_[ce]id|_hsenc|_hsmi|mkt_tok|_bhlid|vero_.*|ck_subscriber_id|"
    r"r|ref|token|key|uuid|uid|user(_?id)?|sub(scriber)?(_?id)?|email|e|j|sig|signature|hash|"
    r"auth|session|isFreemail|triedRedirect|publication_id|post_id)$", re.I)
OPAQUE = re.compile(r"^[A-Za-z0-9_\-.=%]{24,}$")  # long random-looking value: treat as a token
ACCOUNT_PATH = re.compile(r"/(log-?in|sign-?in|account|magic|auth|unsubscribe|preferences|"
                          r"disable_email|action|subscribe|signup|manage)\b", re.I)
TRACKER_HOST = re.compile(r"(^|\.)(click|clicks|links?|email|e|mail|t|track|trk|r)\.|list-manage\.com|"
                          r"convertkit-mail|kit-mail|mailchi\.mp|sendgrid\.net|hubspotlinks|mjt\.lu|"
                          r"beehiiv\.com|substack\.com/redirect|cmail\d+\.com|"
                          r"doubleclick\.net|googleadservices|adsrvr\.org|/trackclk/", re.I)


def clean_query(url, own_addresses=()):
    u = urlsplit(url)
    keep = []
    for k, v in parse_qsl(u.query, keep_blank_values=True):
        if TOKEN_PARAMS.match(k) or "@" in v or OPAQUE.match(v) or any(a in v.lower() for a in own_addresses):
            continue
        keep.append((k, v))
    return urlunsplit(u._replace(query=urlencode(keep), fragment=u.fragment if len(u.fragment) < 40 else ""))


def is_tracker(url):
    u = urlsplit(url)
    return bool(TRACKER_HOST.search((u.hostname or "") + u.path[:30]))


def is_account_link(url):
    return bool(ACCOUNT_PATH.search(urlsplit(url).path))
