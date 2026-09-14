import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import frontfile_runner as ffr


class TestDeliveryClassification(unittest.TestCase):
    """Catch-up decides what to do with each delivery from delivery_files alone."""

    def test_delivery_with_no_rows_is_new(self):
        states = ffr.classify_deliveries({}, [101])
        self.assertEqual(states[101][0], ffr.STATE_NEW)

    def test_delivery_with_all_files_completed_is_done(self):
        counts = {101: {"total": 4, "completed": 4, "failed": 0, "outstanding": 0}}
        states = ffr.classify_deliveries(counts, [101])
        self.assertEqual(states[101][0], ffr.STATE_DONE)

    def test_delivery_with_pending_files_is_outstanding(self):
        counts = {101: {"total": 4, "completed": 3, "failed": 0, "outstanding": 1}}
        states = ffr.classify_deliveries(counts, [101])
        self.assertEqual(states[101][0], ffr.STATE_OUTSTANDING)

    def test_delivery_with_failed_files_is_outstanding(self):
        counts = {101: {"total": 4, "completed": 3, "failed": 1, "outstanding": 1}}
        state, detail = ffr.classify_deliveries(counts, [101])[101]
        self.assertEqual(state, ffr.STATE_OUTSTANDING)
        self.assertEqual(detail["failed"], 1)

    def test_every_planned_delivery_is_classified(self):
        counts = {101: {"total": 2, "completed": 2, "failed": 0, "outstanding": 0}}
        states = ffr.classify_deliveries(counts, [101, 102, 103])
        self.assertEqual(set(states), {101, 102, 103})
        self.assertEqual(states[102][0], ffr.STATE_NEW)


def _summary(**kwargs):
    summary = ffr.RunSummary(
        mode=kwargs.pop("mode", "catchup"),
        started_at=datetime(2026, 9, 9, 10, 0, 0),
        log_file="/tmp/pipeline_frontfile.log",
    )
    summary.finished_at = datetime(2026, 9, 9, 10, 30, 15)
    summary.product_id = 3
    for key, value in kwargs.items():
        setattr(summary, key, value)
    return summary


class TestRunSummary(unittest.TestCase):
    def test_duration_is_human_readable(self):
        self.assertEqual(_summary().duration, "0h 30m 15s")

    def test_doc_totals_sum_across_deliveries(self):
        summary = _summary(
            processed=[
                {"week": "2026/009", "delivery_id": 1, "delivery_name": "a",
                 "stats": {"docs_upserted": 10, "docs_deleted": 2, "docs_skipped": 1, "total_files": 3}},
                {"week": "2026/010", "delivery_id": 2, "delivery_name": "b",
                 "stats": {"docs_upserted": 5, "docs_deleted": 0, "docs_skipped": 4, "total_files": 2}},
            ]
        )
        self.assertEqual(
            summary.doc_totals(),
            {"upserted": 15, "deleted": 2, "skipped": 5},
        )

    def test_doc_totals_include_the_failed_delivery(self):
        """A delivery that fails on its last volume has still written the earlier ones."""
        summary = _summary(
            processed=[{"week": "2026/009", "delivery_id": 1, "delivery_name": "a",
                        "stats": {"docs_upserted": 10, "docs_deleted": 0, "docs_skipped": 0}}],
            failed={"week": "2026/010", "delivery_id": 2, "delivery_name": "b",
                    "stats": {"docs_upserted": 4_200_000, "docs_deleted": 12, "docs_skipped": 3}},
        )
        totals = summary.doc_totals()
        self.assertEqual(totals["upserted"], 4_200_010)
        self.assertEqual(totals["deleted"], 12)

    def test_status_reflects_failure(self):
        self.assertEqual(_summary().status, "COMPLETED")
        self.assertEqual(_summary(failed={"week": "2026/009"}).status, "FAILED")
        self.assertEqual(_summary(fatal_error="BACKFILE_TIME is not set").status, "FAILED")


