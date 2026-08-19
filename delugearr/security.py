"""Trusted-network auth bypass helpers for the UI.

The REST API always requires the API key; this module only powers the optional
bypass for the NiceGUI pages. ``X-Forwarded-For`` is only honoured when the
direct peer is itself in the configured trusted-proxy list, so remote clients
cannot spoof a trusted address.
"""

import ipaddress

# Localhost is trusted by default, even when the stored trusted_networks /
# trusted_proxies list was accidentally written without it.
ALWAYS_TRUSTED = ("127.0.0.1/32", "::1/128")


def _networks(cidrs):
    nets = []
    for item in cidrs or []:
        try:
            nets.append(ipaddress.ip_network(str(item), strict=False))
        except ValueError:
            continue
    return nets


def _contains(nets, ip):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in nets)


def effective_client_ip(request, trusted_proxies):
    """Return the effective client IP, honouring XFF only from trusted proxies."""
    peer = request.client.host if request.client else ""
    proxies = _networks(trusted_proxies) + _networks(ALWAYS_TRUSTED)
    if not peer or not _contains(proxies, peer):
        return peer
    xff = request.headers.get("x-forwarded-for")
    if not xff:
        return peer
    for raw in reversed([i.strip() for i in xff.split(",") if i.strip()]):
        if not _contains(proxies, raw):
            return raw
    return peer


def should_bypass_auth(client_ip, settings):
    """True when bypass is enabled and the client is in a trusted network."""
    if not settings.get("auth_bypass_enabled"):
        return False
    network_list = _networks(settings.get("trusted_networks")) + _networks(ALWAYS_TRUSTED)
    return _contains(network_list, client_ip)
