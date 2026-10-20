"""Manual release checks using the published executable's SHA256 digest.

Only release metadata is fetched. Transport instances are private to each check;
proxy selection never changes environment variables or the global socket API.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import json
from pathlib import Path
import re
import socket
import ssl
import sys
from urllib.parse import urlsplit


RELEASES_URL = 'https://github.com/Punizione/Loka/releases'
CHECK_TIMEOUT = 15
_API_HOST = 'api.github.com'
_API_PATH = '/repos/Punizione/Loka/releases/latest'
_MAX_METADATA = 1024 * 1024


class UpdateCheckError(Exception):
    """A stable UI error code, deliberately excluding remote/proxy error text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Proxy:
    scheme: str
    host: str
    port: int


@dataclass(frozen=True)
class UpdateCheckResult:
    update_available: bool
    latest_version: str = ''
    published_at: str = ''


def validate_proxy(url: str) -> Proxy | None:
    """Accept an explicit HTTP/SOCKS5 host and port, or empty for direct access."""
    url = url.strip()
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'socks5') or not parsed.hostname
                or not parsed.port or parsed.username is not None or parsed.password is not None
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                or any(c.isspace() or ord(c) < 32 for c in url)):
            raise ValueError('invalid proxy')
        return Proxy(parsed.scheme, parsed.hostname, parsed.port)
    except (ValueError, UnicodeError):
        raise UpdateCheckError('proxy') from None


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise OSError('Incomplete SOCKS5 response')
        chunks.extend(chunk)
    return bytes(chunks)


def _socks5_connect(address: tuple[str, int], proxy: Proxy, timeout: float) -> socket.socket:
    """RFC 1928 CONNECT with remote DNS (the local-proxy, no-auth method)."""
    sock = socket.create_connection((proxy.host, proxy.port), timeout=timeout)
    try:
        sock.sendall(b'\x05\x01\x00')
        if _read_exact(sock, 2) != b'\x05\x00':
            raise OSError('SOCKS5 authentication method rejected')
        host, port = address
        name = host.encode('idna')
        if not 0 < len(name) <= 255:
            raise OSError('Invalid SOCKS5 destination')
        sock.sendall(b'\x05\x01\x00\x03' + bytes([len(name)]) + name + port.to_bytes(2, 'big'))
        reply = _read_exact(sock, 4)
        if reply[:3] != b'\x05\x00\x00':
            raise OSError('SOCKS5 CONNECT rejected')
        if reply[3] == 1:
            size = 4
        elif reply[3] == 4:
            size = 16
        elif reply[3] == 3:
            size = _read_exact(sock, 1)[0]
        else:
            raise OSError('Invalid SOCKS5 address type')
        _read_exact(sock, size + 2)
        return sock
    except BaseException:
        sock.close()
        raise


def _https_connection(proxy: Proxy | None, timeout: float) -> http.client.HTTPSConnection:
    if proxy is not None and proxy.scheme == 'http':
        connection = http.client.HTTPSConnection(proxy.host, proxy.port, timeout=timeout)
        connection.set_tunnel(_API_HOST, 443)
    else:
        connection = http.client.HTTPSConnection(_API_HOST, timeout=timeout)
        if proxy is not None:
            connection._create_connection = lambda address, timeout, source_address=None: _socks5_connect(
                address, proxy, timeout)
    return connection


def _fetch_release(proxy: Proxy | None, timeout: float) -> dict:
    connection = _https_connection(proxy, timeout)
    try:
        connection.request('GET', _API_PATH, headers={
            'Host': _API_HOST,
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'Loka-update-check',
        })
        with connection.getresponse() as response:
            if response.status in (403, 429):
                raise UpdateCheckError('rate_limit')
            if response.status != 200:
                raise UpdateCheckError('server')
            raw = response.read(_MAX_METADATA + 1)
            if len(raw) > _MAX_METADATA:
                raise UpdateCheckError('metadata')
        metadata = json.loads(raw)
        if not isinstance(metadata, dict):
            raise UpdateCheckError('metadata')
        return metadata
    except (TimeoutError, socket.timeout):
        raise UpdateCheckError('timeout') from None
    except ssl.SSLError:
        raise UpdateCheckError('tls') from None
    except (OSError, http.client.HTTPException):
        raise UpdateCheckError('network') from None
    except (ValueError, UnicodeError):
        raise UpdateCheckError('metadata') from None
    finally:
        connection.close()


def check_update(executable: Path | None = None, proxy_url: str = '',
                 *, timeout: float = CHECK_TIMEOUT) -> UpdateCheckResult:
    """Any hash mismatch is an update, independent of version strings."""
    proxy = validate_proxy(proxy_url)
    if executable is None:
        if not getattr(sys, 'frozen', False):
            raise UpdateCheckError('source')
        executable = Path(sys.executable)
    try:
        with Path(executable).open('rb') as stream:
            local_digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    except OSError:
        raise UpdateCheckError('local_file') from None
    metadata = _fetch_release(proxy, timeout)
    assets = metadata.get('assets')
    if not isinstance(assets, list):
        raise UpdateCheckError('metadata')
    executables = [asset for asset in assets if isinstance(asset, dict)
                   and isinstance(asset.get('name'), str)
                   and re.fullmatch(r'Loka(?:-[^/\\]+)?\.exe', asset['name'], re.IGNORECASE)]
    if len(executables) != 1:
        raise UpdateCheckError('metadata')
    digest = executables[0].get('digest')
    if not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest):
        raise UpdateCheckError('metadata')
    version = metadata.get('tag_name')
    published_at = metadata.get('published_at')
    return UpdateCheckResult(
        local_digest != digest[7:].lower(),
        latest_version=version.strip() if isinstance(version, str) else '',
        published_at=published_at if isinstance(published_at, str) else '',
    )
