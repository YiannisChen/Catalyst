from __future__ import annotations

import ipaddress

import pytest

from deploy.ops.generate_cloudflare_include import render


def _ranges() -> list[str]:
    return [
        *(f"192.0.{index}.0/24" for index in range(10)),
        *(f"2001:db8:{index}::/48" for index in range(5)),
    ]


def test_render_trusts_and_classifies_every_cloudflare_range() -> None:
    ranges = _ranges()

    result = render(ranges)

    for value in ranges:
        network = ipaddress.ip_network(value)
        assert f"set_real_ip_from {network};" in result
        assert f"    {network} 1;" in result
    assert "geo $realip_remote_addr $catalyst_cloudflare_peer {" in result
    assert "    default 0;" in result
    assert "allow " not in result
    assert "deny all;" not in result


def test_render_requires_minimum_ranges_for_both_families() -> None:
    ranges = _ranges()

    with pytest.raises(ValueError, match="v4 list"):
        render(ranges[1:])
    with pytest.raises(ValueError, match="v6 list"):
        render(ranges[:-1])