class TestReportEmail(unittest.TestCase):
    def test_success_report_lists_processed_and_skipped(self):
        summary = _summary(
            planned=3,
            processed=[{"week": "2026/010", "delivery_id": 2, "delivery_name": "DOCDB Amend",
                        "stats": {"docs_upserted": 1234, "docs_deleted": 5, "docs_skipped": 0, "total_files": 2}}],
            skipped=[{"week": "2026/009", "delivery_id": 1, "delivery_name": "DOCDB CreateDelete",
                      "total_files": 4}],
        )
        subject, body = ffr.build_report_email(summary)

        self.assertIn("COMPLETED", subject)
        self.assertIn("catchup", subject)
        self.assertIn("1,234", body)          # doc counts are formatted
        self.assertIn("DOCDB Amend", body)
        self.assertIn("DOCDB CreateDelete", body)
        self.assertIn("0h 30m 15s", body)
        self.assertIn("/tmp/pipeline_frontfile.log", body)

    def test_nothing_to_do_report(self):
        subject, body = ffr.build_report_email(_summary(planned=2, skipped=[
            {"week": "2026/009", "delivery_id": 1, "delivery_name": "x", "total_files": 1},
            {"week": "2026/010", "delivery_id": 2, "delivery_name": "y", "total_files": 1},
        ]))
        self.assertIn("nothing to do", subject)
        self.assertIn("no outstanding work", body)

    def test_failure_report_names_the_week_and_error(self):
        summary = _summary(
            planned=3,
            failed={
                "week": "2026/011", "delivery_id": 7, "delivery_name": "DOCDB Amend",
                "error": "boom",
                "stats": {"failed_files": [{"file_id": 42, "filename": "vol_001.zip", "error": "bad zip"}]},
            },
            not_attempted=[{"week": "2026/012", "delivery_id": 8, "delivery_name": "later"}],
        )
        subject, body = ffr.build_report_email(summary)

        self.assertIn("FAILED", subject)
        self.assertIn("2026/011", subject)
        self.assertIn("vol_001.zip", body)
        self.assertIn("bad zip", body)
        self.assertIn("--retry-failed", body)
        self.assertIn("later", body)

    def test_failure_report_shows_documents_written_before_the_failure(self):
        summary = _summary(
            planned=2,
            failed={
                "week": "2026/011", "delivery_id": 7, "delivery_name": "DOCDB Amend",
                "error": "boom",
                "stats": {"docs_upserted": 4_200_000, "docs_deleted": 12, "failed_files": []},
            },
        )
        _subject, body = ffr.build_report_email(summary)
        self.assertIn("Written before the failure", body)
        self.assertIn("4,200,000", body)
        self.assertNotIn("Documents upserted</b></td><td style='padding:2px 0;'>0<", body)

    def test_skipped_deliveries_show_their_file_count(self):
        summary = _summary(
            planned=1,
            skipped=[{"week": "2026/009", "delivery_id": 1,
                      "delivery_name": "DOCDB Cr-Del", "total_files": 5}],
        )
        _subject, body = ffr.build_report_email(summary)
        skipped_block = body.split("Deliveries skipped")[1]
        self.assertIn(">5<", skipped_block)

    def test_fatal_error_report(self):
        subject, body = ffr.build_report_email(_summary(fatal_error="BACKFILE_TIME is not set in .env."))
        self.assertIn("FAILED", subject)
        self.assertIn("run aborted", subject)
        self.assertIn("BACKFILE_TIME is not set", body)

    def test_delivery_names_are_html_escaped(self):
        summary = _summary(processed=[{"week": "2026/010", "delivery_id": 2,
                                       "delivery_name": "<script>alert(1)</script>", "stats": {}}])
        _subject, body = ffr.build_report_email(summary)
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)


class TestSendRunReport(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("EMAIL_RECIPIENT")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("EMAIL_RECIPIENT", None)
        else:
            os.environ["EMAIL_RECIPIENT"] = self._saved

    def test_missing_recipient_is_not_an_error(self):
        os.environ.pop("EMAIL_RECIPIENT", None)
        self.assertFalse(ffr.send_run_report(_summary()))

    def test_send_failure_never_propagates(self):
        os.environ["EMAIL_RECIPIENT"] = "ops@example.com"
        original = ffr.send_email
        try:
            ffr.send_email = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("smtp down"))
            self.assertFalse(ffr.send_run_report(_summary()))
        finally:
            ffr.send_email = original


# ── Stand-ins for the pipeline, so these tests touch no database or network ──

class _FakeDB:
    def __init__(self, counts):
        self._counts = counts

    def get_delivery_status_summary(self, product_id, delivery_ids):
        return self._counts


class _FakeOrchestrator:
    """Records what process_delivery asked of it."""

    counts = {}
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.synced = False
        self.ran = False
        self.db = _FakeDB(_FakeOrchestrator.counts)
        self.last_run_stats = {"docs_upserted": 7, "total_files": 2}
        _FakeOrchestrator.instances.append(self)

    def sync(self):
        self.synced = True

    def run(self, **kwargs):
        self.ran = True
        return True


class TestProcessDelivery(unittest.TestCase):
    def setUp(self):
        self._real = ffr.PipelineOrchestrator
        ffr.PipelineOrchestrator = _FakeOrchestrator
        _FakeOrchestrator.instances = []
        _FakeOrchestrator.counts = {}

    def tearDown(self):
        ffr.PipelineOrchestrator = self._real

    def _process(self, **kwargs):
        return ffr.process_delivery(
            product_id=3, delivery_id=101, delivery_name="DOCDB 2026/010 Amend", **kwargs
        )

    def test_always_syncs_before_running(self):
        """A file EPO adds after an earlier partial sync is only seen if we re-sync."""
        success, _stats, ran = self._process()
        self.assertTrue(success)
        self.assertTrue(ran)
        self.assertTrue(_FakeOrchestrator.instances[0].synced)
        self.assertTrue(_FakeOrchestrator.instances[0].run)

    def test_skip_when_complete_syncs_then_skips_the_run(self):
        _FakeOrchestrator.counts = {101: {"total": 4, "completed": 4, "failed": 0, "outstanding": 0}}
        success, stats, ran = self._process(skip_when_complete=True)
        self.assertTrue(success)
        self.assertFalse(ran)
        self.assertEqual(stats["total_files"], 4)
        self.assertTrue(_FakeOrchestrator.instances[0].synced)   # sync still happened
        self.assertFalse(_FakeOrchestrator.instances[0].ran)     # but no pipeline run

    def test_skip_when_complete_still_runs_if_the_sync_found_work(self):
        _FakeOrchestrator.counts = {101: {"total": 5, "completed": 4, "failed": 0, "outstanding": 1}}
        _success, _stats, ran = self._process(skip_when_complete=True)
        self.assertTrue(ran)
        self.assertTrue(_FakeOrchestrator.instances[0].ran)


