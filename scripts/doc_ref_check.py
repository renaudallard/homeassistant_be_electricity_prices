"""Check, and optionally re-derive, the ``file.py:line`` pins in docs/.

The docs pin roughly 760 source references. They rot whenever a module grows,
and a stale pin is worse than none: it sends a reader to a line that now says
something else entirely.

Resolution is by CONTENT, never offset arithmetic. For each reference the doc
almost always names the symbol it points at, in backticks, on the same line
(``_extract_energy`` (`_mega_cards.py:137`)); this resolves that symbol's real
definition line and, with ``--write``, rewrites the number when it moved.

Five reference forms exist and all five are checked, because for a long time
only the first was and the other four rotted in silence:

  1. ``file.py:12``          plain
  2. ``file.py:12-34``       a range
  3. ``(`file.py:12`, `:34`)`` a continuation, inheriting the last filename
  4. a symbol that MOVED to another module, whose old line still happens to
     land inside the now-shorter file, so nothing looks broken
  5. ``README.md:214``       a line in a MARKDOWN file, which has no symbols
     to anchor on, so it is scored on shared distinctive words instead

CI runs this without ``--write`` and fails on references that are PROVABLY
broken (past the end of the file they name, or landing on a dead line) and on
any INCREASE in the rewritable or unanchored counts over the baselines below.
Everything else is reported for a human, never auto-corrected, because the same
shapes have legitimate forms. A ref pointing at a USE site rather than a
definition is correct and common, and the anchor heuristic cannot tell it from
a stale one.

Usage:  doc_ref_check.py [--write] [--verbose]
"""

from __future__ import annotations

import ast
import re
import sys
from collections import Counter
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
# Backticked words that are prose, not symbols, and so cannot anchor a range.
_RANGE_LITERALS = frozenset(
    {"True", "False", "None", "int", "str", "float", "bool", "dict", "list"}
)
# Pins into a MARKDOWN file ("README.md:214"), which none of the three
# regexes above can see: they all require a .py name inside backticks. The
# glossary pins README passages this way, and 12 of its 15 pins had rotted --
# some by over 100 lines, onto a fragment like "> month." or "overwritten." --
# while every run printed a clean sweep. A markdown line has no symbol to
# anchor on, so these are scored on how many DISTINCTIVE words the doc's claim
# shares with the passage the pin lands in.
MDREF = re.compile(r"([A-Za-z0-9_./-]+\.md):(\d+)(?:-(\d+))?")
# Any file:line pin, stripped before scoring so a shared "README" or "py" can
# never stand in for a shared word of the claim itself.
FILEREF = re.compile(r"[A-Za-z0-9_./-]+\.(?:py|md):\d+(?:-\d+)?")
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Lines that OPEN a markdown block. A pin's passage stops at them, so an
# anchor from a neighbouring bullet is never credited to this pin.
MD_BLOCK = re.compile(r"\s*(?:[-*+]\s|\d+\.\s|#{1,6}\s|\||```|>)")
# A pin landing here points at nothing at all: a blank line, a bare fence, or
# a table rule. Provably useless, so it fails rather than being reported.
MD_DEAD = re.compile(r"\s*(?:```|\|[\s|:-]*\|)?\s*$")
# A word counts as distinctive when it appears on at most this share of the
# target file's lines: "the" and "energy" are on hundreds of README lines and
# say nothing about where a pin belongs, "energiefonds" and "picker" do.
_MD_COMMON_LINE_RATIO = 0.03
# How many distinctive words the claim and the pinned passage must share.
# Measured over the 15 README pins as they stood before the 2026-08-17 repin:
# all 12 stale pins scored 3 or less (six scored 0) and the three that still
# resolved scored 4, 7 and 14. After the repin the spread is 3..17.
_MD_MIN_SHARED = 3
# Unanchored markdown pins tolerated on a green run, same ratchet as the
# rewritable baseline above. The one tolerated pin is the glossary's TSO row:
# it defines Elia and the transmission charge, which README never states, so
# the closest honest target is the `network_component` sensor row and that
# shares only "distribution" and "transport". Raise this only for a pin with
# no better target in the file, and say which in the commit message.
_MD_UNANCHORED_BASELINE = 1
# Range pins whose span holds none of the identifiers its sentence names. Not
# all of these are wrong: a pin may deliberately cross files (the prose names a
# function in one module while pointing the reader at the constants it reads in
# another), or target a sub-region of a block. Those legitimately trip the
# test, so this is a ratchet like the two above rather than a zero. Until the
# 2026-08 sweep the only thing checked about a range was that neither end ran
# past EOF, so a span could sit on entirely unrelated code and pass: five of
# the eight const.py ranges did exactly that.
_RANGE_UNANCHORED_BASELINE = 32


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


