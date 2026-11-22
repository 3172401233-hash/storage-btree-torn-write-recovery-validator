"""Crash-recovery validation for B-tree pages.

The validator combines the data file, the WAL and the doublewrite area
and decides, per page, which version may safely be exposed:

1. Base image selection: a data page with a valid checksum is trusted.
   A torn page falls back to its doublewrite copy. If both are corrupt
   the page is unrecoverable.
2. WAL truncation: frames past the WAL durability boundary, frames with
   a bad checksum, and conflicting duplicate frames truncate the log;
   nothing after the truncation point is replayed.
3. Per-page replay: frames form a chain via ``prev_page_lsn``. A missing
   frame stops the chain; later updates for that page stay hidden.
4. Parent/child consistency: a child page may not be exposed at a
   version whose parent pointer LSN the parent page cannot reach.
5. Sibling repair: cross-linked sibling pointers are verified and
   repaired when both sides are recoverable.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .page import Page
from .wal import WALFrame


@dataclass
class RecoveryReport:
    # page_id -> highest LSN that may safely be exposed
    applicable_versions: Dict[int, int] = field(default_factory=dict)
    # page_id -> recovered page image at the applicable version
    recovered_pages: Dict[int, Page] = field(default_factory=dict)
    # (first replayed LSN, last replayed LSN), None if nothing replayed
    replay_range: Optional[Tuple[int, int]] = None
    # global replay order as (lsn, page_id) pairs
    replay_order: List[Tuple[int, int]] = field(default_factory=list)
    # page_id -> reason the page cannot be exposed at all
    failed_pages: Dict[int, str] = field(default_factory=dict)
    # page_id -> reason replay stopped before the newest durable update
    blocked_updates: Dict[int, str] = field(default_factory=dict)
    # human-readable repair actions (sibling pointer fixes)
    repairs: List[str] = field(default_factory=list)
    # first WAL LSN that was dropped, if the log was truncated
    wal_truncated_at: Optional[int] = None


class RecoveryValidator:
    def __init__(self, data_pages, wal_frames, doublewrite_copies,
                 wal_durable_lsn, replay_state=None):
        """
        :param data_pages: {page_id: Page} as found in the data file.
        :param wal_frames: [WALFrame] in log order.
        :param doublewrite_copies: {page_id: Page} from the doublewrite area.
        :param wal_durable_lsn: highest LSN known to be persisted in the WAL.
        :param replay_state: {page_id: lsn} already replayed before an
            earlier crash during recovery; used to skip duplicate frames.
        """
        self.data_pages = dict(data_pages)
        self.wal_frames = list(wal_frames)
        self.doublewrite = dict(doublewrite_copies)
        self.wal_durable_lsn = wal_durable_lsn
        self.replay_state = dict(replay_state or {})

    def recover(self) -> RecoveryReport:
        report = RecoveryReport()
        frames = self._truncate_wal(report)
        bases = self._select_base_images(report)
        chains = self._replay_chains(frames, bases, report)
        self._check_parent_pointers(chains, report)
        self._repair_sibling_links(report)
        applied = [lsn for lsn, _ in report.replay_order]
        if applied:
            report.replay_range = (min(applied), max(applied))
        return report

    # ------------------------------------------------------------------
    # Step 1: WAL truncation
    # ------------------------------------------------------------------
    def _truncate_wal(self, report) -> List[WALFrame]:
        usable = []
        seen = {}  # (page_id, lsn) -> frame, for duplicate detection
        for frame in self.wal_frames:
            if frame.lsn > self.wal_durable_lsn:
                report.wal_truncated_at = frame.lsn
                break
            if not frame.valid:
                report.wal_truncated_at = frame.lsn
                break
            key = (frame.page_id, frame.lsn)
            if key in seen:
                if seen[key].image != frame.image:
                    # conflicting duplicates: log is corrupt from here on
                    report.wal_truncated_at = frame.lsn
                    break
                continue  # identical duplicate frame: safe to ignore
            seen[key] = frame
            usable.append(frame)
        return usable

    # ------------------------------------------------------------------
    # Step 2: base image selection (torn page / doublewrite)
    # ------------------------------------------------------------------
    def _select_base_images(self, report) -> Dict[int, Page]:
        page_ids = set(self.data_pages) | set(self.doublewrite)
        bases = {}
        for pid in sorted(page_ids):
            data = self.data_pages.get(pid)
            if data is not None and data.valid:
                bases[pid] = data
                continue
            copy = self.doublewrite.get(pid)
            if copy is not None and copy.valid:
                bases[pid] = copy
                report.repairs.append(
                    "page %d: torn data page restored from doublewrite "
                    "copy at LSN %d" % (pid, copy.header.page_lsn))
                continue
            reason = "torn page (checksum mismatch)"
            if copy is not None:
                reason = ("data page and doublewrite copy both corrupt; "
                          "no trustworthy base image")
            report.failed_pages[pid] = reason
        return bases

    # ------------------------------------------------------------------
    # Step 3: per-page replay chains
    # ------------------------------------------------------------------
    def _replay_chains(self, frames, bases, report):
        by_page = {}
        for frame in frames:
            by_page.setdefault(frame.page_id, []).append(frame)
        for pid in by_page:
            by_page[pid].sort(key=lambda f: f.lsn)

        chains = {}  # pid -> list of applied frames (in order)
        for pid, base in bases.items():
            current_lsn = max(base.header.page_lsn,
                              self.replay_state.get(pid, 0))
            current_page = base
            applied = []
            for frame in by_page.get(pid, []):
                if frame.lsn <= current_lsn:
                    continue  # already applied / duplicate replay
                if frame.prev_page_lsn != current_lsn:
                    report.blocked_updates[pid] = (
                        "missing WAL frame: expected frame with "
                        "prev_page_lsn=%d, found LSN %d; updates from "
                        "LSN %d onward stay hidden"
                        % (current_lsn, frame.lsn, frame.lsn))
                    break
                image = Page(frame.image)
                if not image.valid:
                    report.blocked_updates[pid] = (
                        "WAL frame at LSN %d carries a corrupt page image; "
                        "updates from LSN %d onward stay hidden"
                        % (frame.lsn, frame.lsn))
                    break
                current_page = image
                current_lsn = frame.lsn
                applied.append(frame)
            chains[pid] = applied
            report.applicable_versions[pid] = current_lsn
            report.recovered_pages[pid] = current_page
            report.replay_order.extend((f.lsn, pid) for f in applied)

        # frames for pages with no base image at all
        for pid, page_frames in by_page.items():
            if pid in bases:
                continue
            report.failed_pages[pid] = (
                "no base image in data file or doublewrite area; "
                "%d WAL frame(s) cannot be replayed" % len(page_frames))

        report.replay_order.sort()
        return chains

    # ------------------------------------------------------------------
    # Step 4: parent pointer vs child LSN consistency
    # ------------------------------------------------------------------
    def _check_parent_pointers(self, chains, report):
        for pid, page in list(report.recovered_pages.items()):
            parent = page.header.parent_page
            pointer_lsn = page.header.parent_pointer_lsn
            if parent is None or pointer_lsn == 0:
                continue
            if parent in report.failed_pages or parent not in \
                    report.applicable_versions:
                self._block_or_fail_child(
                    pid, page, pointer_lsn, report,
                    "parent page %d is unrecoverable" % parent)
                continue
            parent_lsn = report.applicable_versions[parent]
            if pointer_lsn > parent_lsn:
                self._block_or_fail_child(
                    pid, page, pointer_lsn, report,
                    "parent pointer LSN %d exceeds parent page %d "
                    "recoverable LSN %d" % (pointer_lsn, parent, parent_lsn))

    def _block_or_fail_child(self, pid, page, pointer_lsn, report, why):
        base_lsn = self._base_lsn(pid)
        if base_lsn >= pointer_lsn:
            # even the base image references a pointer the parent never
            # persisted: no safe version exists
            report.failed_pages[pid] = (
                "parent/child LSN inconsistency: %s" % why)
            report.applicable_versions.pop(pid, None)
            report.recovered_pages.pop(pid, None)
            report.replay_order = [e for e in report.replay_order
                                   if e[1] != pid]
            return
        # cap the child below the unsafe pointer LSN
        report.blocked_updates[pid] = (
            "parent/child LSN inconsistency: %s; child capped below "
            "LSN %d" % (why, pointer_lsn))
        report.applicable_versions[pid] = base_lsn
        report.recovered_pages[pid] = self._base_page(pid)
        report.replay_order = [e for e in report.replay_order if e[1] != pid]

    def _base_lsn(self, pid):
        data = self.data_pages.get(pid)
        if data is not None and data.valid:
            return data.header.page_lsn
        copy = self.doublewrite.get(pid)
        if copy is not None and copy.valid:
            return copy.header.page_lsn
        return 0

    def _base_page(self, pid):
        data = self.data_pages.get(pid)
        if data is not None and data.valid:
            return data
        return self.doublewrite.get(pid)

    # ------------------------------------------------------------------
    # Step 5: sibling pointer repair
    # ------------------------------------------------------------------
    def _repair_sibling_links(self, report):
        for pid, page in list(report.recovered_pages.items()):
            for attr, peer_attr, direction in (
                    ("right_sibling", "left_sibling", "right"),
                    ("left_sibling", "right_sibling", "left")):
                sibling_id = getattr(page.header, attr)
                if sibling_id is None:
                    continue
                sibling = report.recovered_pages.get(sibling_id)
                if sibling is None:
                    report.repairs.append(
                        "page %d: %s sibling %d unavailable, pointer "
                        "left as-is" % (pid, direction, sibling_id))
                    continue
                if getattr(sibling.header, peer_attr) != pid:
                    fixed = sibling.with_links(
                        **{peer_attr: pid})
                    report.recovered_pages[sibling_id] = fixed
                    report.repairs.append(
                        "page %d: repaired %s sibling pointer of page %d "
                        "to point back to %d"
                        % (pid, direction, sibling_id, pid))
