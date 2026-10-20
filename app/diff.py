"""Unified-diff parsing and bounded, untrusted-input-safe context building."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
TEST_PATTERN = re.compile(r"(^|/)(tests?|__tests__|spec)/|\.(test|spec)\.[a-z]+$|(^|/)test_[^/]+\.py$|_test\.(py|go)$")
DOC_PATTERN = re.compile(r"\.(md|rst|txt)$|(^|/)docs?/|(^|/)LICENSE")


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[str] = field(default_factory=list)
    section: str = ""  # git's function context, e.g. "export function getOwnedAccount(...)"


@dataclass
class ChangedFile:
    path: str
    old_path: str | None
    status: str  # added | modified | deleted | renamed | binary
    hunks: list[Hunk] = field(default_factory=list)
    patch: str = ""

    @property
    def additions(self) -> int:
        return sum(line.startswith("+") for h in self.hunks for line in h.lines)

    @property
    def deletions(self) -> int:
        return sum(line.startswith("-") for h in self.hunks for line in h.lines)

    @property
    def is_test(self) -> bool:
        return bool(TEST_PATTERN.search(self.path))

    @property
    def is_doc(self) -> bool:
        return bool(DOC_PATTERN.search(self.path))


def normalize_path(path: str) -> str:
    path = path.strip().strip('"')
    for prefix in ("a/", "b/", "./"):
        if path.startswith(prefix):
            path = path[len(prefix) :]
    return path


def parse_diff(diff: str) -> list[ChangedFile]:
    files: list[ChangedFile] = []
    current: ChangedFile | None = None
    hunk: Hunk | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            new_path = normalize_path(parts[1]) if len(parts) == 2 else ""
            old_path = normalize_path(parts[0][len("diff --git ") :])
            current = ChangedFile(path=new_path, old_path=old_path, status="modified")
            files.append(current)
            hunk = None
        elif current is None:
            continue
        elif line.startswith("new file mode"):
            current.status = "added"
        elif line.startswith("deleted file mode"):
            current.status = "deleted"
        elif line.startswith("rename from "):
            current.status = "renamed"
        elif line.startswith("Binary files"):
            current.status = "binary"
        elif line.startswith("--- ") and hunk is None:
            pass
        elif line.startswith("+++ ") and hunk is None:
            target = line[4:].strip()
            if target != "/dev/null":
                current.path = normalize_path(target)
        elif match := _HUNK.match(line):
            a, b, c, d, section = match.groups()
            hunk = Hunk(int(a), int(b or 1), int(c), int(d or 1), section=section.strip()[:200])
            current.hunks.append(hunk)
        elif hunk is not None and (line == "" or line[:1] in ("+", "-", " ", "\\")):
            hunk.lines.append(line)
        if current is not None:
            current.patch += line + "\n"
    if current is None and diff.strip():
        raise ValueError("Input is not a unified git diff (no 'diff --git' headers found)")
    for f in files:
        if f.status == "deleted":
            f.path = f.old_path or f.path
    return files


def hunk_line_ranges(f: ChangedFile) -> list[tuple[str, int, int]]:
    """(side, start, end) covered by each hunk, on both old and new sides."""
    ranges = []
    for h in f.hunks:
        ranges.append(("new", h.new_start, h.new_start + max(h.new_len, 1) - 1))
        ranges.append(("old", h.old_start, h.old_start + max(h.old_len, 1) - 1))
    return ranges


# ---- Context building ----

UNTRUSTED_OPEN = "<untrusted_pr_content>"
UNTRUSTED_CLOSE = "</untrusted_pr_content>"


def neutralize(text: str) -> str:
    """Stop untrusted text from closing or faking our delimiters."""
    return re.sub(r"</?\s*untrusted_pr_content\s*>", "[delimiter removed]", text, flags=re.IGNORECASE)


def render_patch(f: ChangedFile) -> str:
    """Patch annotated with old/new line numbers so models can cite real locations."""
    out = []
    for h in f.hunks:
        section = f" {h.section}" if h.section else ""
        out.append(f"@@ -{h.old_start},{h.old_len} +{h.new_start},{h.new_len} @@{section}   (columns: old new)")
        old, new = h.old_start, h.new_start
        for line in h.lines:
            marker = line[:1] or " "
            if marker == "-":
                out.append(f"{old:>5} {'':>5} {line}")
                old += 1
            elif marker == "+":
                out.append(f"{'':>5} {new:>5} {line}")
                new += 1
            elif marker == "\\":
                out.append(f"{'':>11} {line}")
            else:
                out.append(f"{old:>5} {new:>5} {line or ' '}")
                old += 1
                new += 1
    return "\n".join(out) if out else f"[no textual changes: {f.status}]"


def build_context_text(
    title: str,
    description: str,
    files: list[ChangedFile],
    context_files: dict[str, str],
    *,
    max_chars: int,
    max_file_chars: int,
    facts: list[str],
) -> tuple[str, list[str], list[str], list[str]]:
    """Render bounded context. Returns (text, included, omitted, truncated)."""
    included, omitted, truncated = [], [], []
    parts = [f"PR title: {neutralize(title[:300])}", f"PR description:\n{neutralize(description[:2000])}"]
    used = sum(map(len, parts))
    # Smaller patches first so one huge file cannot crowd out the rest.
    for f in sorted(files, key=lambda f: len(f.patch)):
        patch = render_patch(f)
        if len(patch) > max_file_chars:
            patch = patch[:max_file_chars] + "\n[... patch truncated by PR Quality Chain ...]\n"
            truncated.append(f.path)
        if used + len(patch) > max_chars:
            omitted.append(f.path)
            continue
        parts.append(f"### Changed file: {f.path} ({f.status})\n{neutralize(patch)}")
        included.append(f.path)
        used += len(patch)
    for path, content in context_files.items():
        numbered = "\n".join(f"{i:>5} | {line}" for i, line in enumerate(content.splitlines(), 1))
        block = f"### Related unchanged file (for context only): {path}\n{neutralize(numbered[:max_file_chars])}"
        if used + len(block) > max_chars:
            omitted.append(path)
            continue
        parts.append(block)
        used += len(block)

    header = "Facts computed by PR Quality Chain (trusted):\n" + "\n".join(f"- {fact}" for fact in facts)
    if omitted:
        header += f"\n- Omitted for size limits (not reviewed): {', '.join(omitted)}"
    if truncated:
        header += f"\n- Truncated for size limits: {', '.join(truncated)}"
    text = f"{header}\n\n{UNTRUSTED_OPEN}\n" + "\n\n".join(parts) + f"\n{UNTRUSTED_CLOSE}"
    return text, included, omitted, truncated
