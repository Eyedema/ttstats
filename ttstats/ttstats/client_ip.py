"""The client address the rate limits key on in prod (`RATELIMIT_IP_META_KEY`).

Prod runs behind Pangolin's Traefik, which connects to gunicorn over the
docker network, so REMOTE_ADDR is the proxy's container address for every
request: gunicorn never rewrites it from X-Forwarded-For
(`--forwarded-allow-ips` only governs the scheme headers). Keyed on
REMOTE_ADDR, each `key="ip"` limit was one bucket for the whole site.

Traefik appends the address it accepted the connection from to
X-Forwarded-For, so the **right-most** entry is the one hop we can trust;
entries to its left are whatever the client sent. Assumption: exactly one
proxy of ours (Traefik) sits in front of gunicorn. Add a second (a CDN) and
this must skip that hop too.
"""
import ipaddress


def client_ip(request):
    """X-Forwarded-For's last hop, or REMOTE_ADDR without a usable one."""
    hops = [
        hop.strip()
        for hop in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")
        if hop.strip()
    ]
    if hops:
        try:
            return str(ipaddress.ip_address(hops[-1]))
        except ValueError:
            pass
    return request.META["REMOTE_ADDR"]
