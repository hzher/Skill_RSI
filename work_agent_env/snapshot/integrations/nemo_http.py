"""Cookie-preserving diagnostic client of the real NeMo Gym HTTP surface."""
import http.cookiejar
import base64
import binascii
import json
import time
import urllib.error
import urllib.request

from integrations.resource_transport import MAX_RESPONSE_FRAME_BYTES


class NeMoHTTP:
    def __init__(self, base_url="http://127.0.0.1:18700", *, request_timeout=200):
        if base_url != "http://127.0.0.1:18700":
            raise ValueError("This local diagnostic client is restricted to its owned NeMo runtime")
        self.base_url = base_url
        if type(request_timeout) not in (int, float) or not 0 < request_timeout <= 200:
            raise ValueError("NeMo request timeout must be in (0, 200]")
        self.request_timeout = request_timeout
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.journal = []

    def _wait_for_cookie_clock(self):
        # NeMo uses TimestampSigner cookies. A WSL wall-clock correction can
        # place the latest signature slightly in the future; itsdangerous then
        # discards the session. Wait before sending, never replay a request.
        # The unsigned timestamp is only a bounded scheduling hint, not proof
        # of identity. The server still verifies the complete signature.
        timestamps = []
        for cookie in self.cookies:
            try:
                part = cookie.value.rsplit(".", 2)[1]
                if not 1 <= len(part) <= 12: continue
                raw = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
                if not 1 <= len(raw) <= 8: continue
                timestamps.append(int.from_bytes(raw, "big"))
            except (ValueError, IndexError, binascii.Error): continue
        if not timestamps: return 0.0
        timestamp, started = max(timestamps), time.monotonic()
        while timestamp > int(time.time()):
            if timestamp - time.time() > 5 or time.monotonic() - started >= 5:
                raise RuntimeError("Cookie timestamp remains ahead of the local clock; no request sent")
            time.sleep(0.02)
        return time.monotonic() - started

    def post(self, route, body, *, expected=200):
        waited = self._wait_for_cookie_clock()
        request = urllib.request.Request(self.base_url + route, json.dumps(body, allow_nan=False).encode(),
                                         {"Content-Type": "application/json"})
        try:
            response = self.opener.open(request, timeout=self.request_timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            status = response.status
            raw = response.read(MAX_RESPONSE_FRAME_BYTES + 1)
            if len(raw) > MAX_RESPONSE_FRAME_BYTES:
                raise RuntimeError("NeMo response exceeds its wire budget; outcome unknown, do not replay")
            data = json.loads(raw)
        self.journal.append({"route": route, "request": body, "status": status, "response": data})
        if waited >= 0.02: self.journal[-1]["pre_request_clock_wait_seconds"] = waited
        if status != expected:
            raise RuntimeError(f"NeMo {route}: HTTP {status}, expected {expected}: {str(data)[:500]}")
        return data