def _symbol_blocks(path: Path) -> dict[str, list[tuple[int, int]]]:
    """Every named definition -> the line spans it occupies.

    ``_symbol_lines`` gives only the first line, which cannot answer the
    question a range pin poses: is this span INSIDE the thing the sentence
    names? A pin at the fields of a dataclass holds the class name nowhere in
    its own text, so a substring search reports a false alarm.

    ``ast.alias`` carries a ``.name``, so an unfiltered walk indexes every
    IMPORTED symbol as though this module defined it, and a pin then "resolves"
    onto an import line. Hence the explicit skip.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return {}
    out: dict[str, list[tuple[int, int]]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.alias, ast.Import, ast.ImportFrom)):
            continue
        name = getattr(node, "name", None)
        if name is None and isinstance(node, ast.Assign):
            tgt = node.targets[0] if node.targets else None
            name = getattr(tgt, "id", None)
        if name is None and isinstance(node, ast.AnnAssign):
            name = getattr(node.target, "id", None)
        if name and hasattr(node, "lineno"):
            end = getattr(node, "end_lineno", None) or node.lineno
            out.setdefault(name, []).append((node.lineno, end))
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


def _md_words(text: str) -> set[str]:
    """Lowercased word set of a claim or a passage, pins stripped out."""
    return {w.lower() for w in WORD.findall(FILEREF.sub(" ", text))}


def _md_index(path: Path) -> tuple[list[str], Counter[str]]:
    """A markdown file's lines, plus how many lines each word appears on."""
    lines = path.read_text(encoding="utf-8").splitlines()
    freq: Counter[str] = Counter()
    for line in lines:
        freq.update(_md_words(line))
    return lines, freq


def _md_passage(lines: list[str], num: int) -> tuple[int, int]:
    """The paragraph or list item the pinned line belongs to.

    README prose is hard-wrapped, so the claim a pin aims at rarely fits on
    the pinned line alone: the injection-regime pin lands on the bullet's
    first line and names ``injection_price`` on its second. Widening to the
    surrounding block picks that up, while stopping at a blank line and at
    the head of the next block keeps a bullet from borrowing its neighbour's
    words -- and a table row, which carries its whole claim, stays one line.
    """
    start = end = num
    while (
        start > 1 and lines[start - 2].strip() and not MD_BLOCK.match(lines[start - 1])
    ):
        start -= 1
    while end < len(lines) and lines[end].strip() and not MD_BLOCK.match(lines[end]):
        end += 1
    return start, end


def _md_claim(lines: list[str], i: int) -> str:
    """The doc text whose claim the pin on line ``i`` (1-based) supports.

    A table row carries its whole claim. Running prose does not: the glossary
    states the all-in formula on one line and pins it on the next, so the
    line above comes along.
    """
    line = lines[i - 1]
    if line.lstrip().startswith("|"):
        return line
    return " ".join(lines[max(0, i - 2) : i])


