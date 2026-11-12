"""Which address the rate limits key on in prod (`ttstats.client_ip`).

gunicorn sets REMOTE_ADDR from the socket peer, which in prod is the
Pangolin/Traefik container, and never rewrites it from X-Forwarded-For
(`--forwarded-allow-ips` governs only the scheme headers). Traefik appends the
address it saw to X-Forwarded-For, so the right-most entry is the one hop we
can trust; anything to its left is whatever the client sent.
"""
import pytest
from django.test import Client, RequestFactory
from django.urls import reverse

from ttstats.client_ip import client_ip

from .conftest import PlayerFactory

PROXY = "172.18.0.5"


def _request(xff=None, remote=PROXY):
    extra = {"REMOTE_ADDR": remote}
    if xff is not None:
        extra["HTTP_X_FORWARDED_FOR"] = xff
    return RequestFactory().get("/", **extra)


class TestClientIp:
    def test_the_proxys_hop_is_the_client(self):
        assert client_ip(_request("203.0.113.7")) == "203.0.113.7"

    def test_a_spoofed_left_entry_is_ignored(self):
        assert client_ip(_request("1.2.3.4, 203.0.113.7")) == "203.0.113.7"

    def test_whitespace_and_empty_entries(self):
        assert client_ip(_request(" 1.2.3.4 ,203.0.113.7 , ")) == "203.0.113.7"

    def test_ipv6(self):
        assert client_ip(_request("2001:db8::1")) == "2001:db8::1"

    def test_no_header_falls_back_to_the_socket_peer(self):
        """A request that did not come through the proxy (a health check on
        the docker network)."""
        assert client_ip(_request()) == PROXY
        assert client_ip(_request("")) == PROXY

    def test_garbage_falls_back_to_the_socket_peer(self):
        """Never a 500 and never an arbitrary string as a cache key."""
        assert client_ip(_request("not-an-ip")) == PROXY
        assert client_ip(_request("1.2.3.4, unknown")) == PROXY


@pytest.mark.django_db
class TestRateLimitsKeyOnTheClient:
    def test_two_clients_behind_the_proxy_have_separate_login_buckets(self, settings):
        settings.RATELIMIT_IP_META_KEY = "ttstats.client_ip.client_ip"
        player = PlayerFactory(with_user=True)
        bad = {"username": player.user.username, "password": "wrong"}

        def attempts(ip, n):
            c = Client(REMOTE_ADDR=PROXY, HTTP_X_FORWARDED_FOR=ip)
            return [c.post(reverse("login"), bad).status_code for _ in range(n)]

        assert 403 in attempts("203.0.113.7", 6)  # 5/15m, per client
        assert 403 not in attempts("198.51.100.9", 5)  # someone else is fine
