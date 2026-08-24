from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Optional
from urllib.parse import urlparse

import httpx

_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _addr_ok(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return False
    if addr.version == 4 and addr in _CGNAT:
        return False
    return True


def validate_base_url(url: str) -> Optional[str]:
    """Static checks on a user-supplied base URL. Returns an error or None."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return "base_url must use https"
    host = parsed.hostname
    if not host:
        return "base_url has no hostname"
    if parsed.username or parsed.password:
        return "base_url must not contain credentials"
    try:
        ipaddress.ip_address(host)
        return "base_url must use a hostname, not an IP address"
    except ValueError:
        pass
    if "." not in host or host.endswith("."):
        return "base_url hostname must be a public DNS name"
    return None


def resolve_public_ip(host: str) -> str:
    """Resolves host and returns one address; raises ValueError if any resolved
    address is private/loopback/link-local/CGNAT/reserved."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"could not resolve {host}: {exc}") from exc
    ips = {info[4][0] for info in infos}
    if not ips:
        raise ValueError(f"no addresses for {host}")
    for ip in ips:
        if not _addr_ok(ip):
            raise ValueError(f"{host} resolves to a non-public address")
    return sorted(ips)[0]


class PinnedPublicTransport(httpx.AsyncHTTPTransport):
    """Re-resolves and validates the destination on every request, then connects
    to the validated IP (keeping Host + SNI on the original name) so a DNS
    rebind between validation and connect cannot redirect the request into the
    internal network."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        error = validate_base_url(str(request.url))
        if error:
            raise httpx.RequestError(f"blocked base_url: {error}", request=request)
        ip = await asyncio.to_thread(resolve_public_ip, host)
        request.headers["Host"] = request.url.netloc.decode("ascii")
        request.extensions["sni_hostname"] = host
        request.url = request.url.copy_with(host=ip)
        return await super().handle_async_request(request)


def safe_async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=PinnedPublicTransport())
