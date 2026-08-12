"""Check, and optionally re-derive, the ``file.py:line`` pins in docs/.

The docs pin roughly 760 source references. They rot whenever a module grows,
and a stale pin is worse than none: it sends a reader to a line that now says
something else entirely.

Resolution is by CONTENT, never offset arithmetic. For each reference the doc
almost always names the symbol it points at, in backticks, on the same line
(``_extract_energy`` (`_mega_cards.py:137`)); this resolves that symbol's real
definition line and, with ``--write``, rewrites the number when it moved.

Four reference forms exist and all four are checked, because for a long time
only the first was and the other three rotted in silence:

  1. ``file.py:12``          plain
  2. ``file.py:12-34``       a range
  3. ``(`file.py:12`, `:34`)`` a continuation, inheriting the last filename
  4. a symbol that MOVED to another module, whose old line still happens to
     land inside the now-shorter file, so nothing looks broken

CI runs this without ``--write`` and fails on references that are PROVABLY
broken (past the end of the file they name) and on any INCREASE in the
rewritable count over the baseline below. Everything else is reported for a
human, never auto-corrected, because the same shapes have legitimate forms. A
ref pointing at a USE site rather than a definition is correct and common, and
the anchor heuristic cannot tell it from a stale one.

Usage:  doc_ref_check.py [--write] [--verbose]
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Rewritable references tolerated on a green run. These are pins that name a
# symbol but deliberately point at the code implementing the behaviour the
# prose describes, which the anchor heuristic reports as rewritable and a
# human must not "fix": four of the five sit on a line that does not even
# mention the symbol, so no per-reference rule can tell them from a stale pin.
# Freezing the COUNT gates the case that rule cannot: a commit that shifts
# source lines under pins that used to resolve. That leaves this number
# untouched (an ambiguous pin stays ambiguous however the file moves) while a
# batch of newly rotted pins pushes it up, which is exactly the drift that
# used to land green -- one branch shifted 98 refs and CI never noticed.
# Raise it only for a pin deliberately aimed at an implementation site, and
# say which in the commit message; lower it whenever one is resolved.
_REWRITABLE_BASELINE = 5
REF = re.compile(r"`([A-Za-z0-9_./]+\.py):(\d+)`")
# Range refs ("`coordinator.py:2653-2675`"). REF does not match these, so for
# as long as the sweep only knew about single-line refs they were never
# checked at all: 28 of them were stale, in three docs, while the sweep
# printed a clean run. They are only REPORTED, never rewritten -- the end of
# a span is not derivable from an anchor symbol, and inventing one would be
# worse than leaving the pin visible.
RANGE = re.compile(r"`([A-Za-z0-9_./]+\.py):(\d+)-(\d+)`")
# Continuation refs ("`config_flow.py:483`, `:507`"), which inherit the last
# filename named on the line. Four were stale and, like the ranges, invisible.
CONT = re.compile(r"`([A-Za-z0-9_./]+\.py):\d+(?:-\d+)?`|`:(\d+)(?:-(\d+))?`")
# Backtick-quoted identifiers on the same line, longest first so
# `_extract_energy_fund` wins over `_extract_energy`.
IDENT = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)`")


def _source_for(rel: str) -> Path | None:
    """Resolve a doc's path token to a file on disk."""
    for base in (
        "",
        "custom_components/be_electricity_prices/",
        # The docs write provider refs bare ("mega.py:123"), not
        # "providers/mega.py:123". Without this base every one of them
        # resolved to None and was skipped in silence -- so the sweep's
        # "all refs correct" only ever covered the top-level modules.
        "custom_components/be_electricity_prices/providers/",
        "scripts/",
        "tests/",
    ):
        cand = ROOT / f"{base}{rel}"
        if cand.is_file():
            return cand
    cand = ROOT / rel
    return cand if cand.is_file() else None


