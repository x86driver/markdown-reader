from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from markdown_finder.engine import FileEntry, scan_markdown, search_files


def entry(relative: str) -> FileEntry:
    return FileEntry(f"/home/test/{relative}", Path(relative).name, relative)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def scan(self, **overrides):
        self.batches = []
        self.progress = []
        callbacks = dict(
            cancelled=lambda: False,
            on_batch=self.batches.append,
            on_progress=self.progress.append,
        )
        callbacks.update(overrides)
        return scan_markdown(self.root, **callbacks)

    def create(self, relative: str):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Example", encoding="utf-8")
        return path

    def test_all_hidden_and_ignored_paths_and_case_insensitive_extension(self):
        expected = {".hidden.md", ".config/deep/NOTES.MD", "node_modules/lib/README.md", "日記/今天.md"}
        for relative in expected | {"text.txt", ".gitignore"}:
            self.create(relative)
        stats = self.scan()
        found = {item.relative_path for batch in self.batches for item in batch}
        self.assertEqual(found, expected)
        self.assertEqual(stats.markdown_files, 4)
        self.assertEqual(stats.files_seen, 6)
        self.assertEqual(stats.inaccessible, 0)
        self.assertEqual(self.progress[-1], stats)

    def test_batches_are_bounded_and_final_partial_batch_is_delivered(self):
        for index in range(520):
            self.create(f"{index}.md")
        stats = self.scan()
        self.assertEqual(sum(map(len, self.batches)), 520)
        self.assertTrue(all(0 < len(batch) <= 256 for batch in self.batches))
        self.assertEqual(stats.markdown_files, 520)

    def test_symlink_directory_cycles_skipped_but_regular_file_links_included(self):
        target = self.create("notes/real.md")
        (self.root / "notes" / "cycle").symlink_to(self.root, target_is_directory=True)
        (self.root / "file-link.md").symlink_to(target)
        (self.root / "broken.md").symlink_to(self.root / "missing.md")
        stats = self.scan()
        found = {item.relative_path for batch in self.batches for item in batch}
        self.assertEqual(found, {"notes/real.md", "file-link.md"})
        self.assertEqual(stats.skipped_symlinks, 2)
        self.assertEqual(stats.directories, 2)

    def test_permission_failure_keeps_scanning_other_directories(self):
        self.create("private/secret.md")
        self.create("readable/public.md")
        original_scandir = os.scandir

        def guarded_scandir(path):
            if Path(path).name == "private":
                raise PermissionError(13, "Permission denied", str(path))
            return original_scandir(path)

        with patch("markdown_finder.engine.os.scandir", side_effect=guarded_scandir):
            stats = self.scan()
        self.assertEqual(stats.inaccessible, 1)
        self.assertEqual(stats.markdown_files, 1)
        self.assertIn("private", stats.errors[0])

    def test_cancellation_flushes_already_found_entries(self):
        for index in range(100):
            self.create(f"{index}.md")
        calls = 0

        def stop():
            nonlocal calls
            calls += 1
            return calls >= 12

        stats = self.scan(cancelled=stop)
        self.assertTrue(stats.cancelled)
        self.assertGreater(stats.markdown_files, 0)
        self.assertLess(stats.markdown_files, 100)
        self.assertEqual(sum(map(len, self.batches)), stats.markdown_files)

    def test_missing_root_reports_error(self):
        stats = scan_markdown(
            self.root / "missing", cancelled=lambda: False,
            on_batch=lambda batch: self.fail("Unexpected entries"), on_progress=lambda stats: None,
        )
        self.assertEqual(stats.inaccessible, 1)
        self.assertEqual(stats.markdown_files, 0)


class SearchTests(unittest.TestCase):
    def test_tokens_match_across_path_and_basename(self):
        target = entry("projects/cpu/docs/pipeline.md")
        results = search_files([target, entry("other/gpu/pipeline.md")], "cpu pipe")
        self.assertEqual([hit.entry for hit in results.matches], [target])

    def test_ordered_subsequence_and_all_tokens_are_required(self):
        entries = [entry("pipeline.md"), entry("plain.md"), entry("unrelated.md")]
        self.assertEqual([hit.entry.name for hit in search_files(entries, "ppln").matches], ["pipeline.md"])
        self.assertEqual(search_files(entries, "pipline extra").total_matches, 0)
        self.assertEqual(search_files(entries, "enilepip").total_matches, 0)

    def test_unicode_normalization_and_chinese_subsequences(self):
        target = entry("處理器/流水線設計.md")
        full_width = entry("設計/ＣＰＵ.md")
        self.assertEqual(search_files([target], "處理 流線").matches[0].entry, target)
        self.assertEqual(search_files([full_width], "cpu").matches[0].entry, full_width)
        self.assertEqual(search_files([entry("Straße.md")], "STRASSE").total_matches, 1)

    def test_basename_exact_prefix_substring_and_path_ranking(self):
        entries = [
            entry("cpu/reference.md"), entry("docs/old-cpu-notes.md"),
            entry("docs/cpu-notes.md"), entry("docs/cpu.md"),
        ]
        ranked = [hit.entry.name for hit in search_files(entries, "cpu").matches]
        self.assertEqual(ranked, ["cpu.md", "cpu-notes.md", "old-cpu-notes.md", "reference.md"])

    def test_recent_boost_cannot_overcome_basename_preference(self):
        exact, path_only = entry("cpu.md"), entry("cpu/reference.md")
        self.assertEqual(search_files([path_only, exact], "cpu", recent=[path_only.path]).matches[0].entry, exact)

    def test_blank_query_recency_then_deterministic_names_and_paths(self):
        a, b, c, d = entry("z/a.md"), entry("a/a.md"), entry("b.md"), entry("c.md")
        results = search_files([c, a, d, b], "  ", recent=[d.path, c.path, d.path])
        self.assertEqual([hit.entry for hit in results.matches], [d, c, b, a])

    def test_limit_does_not_change_match_count(self):
        entries = [entry(f"notes/{index}.md") for index in range(20)]
        results = search_files(entries, "note", limit=3)
        self.assertEqual(len(results.matches), 3)
        self.assertEqual(results.total_matches, 20)
        empty = search_files(entries, "note", limit=0)
        self.assertEqual(empty.matches, [])
        self.assertEqual(empty.total_matches, 20)

    def test_cancellation_returns_no_stale_hits(self):
        calls = 0

        def stop():
            nonlocal calls
            calls += 1
            return calls > 3

        results = search_files([entry(f"note-{i}.md") for i in range(20)], "note", cancelled=stop)
        self.assertTrue(results.cancelled)
        self.assertEqual(results.matches, [])
        self.assertEqual(results.total_matches, 3)

    def test_duplicate_paths_and_equal_scores_are_stable(self):
        duplicate = entry("README.md")
        results = search_files([duplicate, duplicate], "read", limit=2)
        self.assertEqual([hit.entry for hit in results.matches], [duplicate, duplicate])


if __name__ == "__main__":
    unittest.main()
