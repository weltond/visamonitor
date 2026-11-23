import contextlib
import datetime as dt
import io
import json
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

import monitor


TEST_KEY = "fake-device-key-for-tests"


def response(code=200, status=200):
    reply = mock.MagicMock(status=status)
    reply.__enter__.return_value = reply
    reply.read.return_value = json.dumps({"code": code, "message": TEST_KEY}).encode()
    return reply


def record(date="2027-01-15", city="上海", visa="B1/B2"):
    return {"city": city, "visa": visa, "date": date, "age": "刚刚"}


class NotificationTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.state = tmp / "state.json"
        stack.enter_context(mock.patch.multiple(
            monitor, NOTIFY_CHANNELS=("bark",), BARK_KEY=TEST_KEY,
            BARK_SERVER="https://bark.example", BARK_LEVEL="timeSensitive",
            NTFY_SERVER="https://ntfy.example", NTFY_TOPIC="test-topic",
            NTFY_TOKEN="test-token", EMAIL_TO="alert@example.com",
            STATE_FILE=self.state, HISTORY_FILE=tmp / "history.jsonl",
            CITIES=["上海"], VISA_PREFIX="B1/B2", CUTOFF="2027-01-31",
            TODAY=dt.date(2027, 1, 1), STALE_AFTER_MIN=720, PUSH_REPEAT=2, PUSH_INTERVAL=5,
        ))
        self.bark = stack.enter_context(mock.patch.object(monitor._BARK_OPENER, "open", return_value=response()))
        self.ntfy = stack.enter_context(mock.patch.object(monitor.urllib.request, "urlopen", return_value=response()))
        self.sleep = stack.enter_context(mock.patch.object(monitor.time, "sleep"))
        self.log = io.StringIO()
        stack.enter_context(contextlib.redirect_stdout(self.log))

    def check(self, *records):
        monitor.check_and_notify({"records": records}, verbose=False)

    def test_channel_configuration_and_defaults(self):
        with mock.patch.dict("os.environ", {"VISA_NOTIFY_CHANNELS": ""}, clear=True):
            config = runpy.run_path(monitor.__file__)
        self.assertEqual(config["NOTIFY_CHANNELS"], ("ntfy",))
        self.assertEqual(config["BARK_SERVER"], "https://api.day.app")
        self.assertEqual(config["BARK_LEVEL"], "timeSensitive")
        with mock.patch.dict("os.environ", {
            "VISA_NOTIFY_CHANNELS": "bark, ntfy, bark",
            "VISA_BARK_KEY": TEST_KEY,
            "VISA_BARK_SERVER": "https://bark.example/subpath/",
            "VISA_BARK_LEVEL": "active",
        }, clear=True):
            config = runpy.run_path(monitor.__file__)
        self.assertEqual(config["NOTIFY_CHANNELS"], ("bark", "ntfy"))
        self.assertEqual(config["BARK_KEY"], TEST_KEY)
        self.assertEqual(config["BARK_SERVER"], "https://bark.example/subpath")
        self.assertEqual(config["BARK_LEVEL"], "active")

    def test_bark_alert_uses_source_dates_and_official_link(self):
        self.check()  # establish baseline
        self.check(record("2027-01-20"), record("2027-01-15"), record("2027-01-15"))
        self.bark.assert_called_once()
        self.ntfy.assert_not_called()
        req = self.bark.call_args.args[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, "https://bark.example/push")
        self.assertEqual(req.get_header("Content-type"), "application/json; charset=utf-8")
        self.assertEqual(self.bark.call_args.kwargs["timeout"], 10)
        payload = json.loads(req.data)
        self.assertEqual(payload["device_key"], TEST_KEY)
        self.assertEqual(payload["title"], "上海美签发现新号源！")
        self.assertIn("签证类型：B1/B2\n领事馆：上海\n可预约日期：2027-01-15", payload["body"])
        self.assertIn("本批共 2 个日期", payload["body"])
        self.assertEqual(payload["level"], "timeSensitive")
        self.assertEqual(payload["group"], "USVisa")
        self.assertEqual(payload["url"], "https://www.usvisascheduling.com/")
        self.assertIn("[bark] push accepted", self.log.getvalue())
        self.assertNotIn(TEST_KEY, self.log.getvalue())
        self.assertNotIn(TEST_KEY, self.state.read_text())

    def test_missing_bark_key_does_not_make_a_request(self):
        with mock.patch.object(monitor, "BARK_KEY", ""):
            self.assertFalse(monitor.notify("Test", "Test"))
        self.assertIn("VISA_BARK_KEY not set", self.log.getvalue())
        self.bark.assert_not_called()

    def test_http_authentication_and_redirect_errors_are_not_retried(self):
        for status in (401, 302):
            with self.subTest(status=status):
                body = io.BytesIO(TEST_KEY.encode())
                self.bark.reset_mock()
                self.bark.side_effect = HTTPError(f"https://bark.example/{TEST_KEY}", status,
                                                  TEST_KEY, None, body)
                self.assertFalse(monitor.notify("Test", "Test"))
                self.bark.assert_called_once()
                self.sleep.assert_not_called()
                self.assertTrue(body.closed)
                self.assertIn(f"HTTP {status}", self.log.getvalue())
        self.assertNotIn(TEST_KEY, self.log.getvalue())
        self.assertNotIn("https://bark.example", self.log.getvalue())

    def test_business_error_is_not_retried(self):
        self.bark.return_value = response(code=400)
        self.assertFalse(monitor.notify("Test", "Test"))
        self.bark.assert_called_once()
        self.sleep.assert_not_called()
        self.assertIn("API rejected push (code 400)", self.log.getvalue())
        self.assertNotIn(TEST_KEY, self.log.getvalue())

    def test_malformed_business_response_is_rejected_without_logging_body(self):
        self.bark.return_value.read.return_value = TEST_KEY.encode()
        self.assertFalse(monitor.notify("Test", "Test"))
        self.bark.assert_called_once()
        self.sleep.assert_not_called()
        self.assertIn("invalid JSON response", self.log.getvalue())
        self.assertNotIn(TEST_KEY, self.log.getvalue())

    def test_network_timeout_has_bounded_retries(self):
        self.bark.side_effect = URLError(TimeoutError(TEST_KEY))
        self.assertFalse(monitor.notify("Test", "Test"))
        self.assertEqual(self.bark.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [mock.call(1), mock.call(2)])
        self.assertIn("TimeoutError", self.log.getvalue())
        self.assertNotIn(TEST_KEY, self.log.getvalue())

    def test_transient_http_error_retries_and_can_recover(self):
        self.bark.side_effect = [HTTPError("https://bark.example/push", 503, "busy", None, None),
                                 response()]
        self.assertTrue(monitor.notify("Test", "Test"))
        self.assertEqual(self.bark.call_count, 2)
        self.sleep.assert_called_once_with(1)
        self.assertIn("HTTP 503, retrying", self.log.getvalue())

    def test_multiple_channels_send_independently(self):
        with mock.patch.object(monitor, "NOTIFY_CHANNELS", ("bark", "ntfy")):
            self.check()
            self.check(record())
        self.bark.assert_called_once()
        self.assertEqual(self.ntfy.call_count, 2)
        self.assertIn("[bark] push accepted", self.log.getvalue())
        self.assertIn("[ntfy] push accepted", self.log.getvalue())

    def test_bark_failure_does_not_block_or_replay_ntfy(self):
        self.bark.return_value = response(code=400)
        with mock.patch.object(monitor, "NOTIFY_CHANNELS", ("bark", "ntfy")):
            self.check()
            self.check(record())
            self.check(record())  # check_and_notify reloads persisted state
        self.bark.assert_called_once()
        self.assertEqual(self.ntfy.call_count, 2)
        self.assertIn("[ntfy] push accepted", self.log.getvalue())

    def test_ntfy_failure_does_not_block_or_replay_bark(self):
        self.ntfy.side_effect = URLError("unreachable")
        with mock.patch.object(monitor, "NOTIFY_CHANNELS", ("ntfy", "bark")):
            self.check()
            self.check(record())
            self.check(record())
        self.bark.assert_called_once()
        self.assertEqual(self.ntfy.call_count, 3)  # repeat plus the first e-mail fallback
        self.assertIn("[bark] push accepted", self.log.getvalue())

    def test_baseline_and_reappearing_records_do_not_alert(self):
        new = "2027-01-15"
        self.check(record("2027-01-13"))
        self.bark.assert_not_called()
        self.check(record(new), record(new))
        self.check(record(new))
        self.check()
        self.check(record(new), record("2027-01-13"))
        self.bark.assert_called_once()
        self.assertIn(new, monitor.load_state()["known"]["上海|B1/B2"])

    def test_no_qualifying_records_does_not_send_slot_alert(self):
        self.check()
        self.check(record("2027-02-01"), record(city="广州"), record(visa="F-1"))
        self.bark.assert_not_called()
        self.ntfy.assert_not_called()

    def test_source_recovers_only_after_a_fresh_record_arrives(self):
        old = record()
        old["age"] = "1天"
        self.check(old)
        self.check()
        self.assertEqual(self.bark.call_count, 1)
        self.assertTrue(monitor.load_state()["src_stale"])
        self.check(record())
        self.assertEqual(self.bark.call_count, 2)
        self.assertFalse(monitor.load_state()["src_stale"])
        self.assertIn("source is LIVE again", json.loads(self.bark.call_args.args[0].data)["title"])

    def test_ntfy_only_preserves_repeats_email_and_click(self):
        with mock.patch.object(monitor, "NOTIFY_CHANNELS", ("ntfy",)):
            self.check()
            self.check(record())
        self.bark.assert_not_called()
        self.assertEqual(self.ntfy.call_count, 2)
        first, second = [call.args[0] for call in self.ntfy.call_args_list]
        self.assertEqual(first.full_url, "https://ntfy.example/test-topic")
        self.assertEqual(first.get_header("Email"), "alert@example.com")
        self.assertIsNone(second.get_header("Email"))
        self.assertEqual(first.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(first.get_header("Click"), "https://qmq.app")
        self.assertIn("(1/2)", first.get_header("Title"))
        self.assertIn("(2/2)", second.get_header("Title"))
        self.assertIn("上海: B1/B2 2027-01-15", first.data.decode())

    def test_ntfy_email_failure_retries_without_email(self):
        self.ntfy.side_effect = [HTTPError("https://ntfy.example/test-topic", 400, "email", None, None),
                                 response(), response()]
        with mock.patch.object(monitor, "NOTIFY_CHANNELS", ("ntfy",)):
            self.check()
            self.check(record())
        self.assertEqual(self.ntfy.call_count, 3)
        first, fallback, second = [call.args[0] for call in self.ntfy.call_args_list]
        self.assertEqual(first.get_header("Title"), fallback.get_header("Title"))
        self.assertIsNone(fallback.get_header("Email"))
        self.assertIsNone(second.get_header("Email"))

    def test_test_push_exit_code_without_scrape_or_state_write(self):
        with mock.patch("sys.argv", [monitor.__file__, "--test-push"]), \
                mock.patch.object(monitor, "scrape") as scrape:
            with self.assertRaises(SystemExit) as success:
                monitor.main()
            self.assertEqual(success.exception.code, 0)
            self.bark.return_value = response(code=400)
            with self.assertRaises(SystemExit) as failure:
                monitor.main()
            self.assertEqual(failure.exception.code, 1)
        scrape.assert_not_called()
        self.assertFalse(self.state.exists())