def _symbol_lines(path: Path) -> dict[str, int]:
    """Every def/class/assignment name -> its 1-based definition line."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return {}
    out: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.setdefault(node.name, node.lineno)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out.setdefault(t.id, node.lineno)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.setdefault(node.target.id, node.lineno)
    return out


def _package_index() -> dict[str, list[tuple[str, int]]]:
    """symbol -> [(doc-style path token, line), ...] across the whole package.

    Lets a ref survive a symbol moving between modules: the doc says
    ``mega.py:1200`` and the function now lives in ``_mega_cards.py``, so both
    halves of the ref are wrong and anchoring on the line alone cannot help.
    Only rewritten when EXACTLY one module defines the name, so an ambiguous
    move is reported rather than guessed.
    """
    out: dict[str, list[tuple[str, int]]] = {}
    roots = [
        ROOT / "custom_components" / "be_electricity_prices",
        ROOT / "scripts",
    ]
    for root in roots:
        for f in sorted(root.rglob("*.py")):
            if "__pycache__" in str(f):
                continue
            token = f.name  # the docs write provider refs bare
            for name, line in _symbol_lines(f).items():
                out.setdefault(name, []).append((token, line))
    return out


def main() -> int:
    write = "--write" in sys.argv
    verbose = "--verbose" in sys.argv
    cache: dict[Path, dict[str, int]] = {}
    fixed = stale_unresolved = ok = 0
    unresolved: list[str] = []
    stale_ranges: list[str] = []
    suspect: list[str] = []
    samples: list[str] = []

    index = _package_index()
    docs = sorted((ROOT / "docs").rglob("*.md")) + [ROOT / "README.md"]
    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        lines = text.splitlines()
        changed = False
        for i, line in enumerate(lines):
            last_file: str | None = None
            for c in CONT.finditer(line):
                if c.group(1):
                    last_file = c.group(1)
                    continue
                if last_file is None:
                    continue
                src = _source_for(last_file)
                if src is None:
                    continue
                total = len(src.read_text(encoding="utf-8").splitlines())
                for v in (c.group(2), c.group(3)):
                    if v and int(v) > total:
                        stale_ranges.append(
                            f"{doc.name}:{i + 1} {last_file} continuation "
                            f"`:{v}` (file has {total})"
                        )
            for r in RANGE.finditer(line):
                src = _source_for(r.group(1))
                if src is None:
                    continue
                total = len(src.read_text(encoding="utf-8").splitlines())
                if int(r.group(2)) > total or int(r.group(3)) > total:
                    stale_ranges.append(
                        f"{doc.name}:{i + 1} {r.group(0).strip('`')} (file has {total})"
                    )
            refs = list(REF.finditer(line))
            if not refs:
                continue
            idents = list(IDENT.finditer(line))
            # Collect edits and apply them right-to-left by SPAN. A plain
            # str.replace with count=1 always hits the leftmost occurrence, so
            # a line naming the same ref twice ("**Dynamic** (`x.py:10`): ...
            # `_fn` (`x.py:10`)") had its second copy rewritten onto the first
            # and the second left stale on every run.
            edits: list[tuple[int, int, str]] = []
            for m in refs:
                rel, num = m.group(1), int(m.group(2))
                src = _source_for(rel)
                if src is None:
                    continue
                if src not in cache:
                    cache[src] = _symbol_lines(src)
                syms = cache[src]
                # Anchor on the identifier IMMEDIATELY BEFORE the reference,
                # matching how the docs are written ("`Symbol` (`file.py:N`)").
                # Taking the longest on the line mis-anchored a class ref onto
                # one of its own field names mentioned later in the sentence.
                before = [g for g in idents if g.end() <= m.start()]
                hit = None
                for g in reversed(before):
                    if g.group(1) in syms:
                        hit = g.group(1)
                        break
                if hit is None:
                    # The anchor may name a symbol that MOVED to another
                    # module: then the filename token is stale too, and no
                    # amount of line arithmetic in this file can fix it.
                    # ONLY for a ref that is provably broken -- past the end
                    # of the file it names. Without that guard this fires on
                    # every ref that legitimately points at a USE site rather
                    # than a definition ("CONF_CONTRACT (config_flow.py:331)"
                    # is where the flow reads it, not where const defines it),
                    # and rewriting those to the definition changes what the
                    # sentence claims.
                    total = len(src.read_text(encoding="utf-8").splitlines())
                    moved = None
                    if num > total:
                        for g in reversed(before):
                            cands = index.get(g.group(1), [])
                            if len(cands) == 1 and cands[0][0] != rel.split("/")[-1]:
                                moved = (g.group(1), cands[0])
                                break
                    if moved is not None:
                        # NOT ``line``: that name holds the doc line being
                        # rewritten, and shadowing it makes the edit loop
                        # below index an int.
                        name, (token, line_no) = moved
                        edits.append((m.start(), m.end(), f"`{token}:{line_no}`"))
                        fixed += 1
                        samples.append(
                            f"{doc.name}: {name}  {rel}:{num} -> {token}:{line_no} (MOVED)"
                        )
                        continue
                    if num > total:
                        stale_unresolved += 1
                        unresolved.append(f"{doc.name}:{i + 1} {rel}:{num} (past EOF)")
                        continue
                    # Not past EOF, so nothing above fires -- but the anchor is
                    # not defined in the file the ref names, and IS defined in
                    # exactly one other module. After a split that is a moved
                    # symbol whose stale line happens to still land inside the
                    # now-shorter file, which is invisible to every check here.
                    # Report only: the same shape is also a legitimate ref to a
                    # USE site ("CONF_CONTRACT (config_flow.py:331)"), and only
                    # a human can tell those apart.
                    for g in reversed(before):
                        cands = index.get(g.group(1), [])
                        if len(cands) == 1 and cands[0][0] != rel.split("/")[-1]:
                            suspect.append(
                                f"{doc.name}:{i + 1} `{g.group(1)}` ({rel}:{num}) "
                                f"but defined only in {cands[0][0]}:{cands[0][1]}"
                            )
                            break
                    continue
                want = syms[hit]
                if want == num:
                    ok += 1
                    continue
                edits.append((m.start(), m.end(), f"`{rel}:{want}`"))
                fixed += 1
                samples.append(f"{doc.name}: {hit}  {rel}:{num} -> :{want}")
            if edits:
                new_line = line
                for start, end, repl in sorted(edits, reverse=True):
                    new_line = new_line[:start] + repl + new_line[end:]
                lines[i] = new_line
                changed = True
        if changed and write:
            doc.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"anchored+correct   : {ok}")
    print(f"anchored+rewritable: {fixed}{' (written)' if write else ' (dry run)'}")
    for x in samples:
        print("   ", x)
    print(f"BROKEN, past EOF   : {stale_unresolved}")
    for u in unresolved:
        print("   ", u)
    print(f"BROKEN ranges/conts: {len(stale_ranges)}")
    for broken_range in stale_ranges:
        print("   ", broken_range)
    # Listed only on demand. Nearly all are legitimate use-site references,
    # so dumping 60-odd lines on every CI run is how a report gets ignored.
    print(
        f"moved-symbol suspects (review by hand): {len(suspect)}"
        f"{'' if verbose else ' -- pass --verbose to list'}"
    )
    if verbose:
        for x in suspect:
            print("   ", x)

    # Fail on references that are provably broken: they name a line past the
    # end of the file. The suspects list stays a report, being a judgement call
    # by construction; failing on it would train people to ignore it.
    broken = stale_unresolved + len(stale_ranges)
    if broken and not write:
        print(
            f"\nFAIL: {broken} reference(s) point past the end of the file they "
            f"name. Re-derive them by content: find what the prose describes in "
            f"the source and repin, never by adding an offset."
        )
        return 1
    # And fail on rewritable refs above the baseline. No single one of them is
    # provably stale, but a rise means pins that used to resolve no longer do.
    if fixed > _REWRITABLE_BASELINE and not write:
        print(
            f"\nFAIL: {fixed} rewritable reference(s), baseline "
            f"{_REWRITABLE_BASELINE}: {fixed - _REWRITABLE_BASELINE} pin(s) that "
            f"used to resolve no longer do. They are listed above as "
            f"'doc: symbol file:old -> :new'. Re-derive each by content and "
            f"repin, never by adding an offset. If a new pin genuinely aims at "
            f"an implementation site rather than a definition, raise the "
            f"baseline in this script and say which pin in the commit message."
        )
        return 1
    if fixed < _REWRITABLE_BASELINE and not write:
        print(
            f"\nNote: rewritable refs ({fixed}) are below the baseline "
            f"({_REWRITABLE_BASELINE}); lower _REWRITABLE_BASELINE to keep the "
            f"ratchet tight."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
