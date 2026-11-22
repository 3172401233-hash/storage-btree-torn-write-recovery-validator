import unittest

from btree_recovery import (
    Page,
    RecoveryValidator,
    build_frame,
    build_page,
)


def make_page(page_id, lsn, **kwargs):
    kwargs.setdefault("body", b"page-%d-v%d" % (page_id, lsn))
    return build_page(page_id, lsn, **kwargs)


def torn(raw):
    """Simulate a half-written page: first half new, second half stale."""
    half = len(raw) // 2
    stale = bytearray(len(raw))
    for i in range(half, len(raw)):
        stale[i] = 0xAA
    return raw[:half] + bytes(stale[half:])


def corrupt(raw):
    """Flip bytes so the checksum no longer matches."""
    buf = bytearray(raw)
    buf[-1] ^= 0xFF
    buf[-2] ^= 0xFF
    return bytes(buf)


class TornPageTests(unittest.TestCase):
    def test_torn_page_recovers_from_doublewrite(self):
        old = make_page(1, 100)
        new = make_page(1, 110)
        data = {1: Page(torn(new))}  # half-written new version
        doublewrite = {1: Page(old)}
        report = RecoveryValidator(data, [], doublewrite,
                                   wal_durable_lsn=110).recover()
        self.assertEqual(report.applicable_versions[1], 100)
        self.assertNotIn(1, report.failed_pages)
        self.assertTrue(any("doublewrite" in r for r in report.repairs))

    def test_double_corruption_fails_page(self):
        new = make_page(1, 110)
        old = make_page(1, 100)
        data = {1: Page(torn(new))}
        doublewrite = {1: Page(corrupt(old))}
        report = RecoveryValidator(data, [], doublewrite,
                                   wal_durable_lsn=110).recover()
        self.assertIn(1, report.failed_pages)
        self.assertIn("both corrupt", report.failed_pages[1])
        self.assertNotIn(1, report.applicable_versions)


class WalReplayTests(unittest.TestCase):
    def test_replay_advances_page_and_reports_range(self):
        base = make_page(1, 100)
        img1 = make_page(1, 110)
        img2 = make_page(1, 120)
        frames = [
            build_frame(110, 1, 100, img1),
            build_frame(120, 1, 110, img2),
        ]
        report = RecoveryValidator({1: Page(base)}, frames, {},
                                   wal_durable_lsn=120).recover()
        self.assertEqual(report.applicable_versions[1], 120)
        self.assertEqual(report.replay_range, (110, 120))
        self.assertEqual(report.replay_order, [(110, 1), (120, 1)])
        self.assertNotIn(1, report.blocked_updates)

    def test_missing_frame_blocks_later_updates(self):
        base = make_page(1, 100)
        img2 = make_page(1, 120)
        # frame for LSN 110 is missing from the WAL
        frames = [build_frame(120, 1, 110, img2)]
        report = RecoveryValidator({1: Page(base)}, frames, {},
                                   wal_durable_lsn=120).recover()
        self.assertEqual(report.applicable_versions[1], 100)
        self.assertIn(1, report.blocked_updates)
        self.assertIn("missing WAL frame", report.blocked_updates[1])
        self.assertIsNone(report.replay_range)

    def test_wal_tail_truncation_hides_undurable_updates(self):
        base = make_page(1, 100)
        img1 = make_page(1, 110)
        img2 = make_page(1, 120)
        frames = [
            build_frame(110, 1, 100, img1),
            build_frame(120, 1, 110, img2),  # beyond durable boundary
        ]
        report = RecoveryValidator({1: Page(base)}, frames, {},
                                   wal_durable_lsn=110).recover()
        self.assertEqual(report.applicable_versions[1], 110)
        self.assertEqual(report.wal_truncated_at, 120)

    def test_corrupt_frame_truncates_wal(self):
        base = make_page(1, 100)
        img1 = make_page(1, 110)
        img2 = make_page(1, 120)
        good = build_frame(110, 1, 100, img1)
        bad = build_frame(120, 1, 110, img2)
        bad.checksum ^= 0xFFFF
        report = RecoveryValidator({1: Page(base)}, [good, bad], {},
                                   wal_durable_lsn=120).recover()
        self.assertEqual(report.applicable_versions[1], 110)
        self.assertEqual(report.wal_truncated_at, 120)

    def test_duplicate_replay_is_idempotent(self):
        base = make_page(1, 100)
        img1 = make_page(1, 110)
        frame = build_frame(110, 1, 100, img1)
        # duplicate frame in the log plus a replay_state left over from
        # an earlier recovery attempt that already applied LSN 110
        frames = [frame, frame]
        report = RecoveryValidator(
            {1: Page(base)}, frames, {},
            wal_durable_lsn=110, replay_state={1: 110}).recover()
        self.assertEqual(report.applicable_versions[1], 110)
        self.assertEqual(report.replay_order, [])
        self.assertNotIn(1, report.failed_pages)
        self.assertNotIn(1, report.blocked_updates)

    def test_conflicting_duplicates_truncate_wal(self):
        base = make_page(1, 100)
        frame_a = build_frame(110, 1, 100, make_page(1, 110, body=b"a"))
        frame_b = build_frame(110, 1, 100, make_page(1, 110, body=b"b"))
        report = RecoveryValidator({1: Page(base)}, [frame_a, frame_b], {},
                                   wal_durable_lsn=110).recover()
        self.assertEqual(report.wal_truncated_at, 110)
        self.assertEqual(report.applicable_versions[1], 110)


class ParentChildTests(unittest.TestCase):
    def test_parent_pointer_beyond_parent_lsn_fails_child(self):
        parent = make_page(10, 100)
        child = make_page(11, 125, parent_page=10, parent_pointer_lsn=120)
        report = RecoveryValidator(
            {10: Page(parent), 11: Page(child)}, [], {},
            wal_durable_lsn=200).recover()
        self.assertIn(11, report.failed_pages)
        self.assertIn("parent/child LSN inconsistency",
                      report.failed_pages[11])
        self.assertNotIn(11, report.applicable_versions)
        self.assertEqual(report.applicable_versions[10], 100)

    def test_consistent_parent_pointer_is_exposed(self):
        parent = make_page(10, 130)
        child = make_page(11, 105, parent_page=10, parent_pointer_lsn=120)
        report = RecoveryValidator(
            {10: Page(parent), 11: Page(child)}, [], {},
            wal_durable_lsn=200).recover()
        self.assertNotIn(11, report.failed_pages)
        self.assertEqual(report.applicable_versions[11], 105)


class SiblingRepairTests(unittest.TestCase):
    def test_sibling_pointer_repair(self):
        left = make_page(1, 100, right_sibling=2)
        # page 2 lost its back-pointer (stale image)
        right = make_page(2, 100, left_sibling=None)
        report = RecoveryValidator(
            {1: Page(left), 2: Page(right)}, [], {},
            wal_durable_lsn=100).recover()
        repaired = report.recovered_pages[2]
        self.assertEqual(repaired.header.left_sibling, 1)
        self.assertTrue(repaired.valid)
        self.assertTrue(any("repaired" in r for r in report.repairs))

    def test_consistent_siblings_need_no_repair(self):
        left = make_page(1, 100, right_sibling=2)
        right = make_page(2, 100, left_sibling=1)
        report = RecoveryValidator(
            {1: Page(left), 2: Page(right)}, [], {},
            wal_durable_lsn=100).recover()
        self.assertEqual(report.repairs, [])


if __name__ == "__main__":
    unittest.main()
