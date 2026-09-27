"""
Phone access over the home Wi-Fi.

Off by default: the app only listens on 127.0.0.1. Turning it on adds a second
listener on the machine's private LAN address (same port). Every request that
arrives there must carry the secret token, first in the QR link (?token=...),
then as a cookie. Turning it off closes that listener and rotates the token,
so old links and cookies stop working.
"""

import ipaddress
import secrets
import socket
import threading
from http.server import ThreadingHTTPServer

COOKIE = "eg_phone"

_lock = threading.Lock()
_server = None
_token = secrets.token_urlsafe(24)


def is_private(ip):
    try:
        a = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return a.is_private and not a.is_loopback and not a.is_link_local


def lan_ip():
    """The private address this machine uses on the local network (no packet is sent)."""
    candidates = []
    for probe in ("8.8.8.8", "192.168.0.1", "10.0.0.1", "172.16.0.1"):   # default route first
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe, 80))           # UDP connect only picks the outgoing interface
            candidates.append(s.getsockname()[0])
        except OSError:
            pass
        finally:
            s.close()
    try:
        candidates += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    return next((ip for ip in candidates if is_private(ip)), None)


def token():
    return _token


def token_ok(value):
    return bool(value) and secrets.compare_digest(str(value), _token)


def status():
    with _lock:
        if not _server:
            return {"on": False}
        ip, port = _server.server_address[:2]
        return {"on": True, "ip": ip, "port": port, "url": f"http://{ip}:{port}/?token={_token}"}


def start(handler_cls, port):
    """Serve the app on the LAN address as well. Returns status()."""
    global _server
    with _lock:
        if not _server:
            ip = lan_ip()
            if not ip:
                raise ValueError("ما لقيتش شبكة محلية (Wi-Fi). تأكد بلي الـ PC متصل بالراوتر.")
            try:
                server = ThreadingHTTPServer((ip, port), handler_cls)
            except OSError as e:
                raise ValueError(f"ما قدرتش نفتح {ip}:{port} ({e.strerror or e}).")
            server.lan = True
            threading.Thread(target=server.serve_forever, daemon=True, name="phone-access").start()
            _server = server
    return status()


def stop():
    """Close LAN access and rotate the token (old QR links and cookies die)."""
    global _server, _token
    with _lock:
        server, _server = _server, None
        _token = secrets.token_urlsafe(24)
    if server:
        server.shutdown()
        server.server_close()
    return status()