class TestPlanBookkeeping(unittest.TestCase):
    def test_plan_entries_maps_the_plan_tuple_correctly(self):
        plan = [("2026/009", "DOCDB 2026/009 Amend", 101, "2026-03-01T00:00:00+00:00")]
        self.assertEqual(
            ffr._plan_entries(plan),
            [{"week": "2026/009", "delivery_id": 101, "delivery_name": "DOCDB 2026/009 Amend"}],
        )

    def _three_delivery_plan(self):
        return [
            {"deliveryId": i, "deliveryName": f"DOCDB - data 2026/{8 + i:03d} Amend",
             "deliveryPublicationDatetime": f"2026-03-0{i}T00:00:00+00:00"}
            for i in (1, 2, 3)
        ]

    def _run_execute(self, summary, process_delivery, patch_logger=None):
        """Drive _execute with the API, the database and the pipeline stubbed out."""
        import argparse

        args = argparse.Namespace(
            mode="catchup", dry_run=False, retry_failed=False, force_sync=False,
            no_email=True, batch_size=1000,
        )
        deliveries = self._three_delivery_plan()
        saved = (ffr.fetch_all_deliveries, ffr.load_delivery_states,
                 ffr.process_delivery, ffr.logger.info)
        os.environ["EPO_FRONTFILE_PRODUCT_ID"] = "3"
        os.environ["BACKFILE_TIME"] = "2026-01-01T00:00:00+00:00"
        try:
            ffr.fetch_all_deliveries = lambda product_id: deliveries
            ffr.load_delivery_states = lambda product_id, ids: {i: (ffr.STATE_NEW, {}) for i in ids}
            ffr.process_delivery = process_delivery
            if patch_logger:
                ffr.logger.info = patch_logger
            return ffr._execute(args, summary)
        finally:
            (ffr.fetch_all_deliveries, ffr.load_delivery_states,
             ffr.process_delivery, ffr.logger.info) = saved

    def test_failure_inside_a_delivery_halts_with_closed_bookkeeping(self):
        """The failed delivery goes to `failed`; only later ones are `not_attempted`."""
        def boom(**kwargs):
            if kwargs["delivery_id"] == 2:
                raise RuntimeError("connection dropped")
            return True, {"docs_upserted": 1}, True

        summary = ffr.RunSummary(mode="catchup", started_at=datetime(2026, 9, 9, 10, 0, 0))
        self.assertEqual(self._run_execute(summary, boom), 1)

        self.assertEqual([e["delivery_id"] for e in summary.processed], [1])
        self.assertEqual(summary.failed["delivery_id"], 2)
        self.assertIn("connection dropped", summary.failed["error"])
        self.assertEqual([e["delivery_id"] for e in summary.not_attempted], [3])
        # planned == processed + failed + not_attempted + skipped
        self.assertEqual(
            summary.planned,
            len(summary.processed) + 1 + len(summary.not_attempted) + len(summary.skipped),
        )

    def test_error_escaping_the_delivery_handler_records_the_remainder(self):
        """A failure outside the per-delivery try must not report 'nothing outstanding'.

        The trigger here is synthetic (a logging call), but it is the only way to
        reach the outer handler: anything raised between deliveries leaves the
        rest of the plan untouched and the report has to say so.
        """
        real_info = ffr.logger.info

        def flaky_info(msg, *a, **kw):
            if "Starting week=2026/010" in str(msg):
                raise RuntimeError("log sink died")
            return real_info(msg, *a, **kw)

        summary = ffr.RunSummary(mode="catchup", started_at=datetime(2026, 9, 9, 10, 0, 0))
        with self.assertRaises(RuntimeError):
            self._run_execute(
                summary,
                lambda **kwargs: (True, {"docs_upserted": 1}, True),
                patch_logger=flaky_info,
            )

        self.assertEqual([e["delivery_id"] for e in summary.processed], [1])
        self.assertEqual(
            [e["delivery_id"] for e in summary.not_attempted], [2, 3],
            "the delivery that blew up and everything after it are still outstanding",
        )


if __name__ == '__main__':
    unittest.main()
