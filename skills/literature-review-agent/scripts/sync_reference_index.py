#!/usr/bin/env python3
"""
sync_reference_index.py — Ensure reference summaries and index stay in sync.

Checks a PaperOrchestra reference database:
    workspace/citation_pool.json
    workspace/reference_database/summaries/<bibtex_key>.md
    workspace/reference_database/papers/<bibtex_key>.pdf
    workspace/reference_database/index.csv
    workspace/reference_database/index.json

With --fix, rewrites index.csv and index.json from the citation pool and
summary frontmatter. With --create-stubs, also creates placeholder Markdown
summary files for missing summaries. By default, placeholders and failed/dry-run
summaries are treated as incomplete and make the check fail; use
--allow-incomplete only for development.

Usage:
    python sync_reference_index.py --workspace workspace
    python sync_reference_index.py --workspace workspace --fix
    python sync_reference_index.py --workspace workspace --fix --create-stubs
"""
import argparse
import csv
import json
import os
import sys
from pathlib import Path


INDEX_FIELDS = [
    "bibtex_key",
    "title",
    "location",
    "year",
    "venue",
    "summary_md_path",
    "pdf_path",
    "one_word_summary",
    "status",
]


def load_pool(path: Path) -> dict:
    with path.open() as f:
        return json.load(f)


def bibtex_key(paper: dict, index: int) -> str:
    return paper.get("bibtex_key") or paper.get("paperId") or f"paper_{index:04d}"


def parse_frontmatter(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(errors="replace")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fields = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip('"')
    return fields


def one_word(value: str) -> str:
    value = (value or "other").strip().lower()
    value = "".join(ch for ch in value if ch.isalnum() or ch == "-")
    if not value or "-" in value:
        return "other"
    return value


def stub_summary(paper: dict, key: str, rel_pdf: str) -> str:
    title = paper.get("title", key)
    venue = paper.get("venue", "")
    year = paper.get("year", "")
    paper_id = paper.get("paperId", "")
    return f"""---
bibtex_key: {key}
title: "{title.replace('"', '\\"')}"
year: {year}
venue: "{venue.replace('"', '\\"')}"
paper_id: {paper_id}
pdf_path: "{rel_pdf}"
semantic_scholar_url: "{'https://www.semanticscholar.org/paper/' + paper_id if paper_id else ''}"
one_word_summary: other
summary_status: missing
---

# {title}

TODO: Generate this summary with summarize_papers_gemini.py.
"""


def build_rows(
    workspace: Path,
    pool: dict,
    create_stubs: bool,
    allow_incomplete: bool,
) -> tuple[list[dict], list[str]]:
    ref_dir = workspace / "reference_database"
    summary_dir = ref_dir / "summaries"
    pdf_dir = ref_dir / "papers"
    summary_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    problems: list[str] = []
    seen_keys: set[str] = set()

    for i, paper in enumerate(pool.get("papers") or []):
        key = bibtex_key(paper, i)
        if key in seen_keys:
            problems.append(f"duplicate bibtex_key in citation_pool.json: {key}")
        seen_keys.add(key)

        summary_path = summary_dir / f"{key}.md"
        pdf_path = pdf_dir / f"{key}.pdf"
        rel_summary = os.path.relpath(summary_path, workspace)
        rel_pdf = os.path.relpath(pdf_path, workspace) if pdf_path.exists() else ""

        if not summary_path.exists() and create_stubs:
            summary_path.write_text(stub_summary(paper, key, rel_pdf))

        fm = parse_frontmatter(summary_path)
        if not summary_path.exists():
            problems.append(f"missing summary: {summary_path}")
        elif fm.get("bibtex_key") != key:
            problems.append(f"{summary_path}: frontmatter bibtex_key does not match {key}")
        elif fm.get("summary_status") != "complete" and not allow_incomplete:
            problems.append(
                f"{summary_path}: summary_status is "
                f"{fm.get('summary_status', 'missing')!r}, expected 'complete'"
            )

        summary_word = one_word(fm.get("one_word_summary", "other"))
        status_parts = []
        status_parts.append("summary_ok" if summary_path.exists() else "summary_missing")
        status_parts.append("pdf_ok" if pdf_path.exists() else "pdf_missing")
        if fm.get("summary_status") in ("missing", "gemini_failed", "needs_review", "prompt_only"):
            status_parts.append(fm["summary_status"])

        rows.append({
            "bibtex_key": key,
            "title": paper.get("title", ""),
            "location": rel_summary if summary_path.exists() else "",
            "year": str(paper.get("year", "")),
            "venue": paper.get("venue", ""),
            "summary_md_path": rel_summary if summary_path.exists() else "",
            "pdf_path": rel_pdf,
            "one_word_summary": summary_word,
            "status": ";".join(status_parts),
        })

    summary_keys = {path.stem for path in summary_dir.glob("*.md")}
    extra_summaries = sorted(summary_keys - seen_keys)
    for key in extra_summaries:
        problems.append(f"orphan summary with no citation_pool entry: {summary_dir / (key + '.md')}")

    return rows, problems


def write_index(workspace: Path, rows: list[dict]) -> tuple[Path, Path]:
    ref_dir = workspace / "reference_database"
    ref_dir.mkdir(parents=True, exist_ok=True)
    csv_path = ref_dir / "index.csv"
    json_path = ref_dir / "index.json"

    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=INDEX_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    json_path.write_text(json.dumps({"papers": rows}, indent=2, ensure_ascii=False) + "\n")
    return csv_path, json_path


def read_existing_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workspace", required=True)
    p.add_argument("--pool", help="citation_pool.json path")
    p.add_argument("--fix", action="store_true", help="Rewrite index.csv/index.json from current files")
    p.add_argument("--create-stubs", action="store_true", help="Create placeholder summaries for missing Markdown files")
    p.add_argument("--allow-incomplete", action="store_true", help="Allow missing/failed/dry-run summary statuses")
    args = p.parse_args()

    workspace = Path(args.workspace)
    pool_path = Path(args.pool) if args.pool else workspace / "citation_pool.json"
    pool = load_pool(pool_path)

    rows, problems = build_rows(
        workspace,
        pool,
        create_stubs=args.create_stubs,
        allow_incomplete=args.allow_incomplete,
    )
    ref_dir = workspace / "reference_database"
    csv_path = ref_dir / "index.csv"
    json_path = ref_dir / "index.json"

    if args.fix:
        csv_path, json_path = write_index(workspace, rows)
        print(f"OK: wrote {len(rows)} rows → {csv_path}")
        print(f"OK: wrote {len(rows)} rows → {json_path}")
    else:
        existing_rows = read_existing_csv(csv_path)
        if not existing_rows:
            problems.append(f"missing index: {csv_path}")
        elif existing_rows != rows:
            problems.append(f"index out of sync: {csv_path} (run with --fix)")

    if problems:
        for problem in problems:
            print(f"WARN: {problem}", file=sys.stderr)
        return 1

    print(f"OK: reference index synchronized ({len(rows)} papers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
