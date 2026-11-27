"""Explicit, per-recorder proxy selection without changing process settings."""
import http.client

from ..gateway import upstream_connection


def connection(parsed, timeout, proxy_mode):
    if proxy_mode == 'environment':
        return upstream_connection(parsed, timeout)
    if proxy_mode != 'direct':
        raise ValueError('proxy_mode must be environment or direct')
    cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
    return cls(parsed.hostname, parsed.port, timeout=timeout), False, {}
