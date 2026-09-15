"""Check the source references in docs/.

The docs name files, and the symbols inside them. They used to pin a line
number as well, and a line number is wrong the moment anything above it
moves, which is most commits: keeping ~2600 of them honest meant a rewrite
sweep on any branch that touched code, and the sweep itself went wrong more
than once. The numbers are gone. A file path and a symbol name are what a
reader greps for, and neither moves when a function grows.

What is checked is what a RENAME breaks, which is the only way these can now
rot:

  1. Every file a doc names in backticks resolves to one on disk.
  2. Every ``file.md#anchor`` resolves to a heading in that file.

Both fail the run, because both are provably wrong rather than a judgement
call. A third thing is reported and never gated: a backticked symbol named
beside a file, which is defined nowhere in the tree. Some of those are
renames the prose did not follow; most are prose words, Home Assistant's own
names and service ids, and no rule separates them. Gating a count of those
would put the docs back to needing an edit whenever they grow, which is what
this replaced.

Usage:  doc_ref_check.py [--verbose]
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

# Where a doc's path token may live. The docs write provider refs bare
# ("mega.py", not "providers/mega.py"), which is why the list is not just "".
BASES = (
    "",
    "custom_components/be_electricity_prices/",
    "custom_components/be_electricity_prices/providers/",
    "custom_components/be_electricity_prices/translations/",
    "scripts/",
    "tests/",
    ".github/workflows/",
)
SOURCE_EXT = "py|yml|yaml|json|sh|toml|cfg|txt|md"
FILE_REF = re.compile(rf"`([A-Za-z0-9_./-]+\.(?:{SOURCE_EXT}))`")
ANCHOR_REF = re.compile(r"\b([A-Za-z0-9_./-]+\.md)#([a-z0-9-]+)")
IDENT = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$")

# Names that are not files in this repository and never will be: what a
# workflow WRITES (into the card archive, or as a job artefact), one
# placeholder the framework doc spells out, and one of Home Assistant's own
# modules. A doc naming these is correct; resolving them is not the point.
NOT_OURS = frozenset(
    {
        "catalog_report.md",
        "coverage.md",
        "drift_report.md",
        "extractor_failures.txt",
        "parser.txt",
        "pdfs.json",
        "pdfs.md",
        "persistent_failures.txt",
        "providers/foo.py",
        "report.md",
        "sensor/recorder.py",
        "unparsed.json",
    }
)
# Words in backticks that look like identifiers and are not symbols of this
# tree: literals, and the two builtins the docs contrast types with.
LITERALS = frozenset(
    {
        "True",
        "False",
        "None",
        "int",
        "str",
        "float",
        "bool",
        "dict",
        "list",
        "set",
        "tuple",
        "bytes",
        "true",
        "false",
        "null",
    }
)


def resolve(rel: str) -> Path | None:
    """The file a doc's path token names, or None."""
    for base in BASES:
        candidate = ROOT / f"{base}{rel}"
        if candidate.is_file():
            return candidate
    return None


def anchors_of(path: Path) -> set[str]:
    """Every heading in a markdown file, as GitHub slugs it."""
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        found = HEADING.match(line)
        if not found:
            continue
        text = re.sub(r"[^\w\s-]", "", found.group(1).replace("`", ""))
        out.add(re.sub(r"\s+", "-", text.strip()).lower())
    return out


def symbols_of(path: Path) -> set[str]:
    """Every name a module defines: functions, classes and module-level
    assignments. Enough to tell a symbol that still exists from one that was
    renamed."""
    if path.suffix != ".py":
        return set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, ValueError):
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            out.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            out.add(node.id)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
    return out


def main() -> int:
    verbose = "--verbose" in sys.argv
    anchor_cache: dict[Path, set[str]] = {}
    symbol_cache: dict[Path, set[str]] = {}
    everywhere: set[str] = set()
    for base in BASES[1:]:
        folder = ROOT / base
        if folder.is_dir():
            for source in folder.glob("*.py"):
                everywhere |= symbol_cache.setdefault(source, symbols_of(source))

    files_seen = anchors_seen = 0
    missing_files: list[str] = []
    missing_anchors: list[str] = []
    unknown_symbols: list[str] = []

    for doc in sorted(DOCS.rglob("*.md")):
        for number, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            named: set[str] = set()
            for rel in FILE_REF.findall(line):
                if rel in NOT_OURS:
                    continue
                files_seen += 1
                target = resolve(rel)
                if target is None:
                    missing_files.append(f"{doc.name}:{number} `{rel}`")
                    continue
                named |= symbol_cache.setdefault(target, symbols_of(target))
            for rel, anchor in ANCHOR_REF.findall(line):
                target = resolve(rel)
                if target is None:
                    continue
                anchors_seen += 1
                if anchor not in anchor_cache.setdefault(target, anchors_of(target)):
                    missing_anchors.append(f"{doc.name}:{number} {rel}#{anchor}")
            if not named:
                continue
            for ident in IDENT.findall(line):
                if ident in LITERALS or ident in named or ident in everywhere:
                    continue
                unknown_symbols.append(f"{doc.name}:{number} `{ident}`")

    print(f"file references : {files_seen}")
    print(f"markdown anchors: {anchors_seen}")
    print(f"names beside a file that this tree does not define: {len(unknown_symbols)}")
    if verbose:
        for line in unknown_symbols:
            print(f"    {line}")
    for line in missing_files:
        print(f"    MISSING FILE   {line}")
    for line in missing_anchors:
        print(f"    MISSING ANCHOR {line}")
    if missing_files or missing_anchors:
        print(
            f"\nFAIL: {len(missing_files)} file reference(s) and "
            f"{len(missing_anchors)} anchor(s) name something that is not there. "
            "A file was renamed or removed and the prose did not follow; fix the "
            "name, or add it to NOT_OURS if it is something a workflow writes."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
