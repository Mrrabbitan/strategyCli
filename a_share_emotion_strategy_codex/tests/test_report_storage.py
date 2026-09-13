"""Report interface regressions use a temporary private store."""
import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run_report


class ReportStorageTests(unittest.TestCase):
    def test_previous_audit_is_loaded_only_from_private_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            day = dt.date(2026, 9, 11)
            folder = root / "reports" / day.isoformat()
            folder.mkdir(parents=True)
            expected = {"trade_day_verified": True, "quote_validation_passed": True, "report_date": str(day)}
            (folder / "19-00.json").write_text(json.dumps(expected))
            with patch.object(run_report, "private_path", side_effect=lambda p: root / p), \
                    patch.object(run_report, "previous_trading_day", return_value=day):
                self.assertEqual(run_report.load_previous_audited_snapshot(dt.date(2026, 9, 14)), expected)
                self.assertEqual(run_report.load_previous_close_snapshot(dt.date(2026, 9, 14)), expected)
                (folder / "19-00.json").write_text(json.dumps(dict(expected, quote_validation_passed=False)))
                self.assertIsNone(run_report.load_previous_audited_snapshot(dt.date(2026, 9, 14)))
                self.assertIsNone(run_report.load_previous_close_snapshot(dt.date(2026, 9, 14)))

    def test_closed_session_does_not_fetch_quotes_or_write_reports(self):
        output = io.StringIO()
        with patch("sys.argv", ["run_report.py", "--date", "2026-09-13", "--slot", "09_00"]), \
                patch.object(run_report, "load_config", return_value={}), \
                patch.object(run_report, "calendar_is_trading_day", return_value=(False, "休市")), \
                patch.object(run_report, "fetch_tencent_quotes") as quotes, \
                patch.object(run_report, "rebuild_dashboard") as render, contextlib.redirect_stdout(output):
            self.assertEqual(run_report.main(), 0)
        quotes.assert_not_called()
        render.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["status"], "skipped")

    def test_quote_cutoffs_and_five_report_slots_remain(self):
        self.assertEqual(len(run_report.SLOTS), 5)
        self.assertTrue(run_report.quote_time_fresh_for_slot("20260911103000", "10_30"))
        self.assertFalse(run_report.quote_time_fresh_for_slot("20260911093100", "10_30"))
        self.assertTrue(run_report.quote_time_fresh_for_slot("20260911150000", "19_00"))
        self.assertEqual(run_report.choose_slot(dt.datetime(2026, 9, 11, 14, 15)), "14_30")

    def test_event_cutoff_excludes_later_events_and_historical_status(self):
        now = dt.datetime(2026, 9, 11, 10, 35, tzinfo=run_report.TZ)
        with patch("monitor_feed.read_events", return_value={"events": []}) as events, \
                patch("monitor_feed.read_status", return_value={"as_of": now.isoformat()}) as status:
            current = run_report.report_monitor_context(now.date(), "10_30", now)
            events.assert_called_once_with(now.date(), cutoff=now.replace(minute=30))
            status.assert_called_once_with(now=now)
            self.assertEqual(current["monitor_cutoff"], now.replace(minute=30).isoformat())
            events.reset_mock()
            status.reset_mock()
            past = now.date() - dt.timedelta(days=1)
            history = run_report.report_monitor_context(past, "19_00", now)
            status.assert_not_called()
            self.assertIsNone(history["monitor_status"])
            self.assertEqual(events.call_args.kwargs["cutoff"].date(), past)

    def test_early_run_does_not_include_future_requested_slot(self):
        now = dt.datetime(2026, 9, 11, 10, 15, tzinfo=run_report.TZ)
        with patch("monitor_feed.read_events", return_value={"events": []}) as events, \
                patch("monitor_feed.read_status", return_value={}):
            run_report.report_monitor_context(now.date(), "10_30", now)
        events.assert_called_once_with(now.date(), cutoff=now)

    def test_atomic_snapshot_precedes_build_and_diagnostic_does_not_build(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "reports" / "test.json"
            day = dt.date(2026, 9, 11)
            snapshot = {"report_date": str(day), "slot": "10_30", "trade_day_verified": True}
            def check_saved(report_date):
                self.assertEqual(report_date, day)
                self.assertEqual(json.loads(target.read_text()), snapshot)
            with patch.object(run_report, "rebuild_dashboard", side_effect=check_saved) as rebuild:
                run_report.save_report_snapshot(target, snapshot, day, refresh=True)
                rebuild.assert_called_once_with(day)
            with patch.object(run_report, "rebuild_dashboard") as rebuild:
                run_report.save_report_snapshot(target, snapshot, day, refresh=False)
                rebuild.assert_not_called()
                with self.assertRaises(ValueError):
                    run_report.save_report_snapshot(target, {"invalid": float("nan")}, day, refresh=True)
                self.assertEqual(json.loads(target.read_text()), snapshot)
                rebuild.assert_not_called()


if __name__ == "__main__":
    unittest.main()