def main() -> int:
    write = "--write" in sys.argv
    verbose = "--verbose" in sys.argv
    cache: dict[Path, dict[str, int]] = {}
    md_cache: dict[Path, tuple[list[str], Counter[str]]] = {}
    fixed = stale_unresolved = ok = md_ok = 0
    unresolved: list[str] = []
    stale_ranges: list[str] = []
    range_unanchored: list[str] = []
    suspect: list[str] = []
    samples: list[str] = []
    md_dead: list[str] = []
    md_unanchored: list[str] = []

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
                body = src.read_text(encoding="utf-8").splitlines()
                total = len(body)
                lo, hi = int(r.group(2)), int(r.group(3))
                if lo > total or hi > total:
                    stale_ranges.append(
                        f"{doc.name}:{i + 1} {r.group(0).strip('`')} (file has {total})"
                    )
                    continue
                if hi < lo:
                    # A rewrite pass that matches the START of a range shifts
                    # it and leaves the end behind, which no check saw: one
                    # such pin read 244-236 for a week.
                    stale_ranges.append(
                        f"{doc.name}:{i + 1} {r.group(0).strip('`')} (inverted)"
                    )
                    continue
                # Does the span hold anything the sentence names? Every
                # backticked identifier on the line is a candidate, because
                # taking only the nearest one mistakes prose (`None`, a field
                # named descriptively) for the symbol the pin is about.
                range_names = {
                    ident.split(".")[-1]
                    for ident in IDENT.findall(line)
                    if not ident.endswith(".py")
                    and ident not in _RANGE_LITERALS
                    and len(ident) > 2
                }
                if not range_names:
                    continue
                blocks = _symbol_blocks(src)
                span = "\n".join(body[lo - 1 : hi])
                inside = any(
                    b_lo <= lo and hi <= b_hi
                    for c in range_names
                    for b_lo, b_hi in blocks.get(c, ())
                )
                if inside:
                    continue
                if not any(
                    re.search(rf"\b{re.escape(c)}\b", span) for c in range_names
                ):
                    range_unanchored.append(
                        f"{doc.name}:{i + 1} {r.group(0).strip('`')} -> holds none of "
                        f"{sorted(range_names)[:4]}"
                    )
            for md in MDREF.finditer(line):
                target = _source_for(md.group(1))
                if target is None:
                    continue
                if target not in md_cache:
                    md_cache[target] = _md_index(target)
                md_lines, md_freq = md_cache[target]
                nums = [int(v) for v in (md.group(2), md.group(3)) if v]
                if any(n > len(md_lines) for n in nums):
                    md_dead.append(
                        f"{doc.name}:{i + 1} {md.group(0)} "
                        f"(file has {len(md_lines)} lines)"
                    )
                    continue
                num = nums[0]
                if MD_DEAD.match(md_lines[num - 1]):
                    md_dead.append(
                        f"{doc.name}:{i + 1} {md.group(0)} lands on a blank "
                        f"line, a bare fence or a table rule"
                    )
                    continue
                lo, hi = _md_passage(md_lines, num)
                cap = max(5, int(len(md_lines) * _MD_COMMON_LINE_RATIO))
                passage = _md_words(" ".join(md_lines[lo - 1 : hi]))
                shared = sorted(
                    w
                    for w in _md_words(_md_claim(lines, i + 1)) & passage
                    if md_freq[w] <= cap
                )
                if len(shared) >= _MD_MIN_SHARED:
                    md_ok += 1
                    continue
                md_unanchored.append(
                    f"{doc.name}:{i + 1} {md.group(0)} -> passage {lo}-{hi} "
                    f"shares only {shared or 'nothing'}"
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
    print(
        f"range pins unanchored: {len(range_unanchored)} "
        f"(baseline {_RANGE_UNANCHORED_BASELINE})"
    )
    for entry in range_unanchored:
        print(f"    {entry}")
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
    print(f"markdown pins ok   : {md_ok}")
    print(f"markdown BROKEN    : {len(md_dead)}")
    for d in md_dead:
        print("   ", d)
    print(
        f"markdown unanchored: {len(md_unanchored)} "
        f"(baseline {_MD_UNANCHORED_BASELINE})"
    )
    for u in md_unanchored:
        print("   ", u)

    # Fail on references that are provably broken: they name a line past the
    # end of the file. The suspects list stays a report, being a judgement call
    # by construction; failing on it would train people to ignore it.
    broken = stale_unresolved + len(stale_ranges) + len(md_dead)
    if broken and not write:
        print(
            f"\nFAIL: {broken} reference(s) point past the end of the file they "
            f"name, or at a line holding nothing. Re-derive them by content: "
            f"find what the prose describes in the source and repin, never by "
            f"adding an offset."
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
    # Same ratchet for the markdown pins. A markdown target has no symbol to
    # resolve, so nothing here is auto-rewritten and no single miss proves rot;
    # a RISE does, because a section inserted into README shifts every pin
    # below it and most land on prose about something else.
    if len(range_unanchored) > _RANGE_UNANCHORED_BASELINE and not write:
        print(
            f"\nFAIL: {len(range_unanchored)} range pin(s) whose span holds none of "
            f"the identifiers their sentence names, baseline "
            f"{_RANGE_UNANCHORED_BASELINE}. Open the span and the sentence: a range "
            f"that landed on unrelated code is repinned to the block the sentence "
            f"actually describes, never by shifting the numbers. A pin that crosses "
            f"files on purpose, or targets a sub-region, is allowed -- raise the "
            f"baseline and say which in the commit message."
        )
        return 1
    if len(range_unanchored) < _RANGE_UNANCHORED_BASELINE and not write:
        print(
            f"\nNote: unanchored range pins ({len(range_unanchored)}) are below the "
            f"baseline ({_RANGE_UNANCHORED_BASELINE}); lower "
            f"_RANGE_UNANCHORED_BASELINE to keep the ratchet tight."
        )
    if len(md_unanchored) > _MD_UNANCHORED_BASELINE and not write:
        print(
            f"\nFAIL: {len(md_unanchored)} markdown pin(s) share fewer than "
            f"{_MD_MIN_SHARED} distinctive words with the passage they name, "
            f"baseline {_MD_UNANCHORED_BASELINE}. Re-derive each by content: "
            f"grep the target file for what the pinned sentence CLAIMS, never "
            f"by adding an offset. If a claim genuinely has no better passage "
            f"to point at, raise the baseline and say which pin in the commit "
            f"message."
        )
        return 1
    if len(md_unanchored) < _MD_UNANCHORED_BASELINE and not write:
        print(
            f"\nNote: unanchored markdown pins ({len(md_unanchored)}) are below "
            f"the baseline ({_MD_UNANCHORED_BASELINE}); lower "
            f"_MD_UNANCHORED_BASELINE to keep the ratchet tight."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
