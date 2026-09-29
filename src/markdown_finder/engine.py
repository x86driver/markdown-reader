"""Filesystem discovery and fuzzy path search, independent of the Qt UI.

Scanning includes hidden directories and ignores no directory names or gitignore
rules. Directory symlinks are never followed, preventing cycles and accidental
traversal of a second directory tree. Symlinks to existing regular files are
included under the link's name, even when the target is outside the scan root;
broken links and links to other types are skipped. Discovery inspects metadata,
not file contents, so readability is checked when a caller opens a result.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import heapq
import os
from pathlib import Path
import time
import unicodedata

from rapidfuzz import fuzz


def normalize(text: str) -> str:
    """Make case and compatibility characters consistent without losing CJK."""
    return unicodedata.normalize("NFKC", text).casefold()


@dataclass(frozen=True, slots=True)
class FileEntry:
    path: str
    name: str
    relative_path: str
    normalized_name: str = field(init=False, repr=False)
    normalized_path: str = field(init=False, repr=False)
    normalized_stem: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "normalized_name", normalize(self.name))
        object.__setattr__(self, "normalized_path", normalize(self.relative_path))
        object.__setattr__(self, "normalized_stem", normalize(self.name[:-3]))


@dataclass(frozen=True, slots=True)
class ScanStats:
    directories: int = 0
    files_seen: int = 0
    markdown_files: int = 0
    inaccessible: int = 0
    skipped_symlinks: int = 0
    cancelled: bool = False
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchHit:
    entry: FileEntry
    score: float


@dataclass(frozen=True, slots=True)
class SearchResults:
    matches: list[SearchHit]
    total_matches: int
    cancelled: bool = False


def scan_markdown(
    root: Path,
    *,
    cancelled: Callable[[], bool],
    on_batch: Callable[[list[FileEntry]], None],
    on_progress: Callable[[ScanStats], None],
) -> ScanStats:
    """Stream .md files under root; callbacks execute in the calling thread.

    Batches contain at most 256 entries and are flushed approximately every
    100 ms while scanning. Progress uses the same interval plus initial/final
    snapshots. Files and folders that disappear or cannot be inspected count
    as inaccessible; the first 20 error descriptions are retained. files_seen
    counts regular files, including links to regular files. Callers should run
    this function in their own worker thread and marshal callbacks to the UI.
    """
    root = root.expanduser().absolute()
    directories = files_seen = markdown_files = inaccessible = skipped_symlinks = 0
    errors: list[str] = []
    pending: list[FileEntry] = []
    stack = [root]
    was_cancelled = False
    last_progress = last_batch = time.monotonic()

    def snapshot() -> ScanStats:
        return ScanStats(
            directories=directories,
            files_seen=files_seen,
            markdown_files=markdown_files,
            inaccessible=inaccessible,
            skipped_symlinks=skipped_symlinks,
            cancelled=was_cancelled,
            errors=tuple(errors),
        )

    def flush(now: float, *, force: bool = False) -> None:
        nonlocal pending, last_batch, last_progress
        if pending and (force or len(pending) >= 256 or now - last_batch >= 0.1):
            batch, pending = pending, []
            last_batch = now
            on_batch(batch)
        if force or now - last_progress >= 0.1:
            last_progress = now
            on_progress(snapshot())

    def record_error(path: str | Path, error: OSError) -> None:
        nonlocal inaccessible
        inaccessible += 1
        if len(errors) < 20:
            errors.append(f"{path}: {error.strerror or str(error)}")

    on_progress(snapshot())
    while stack:
        if cancelled():
            was_cancelled = True
            break
        directory = stack.pop()
        try:
            with os.scandir(directory) as children:
                directories += 1
                for child in children:
                    if cancelled():
                        was_cancelled = True
                        break
                    try:
                        symlink = child.is_symlink()
                        if child.is_dir(follow_symlinks=symlink):
                            if symlink:
                                skipped_symlinks += 1
                            else:
                                stack.append(Path(child.path))
                        elif child.is_file(follow_symlinks=True):
                            files_seen += 1
                            if child.name.casefold().endswith(".md"):
                                markdown_files += 1
                                pending.append(
                                    FileEntry(
                                        path=child.path,
                                        name=child.name,
                                        relative_path=os.path.relpath(child.path, root),
                                    )
                                )
                        elif symlink:
                            skipped_symlinks += 1
                    except OSError as error:
                        record_error(child.path, error)
                    flush(time.monotonic())
        except OSError as error:
            record_error(directory, error)
        if was_cancelled:
            break
        flush(time.monotonic())

    flush(time.monotonic(), force=True)
    return snapshot()


def _is_subsequence(needle: str, haystack: str) -> bool:
    """Require every character in order; similarity alone is not a match."""
    start = 0
    for character in needle:
        index = haystack.find(character, start)
        if index < 0:
            return False
        start = index + 1
    return True


def _token_score(token: str, entry: FileEntry) -> float | None:
    name, path = entry.normalized_name, entry.normalized_path
    if token in name:
        score = 90.0 + 0.15 * fuzz.ratio(token, name)
        if name.startswith(token):
            score += 15.0
        if token == entry.normalized_stem or token == name:
            score += 25.0
        return score
    if _is_subsequence(token, name):
        return 55.0 + 0.3 * fuzz.ratio(token, name) + 0.05 * fuzz.partial_ratio(token, name)
    if token in path:
        return 45.0 + 0.1 * fuzz.ratio(token, path)
    if _is_subsequence(token, path):
        return 10.0 + 0.25 * fuzz.ratio(token, path) + 0.1 * fuzz.partial_ratio(token, path)
    return None


def search_files(
    entries: Sequence[FileEntry],
    query: str,
    *,
    limit: int = 100,
    cancelled: Callable[[], bool] = lambda: False,
    recent: Sequence[str] = (),
) -> SearchResults:
    """Rank paths using AND-ed whitespace tokens and RapidFuzz scoring.

    Every token must be an ordered subsequence of the basename or relative
    path. Contiguous basename and basename-prefix matches receive the largest
    bonuses; matching only a directory ranks lower. Recency gives a small
    bonus. Empty queries show recent files first, then deterministic name/path
    order. Only the best ``limit`` matches are retained, but total_matches
    counts every eligible entry. Cancellation returns no potentially stale
    hits and the number matched before cancellation. No files are opened.
    """
    if limit < 0:
        raise ValueError("limit must be nonnegative")
    normalized_query = normalize(query).strip()
    tokens = tuple(dict.fromkeys(normalized_query.split()))
    recent_positions: dict[str, int] = {}
    for index, path in enumerate(recent):
        recent_positions.setdefault(path, index)
    total_matches = 0
    was_cancelled = False

    def ranked_candidates():
        nonlocal total_matches, was_cancelled
        for index, entry in enumerate(entries):
            if cancelled():
                was_cancelled = True
                return
            recent_index = recent_positions.get(entry.path)
            if not tokens:
                # Any recent entry precedes every non-recent entry.
                score = 1.0 / (recent_index + 1) if recent_index is not None else 0.0
            else:
                total_score = 0.0
                for token in tokens:
                    token_score = _token_score(token, entry)
                    if token_score is None:
                        break
                    total_score += token_score
                else:
                    score = total_score / len(tokens)
                    if normalized_query in entry.normalized_name:
                        score += 12.0
                    if recent_index is not None:
                        score += 6.0 / (1.0 + recent_index * 0.25)
                    total_matches += 1
                    yield (-score, entry.normalized_name, entry.normalized_path, entry.path, index)
                    continue
                continue
            total_matches += 1
            yield (-score, entry.normalized_name, entry.normalized_path, entry.path, index)

    if limit:
        ranked = heapq.nsmallest(limit, ranked_candidates())
    else:
        for _ in ranked_candidates():
            pass
        ranked = []
    # The last candidate may have been processed just as the request changed.
    was_cancelled = was_cancelled or cancelled()
    if was_cancelled:
        return SearchResults([], total_matches, True)
    return SearchResults(
        matches=[SearchHit(entries[item[-1]], -item[0]) for item in ranked],
        total_matches=total_matches,
    )
