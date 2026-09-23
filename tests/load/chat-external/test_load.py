import unittest
from concurrent.futures import Future
from unittest.mock import patch

from load import EvidenceError, percentile, run_schedule, schedule, sink_delivery


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class ImmediateExecutor:
    def __init__(self, max_workers):
        self.max_workers = max_workers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def submit(self, function, item):
        future = Future()
        future.set_result(function(item))
        return future


class HeldFuture:
    def __init__(self, function, item):
        self.function, self.item = function, item

    def done(self):
        return False

    def result(self):
        return self.function(self.item)


class HeldExecutor(ImmediateExecutor):
    def submit(self, function, item):
        return HeldFuture(function, item)


class LoadTests(unittest.TestCase):
    def test_plan_is_bounded_and_arrivals_are_open_loop(self):
        items = schedule(30, "distributed")
        self.assertEqual(len(items), 480)
        self.assertEqual(items[0]["offset_s"], 0)
        self.assertEqual(items[30]["offset_s"], 30)
        self.assertEqual(items[180]["offset_s"], 60)
        self.assertLess(items[-1]["offset_s"], 90)
        self.assertEqual(len({row["room_index"] for row in items}), 14)

    def test_hot_room_receives_exactly_eighty_percent(self):
        items = schedule(30, "hot")
        self.assertEqual(sum(item["room_index"] == 0 for item in items), 384)

    def test_bad_plan_is_refused(self):
        for seconds in (0, 31, True, 1.5):
            with self.assertRaises(EvidenceError):
                schedule(seconds, "distributed")

    def test_virtual_schedule_records_expected_and_actual_time(self):
        clock = Clock()
        items = schedule(1, "distributed")
        rows, reason = run_schedule(
            items,
            lambda _: True,
            clock=clock.now,
            sleep=clock.sleep,
            executor_factory=ImmediateExecutor,
        )
        self.assertIsNone(reason)
        self.assertEqual(len(rows), 16)
        self.assertTrue(all(row["lag_ms"] == 0 for row in rows))
        self.assertEqual(rows[-1]["started_s"], items[-1]["offset_s"])

    def test_failure_stops_further_injection(self):
        clock = Clock()
        rows, reason = run_schedule(
            schedule(1, "distributed"),
            lambda _: False,
            clock=clock.now,
            sleep=clock.sleep,
            executor_factory=ImmediateExecutor,
        )
        self.assertEqual(reason, "delivery_failure")
        self.assertEqual(len(rows), 1)

    def test_operator_stop_prevents_injection(self):
        rows, reason = run_schedule(
            schedule(1, "hot"), lambda _: True, stop_requested=lambda: True
        )
        self.assertEqual(rows, [])
        self.assertEqual(reason, "operator_stop")

    def test_no_unbounded_executor_queue(self):
        clock = Clock()
        rows, reason = run_schedule(
            schedule(1, "distributed"),
            lambda _: True,
            clock=clock.now,
            sleep=clock.sleep,
            executor_factory=HeldExecutor,
        )
        self.assertEqual(reason, "generator_concurrency_limit")
        self.assertEqual(len(rows), 8)

    def test_percentiles_use_samples_not_instance_averages(self):
        self.assertEqual(percentile(list(range(1, 101)), 0.95), 95)
        self.assertEqual(percentile(list(range(1, 101)), 0.99), 99)
        self.assertIsNone(percentile([], 0.95))

    def test_sink_request_has_no_credentials_or_redirects(self):
        with patch("load.HTTPConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.getheader.return_value = "application/json"
            response.read.return_value = (
                b'{"data":{"result":"acknowledged","http_status":201}}'
            )
            self.assertTrue(sink_delivery("http://127.0.0.1:18089"))
            headers = connection.return_value.request.call_args.kwargs["headers"]
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("Cookie", headers)
            response.status = 307
            with self.assertRaises(EvidenceError):
                sink_delivery("http://127.0.0.1:18089")


if __name__ == "__main__":
    unittest.main()
