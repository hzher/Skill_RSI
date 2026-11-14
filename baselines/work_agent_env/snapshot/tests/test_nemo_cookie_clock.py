import base64
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from integrations.nemo_http import NeMoHTTP


class CookieClockTests(unittest.TestCase):
    def client(self, timestamp):
        client = NeMoHTTP()
        token = base64.urlsafe_b64encode(timestamp.to_bytes(4, "big")).decode().rstrip("=")
        client.cookies = [SimpleNamespace(value="payload." + token + ".signature")]
        client.opener = Mock()
        return client

    def test_small_backward_clock_step_waits_without_sending_or_replaying(self):
        client = self.client(101)
        with patch("integrations.nemo_http.time.time", side_effect=[100.5, 100.5, 101.0]), patch(
            "integrations.nemo_http.time.monotonic", side_effect=[10, 10.1, 10.2]
        ), patch("integrations.nemo_http.time.sleep") as sleep:
            self.assertAlmostEqual(client._wait_for_cookie_clock(), .2)
            sleep.assert_called_once_with(.02)
        client.opener.open.assert_not_called()

    def test_large_future_hint_refuses_before_transport(self):
        client = self.client(110)
        with patch("integrations.nemo_http.time.time", return_value=100), patch(
            "integrations.nemo_http.time.monotonic", return_value=10
        ):
            with self.assertRaises(RuntimeError): client.post("/verify", {})
        client.opener.open.assert_not_called()

    def test_non_timestamp_cookie_and_current_timestamp_need_no_sleep(self):
        client = self.client(100)
        with patch("integrations.nemo_http.time.time", return_value=101), patch("integrations.nemo_http.time.sleep") as sleep:
            client._wait_for_cookie_clock()
            sleep.assert_not_called()
        client.cookies = [SimpleNamespace(value="opaque")]
        self.assertEqual(client._wait_for_cookie_clock(), 0)
