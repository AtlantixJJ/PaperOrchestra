#!/usr/bin/env python3
"""
merge_reference_databases.py — Merge two PaperOrchestra reference databases.

Each reference database has the layout:
    <db_root>/index.json
    <db_root>/summaries/<bibtex_key>.md
    <db_root>/papers/<bibtex_key>.pdf       (optional)
    <db_root>/prompts/<bibtex_key>.txt      (optional)

The script:
  1. Reads both index.json files.
  2. Copies all summary .md, PDF .pdf, and prompt .txt files into the output
     database directory, rewriting the ``location`` field in index entries to
     reflect the new paths relative to ``--output-workspace``.
  3. Deduplicates by ``bibtex_key``.  When both databases contain the same key
     the entry with the "better" status is kept (complete > summary_ok >
     summary_missing), and the richer summary text wins ties.  Pass
     ``--on-conflict=first`` or ``--on-conflict=second`` to always prefer one
     source.
  4. Writes a merged index.json to <output>/reference_database/index.json.
  5. Prints a short report of totals and conflicts.

Usage:
    python merge_reference_databases.py \\
        --db1 CVPR2026/reference_database \\
        --db2 paper_orchestra_workspaces/liteavatar_siggraph2026/reference_database \\
        --output merged_workspace

    # keep all files inside existing workspace; write index only
    python merge_reference_databases.py \\
        --db1 path/to/first/reference_database \\
        --db2 path/to/second/reference_database \\
        --output-workspace merged_workspace \\
        --on-conflict=best

The output workspace layout:
    merged_workspace/
      reference_database/
        index.json
        summaries/
        papers/
        prompts/          (if any source has prompts)
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Status ranking helpers
# ---------------------------------------------------------------------------

_STATUS_RANK: dict[str, int] = {
    "complete": 100,
    "summary_ok": 80,
    "summary_missing": 20,
}


def _status_score(status: str) -> int:
    """Return a numeric score for an index entry status string.

    The status field is semicolon-separated tokens such as
    ``summary_ok;pdf_ok`` or ``complete;existing``.
    """
    score = 0
    for token in status.split(";"):
        token = token.strip()
        score = max(score, _STATUS_RANK.get(token, 0))
    # Bonus for having a PDF
    if "pdf_ok" in status:
        score += 5
    return score


# ---------------------------------------------------------------------------
# Index loading
# ---------------------------------------------------------------------------

def load_index(db_root: Path) -> list[dict]:
    index_path = db_root / "index.json"
    if not index_path.exists():
        print(f"WARN: index not found: {index_path}", file=sys.stderr)
        return []
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ERROR: cannot parse {index_path}: {exc}", file=sys.stderr)
        return []
    return data.get("papers", [])


# ---------------------------------------------------------------------------
# File resolution helpers
# ---------------------------------------------------------------------------

def _resolve_asset(db_root: Path, bibtex_key: str, entry_location: str) -> Path | None:
    """Return the absolute path of the summary .md for *entry_location*.

    The location field is stored relative to the *workspace* which is the
    parent of the reference_database directory, i.e. db_root.parent.
    We try both interpretations so the script works with either layout.
    """
    candidates = [
        db_root.parent / entry_location,          # location relative to workspace
        db_root / "summaries" / f"{bibtex_key}.md",  # direct lookup
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _find_pdf(db_root: Path, bibtex_key: str) -> Path | None:
    p = db_root / "papers" / f"{bibtex_key}.pdf"
    return p if p.exists() else None


def _find_prompt(db_root: Path, bibtex_key: str) -> Path | None:
    for name in (f"{bibtex_key}.txt", f"{bibtex_key}.prompt.txt"):
        p = db_root / "prompts" / name
        if p.exists():
            return p
    return None


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------

ConflictStrategy = str  # "best" | "first" | "second"


def _pick_winner(
    key: str,
    entry_a: dict,
    db_a: Path,
    entry_b: dict,
    db_b: Path,
    strategy: ConflictStrategy,
) -> tuple[dict, Path]:
    """Return (winning_entry, winning_db_root) for a duplicate bibtex_key."""
    if strategy == "first":
        return entry_a, db_a
    if strategy == "second":
        return entry_b, db_b

    # strategy == "best": compare status scores, break ties by summary length
    score_a = _status_score(entry_a.get("status", ""))
    score_b = _status_score(entry_b.get("status", ""))

    if score_a > score_b:
        return entry_a, db_a
    if score_b > score_a:
        return entry_b, db_b

    # Equal scores: prefer longer summary text
    len_a = len(entry_a.get("summary", ""))
    len_b = len(entry_b.get("summary", ""))
    return (entry_a, db_a) if len_a >= len_b else (entry_b, db_b)


def merge_databases(
    db_a: Path,
    db_b: Path,
    out_workspace: Path,
    strategy: ConflictStrategy = "best",
    dry_run: bool = False,
) -> int:
    """Merge *db_a* and *db_b* into *out_workspace*/reference_database/.

    Returns the number of warnings encountered.
    """
    out_db = out_workspace / "reference_database"
    out_summaries = out_db / "summaries"
    out_papers = out_db / "papers"
    out_prompts = out_db / "prompts"

    if not dry_run:
        out_summaries.mkdir(parents=True, exist_ok=True)
        out_papers.mkdir(parents=True, exist_ok=True)

    entries_a = load_index(db_a)
    entries_b = load_index(db_b)

    # Build lookup: bibtex_key → (entry, db_root)
    merged: dict[str, tuple[dict, Path]] = {}
    conflicts: list[str] = []
    warnings: int = 0

    def _add(entries: list[dict], db_root: Path, label: str) -> None:
        nonlocal warnings
        for entry in entries:
            key = entry.get("bibtex_key", "").strip()
            if not key:
                print(f"WARN [{label}]: entry missing bibtex_key, skipping: {entry}", file=sys.stderr)
                warnings += 1
                continue
            if key in merged:
                existing_entry, existing_db = merged[key]
                winner, winner_db = _pick_winner(key, existing_entry, existing_db, entry, db_root, strategy)
                if winner is not existing_entry:
                    merged[key] = (winner, winner_db)
                conflicts.append(
                    f"  {key}: kept from {'db1' if winner_db == db_a else 'db2'} "
                    f"(scores: db1={_status_score(existing_entry.get('status',''))}, "
                    f"db2={_status_score(entry.get('status',''))})"
                )
            else:
                merged[key] = (entry, db_root)

    _add(entries_a, db_a, "db1")
    _add(entries_b, db_b, "db2")

    # Sort merged entries alphabetically by bibtex_key for determinism
    sorted_keys = sorted(merged.keys())

    out_rows: list[dict] = []
    copied_summaries = 0
    copied_pdfs = 0
    copied_prompts = 0
    missing_summaries: list[str] = []

    for key in sorted_keys:
        entry, src_db = merged[key]

        # --- summary .md ---
        src_summary = _resolve_asset(src_db, key, entry.get("location", ""))
        if src_summary is None:
            missing_summaries.append(key)
            warnings += 1
            rel_summary = ""
        else:
            dest_summary = out_summaries / f"{key}.md"
            if not dry_run and src_summary != dest_summary:
                shutil.copy2(src_summary, dest_summary)
            copied_summaries += 1
            rel_summary = str(Path("reference_database") / "summaries" / f"{key}.md")

        # --- PDF ---
        src_pdf = _find_pdf(src_db, key)
        if src_pdf:
            dest_pdf = out_papers / f"{key}.pdf"
            if not dry_run and src_pdf != dest_pdf:
                shutil.copy2(src_pdf, dest_pdf)
            copied_pdfs += 1

        # --- prompt ---
        src_prompt = _find_prompt(src_db, key)
        if src_prompt:
            if not dry_run:
                out_prompts.mkdir(parents=True, exist_ok=True)
            dest_prompt = out_prompts / f"{key}.txt"
            if not dry_run and src_prompt != dest_prompt:
                shutil.copy2(src_prompt, dest_prompt)
            copied_prompts += 1

        out_rows.append({
            "bibtex_key": key,
            "title": entry.get("title", ""),
            "location": rel_summary,
            "year": str(entry.get("year", "")),
            "summary": entry.get("summary", ""),
            "status": entry.get("status", ""),
        })

    # Write merged index
    if not dry_run:
        index_out = out_db / "index.json"
        index_out.write_text(
            json.dumps({"papers": out_rows}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote {len(out_rows)} entries → {index_out}")
    else:
        print(f"[dry-run] Would write {len(out_rows)} entries → {out_db / 'index.json'}")

    # Report
    only_in_a = set(e.get("bibtex_key") for e in entries_a) - set(e.get("bibtex_key") for e in entries_b)
    only_in_b = set(e.get("bibtex_key") for e in entries_b) - set(e.get("bibtex_key") for e in entries_a)
    print(f"\nSummary:")
    print(f"  db1 entries : {len(entries_a)}")
    print(f"  db2 entries : {len(entries_b)}")
    print(f"  unique in db1: {len(only_in_a)}")
    print(f"  unique in db2: {len(only_in_b)}")
    print(f"  conflicts   : {len(conflicts)}")
    print(f"  merged total: {len(out_rows)}")
    print(f"  summaries copied : {copied_summaries}")
    print(f"  PDFs copied      : {copied_pdfs}")
    print(f"  prompts copied   : {copied_prompts}")
    if missing_summaries:
        print(f"\nWARN: {len(missing_summaries)} entries had no resolvable summary file:")
        for k in missing_summaries:
            print(f"  {k}")
    if conflicts:
        print(f"\nConflict resolutions ({strategy} strategy):")
        for line in conflicts:
            print(line)

    return warnings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db1",
        required=True,
        metavar="PATH",
        help="Path to the first reference_database directory (contains index.json, summaries/, …).",
    )
    parser.add_argument(
        "--db2",
        required=True,
        metavar="PATH",
        help="Path to the second reference_database directory.",
    )
    parser.add_argument(
        "--output-workspace",
        "--output",
        required=True,
        metavar="PATH",
        help=(
            "Output workspace directory. The merged reference_database/ "
            "will be created inside it."
        ),
    )
    parser.add_argument(
        "--on-conflict",
        default="best",
        choices=["best", "first", "second"],
        help=(
            "How to resolve duplicate bibtex_keys. "
            "'best' keeps the entry with the richer status/summary (default). "
            "'first' always keeps the db1 version. "
            "'second' always keeps the db2 version."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be done without copying any files or writing index.json.",
    )
    args = parser.parse_args()

    db1 = Path(args.db1).resolve()
    db2 = Path(args.db2).resolve()
    out_ws = Path(args.output_workspace).resolve()

    for p, label in [(db1, "--db1"), (db2, "--db2")]:
        if not p.exists():
            print(f"ERROR: {label} path does not exist: {p}", file=sys.stderr)
            return 2
        if not (p / "index.json").exists():
            print(
                f"WARN: {label} has no index.json: {p / 'index.json'} — "
                "will proceed with zero entries from this source.",
                file=sys.stderr,
            )

    warnings = merge_databases(
        db_a=db1,
        db_b=db2,
        out_workspace=out_ws,
        strategy=args.on_conflict,
        dry_run=args.dry_run,
    )
    return 0 if warnings == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
