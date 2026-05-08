#!/usr/bin/env python3
"""
build_reference_database.py — Batch wrapper for reference enrichment.

The literature-review agent should call this script with the workspace and citation pool.
The script handles all mechanics for every verified paper:

1. assign or preserve a stable bibtex_key,
2. download/cache the PDF when an open PDF URL is available,
3. call Gemini CLI to create the structured Markdown summary,
4. update the database index.

Usage:
    python build_reference_database.py --workspace workspace --pool workspace/citation_pool.json
"""
import argparse
import csv
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from bibtex_format import assign_bibtex_keys

# --- Inlined from download_papers.py ---
def arxiv_pdf_url(arxiv_id: str) -> str:
    arxiv_id = arxiv_id.strip()
    if not arxiv_id:
        return ""
    if arxiv_id.lower().startswith("arxiv:"):
        arxiv_id = arxiv_id.split(":", 1)[1]
    return f"https://arxiv.org/pdf/{arxiv_id}.pdf"

def candidate_urls(paper: dict) -> list[tuple[str, str]]:
    urls: list[tuple[str, str]] = []

    open_access_pdf = paper.get("openAccessPdf") or {}
    if isinstance(open_access_pdf, dict):
        url = open_access_pdf.get("url")
        if url:
            urls.append(("openAccessPdf", url))

    ext = paper.get("externalIds") or {}
    if arxiv := ext.get("ArXiv"):
        urls.append(("arxiv", arxiv_pdf_url(arxiv)))

    for field in ("pdf_url", "pdfUrl", "url"):
        url = paper.get(field)
        if isinstance(url, str) and url.lower().endswith(".pdf"):
            urls.append((field, url))

    seen = set()
    unique: list[tuple[str, str]] = []
    for source, url in urls:
        if url and url not in seen:
            unique.append((source, url))
            seen.add(url)
    return unique

def fetch_pdf(url: str, out_path: Path, timeout: int, user_agent: str) -> tuple[bool, str]:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "").lower()
            data = resp.read()
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, f"network error: {exc.reason}"
    except TimeoutError:
        return False, "timeout"

    if not data.startswith(b"%PDF") and "pdf" not in content_type:
        return False, f"not a PDF response: content-type={content_type or 'unknown'}"

    out_path.write_bytes(data)
    return True, f"{len(data)} bytes"


# --- Inlined from summarize_papers_gemini.py ---
TEMPLATE = """---
bibtex_key: {bibtex_key}
title: {title_yaml}
year: {year}
venue: {venue_yaml}
paper_id: {paper_id}
pdf_path: {pdf_path_yaml}
semantic_scholar_url: {semantic_scholar_url_yaml}
one_word_summary: {one_word_summary}
summary_status: {summary_status}
---

# {title}

## One-Sentence Summary

{one_sentence_summary}

## Problem

{problem}

## Core Method

{core_method}

## Representation

{representation}

## Inputs and Assumptions

{inputs_and_assumptions}

## Training and Inference

{training_and_inference}

## Evaluation

{evaluation}

## Main Strengths

{main_strengths}

## Limitations

{limitations}

## Relevance to LiteAvatar

{relevance_to_liteavatar}

## Possible Citation Use

{possible_citation_use}
"""

PROMPT = """You are a careful research assistant building a local reference database for a SIGGRAPH paper on LiteAvatar, a feedforward UV-anchored Gaussian avatar method with animation-rendering guided token refinement.

Summarize the paper below into structured Markdown. Be detailed enough to help write a related-work section, but do not invent details not supported by the paper or metadata. If the local PDF path is available, use it as the primary source. If you cannot access or read the PDF, say the summary is based on metadata only.

Return Markdown only. Use exactly this structure:

---
bibtex_key: <key>
title: <title>
year: <year>
venue: <venue>
paper_id: <paper_id>
pdf_path: <pdf_path or empty>
semantic_scholar_url: <url or empty>
one_word_summary: <one lowercase word>
summary_status: complete
---

# <title>

## One-Sentence Summary

## Problem

## Core Method

## Representation

## Inputs and Assumptions

## Training and Inference

## Evaluation

## Main Strengths

## Limitations

## Relevance to LiteAvatar

## Possible Citation Use

One-word summary constraints: choose one lowercase word, preferably one of:
mesh, nerf, gaussian, feedforward, pose, attention, metric, dataset, rendering, optimization, survey, other.

Paper metadata:
{metadata}

Local PDF path:
{pdf_path}
"""

def yaml_scalar(value: object) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'

def guess_one_word(paper: dict) -> str:
    text = " ".join([
        paper.get("title", ""),
        paper.get("abstract", ""),
        paper.get("venue", ""),
    ]).lower()
    for word in ("gaussian", "feedforward", "attention", "pose", "mesh", "nerf", "metric", "dataset", "rendering", "optimization"):
        if word in text:
            return word
    return "other"

def s2_url(paper: dict) -> str:
    paper_id = paper.get("paperId")
    return f"https://www.semanticscholar.org/paper/{paper_id}" if paper_id else ""

def parse_frontmatter(md: str) -> dict:
    if not md.startswith("---"):
        return {}
    end = md.find("\n---", 3)
    if end == -1:
        return {}
    fields = {}
    for line in md[3:end].splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        fields[k.strip()] = v.strip().strip('"')
    return fields

def fallback_summary(paper: dict, key: str, pdf_path: str, status: str) -> str:
    abstract = paper.get("abstract") or "No abstract is available in the citation pool."
    title = paper.get("title", key)
    one_word = guess_one_word(paper)
    values = {
        "bibtex_key": key,
        "title_yaml": yaml_scalar(title),
        "year": paper.get("year", ""),
        "venue_yaml": yaml_scalar(paper.get("venue", "")),
        "paper_id": paper.get("paperId", ""),
        "pdf_path_yaml": yaml_scalar(pdf_path),
        "semantic_scholar_url_yaml": yaml_scalar(s2_url(paper)),
        "one_word_summary": one_word,
        "summary_status": status,
        "title": title,
        "one_sentence_summary": abstract,
        "problem": "TODO: Replace this metadata-only placeholder with a detailed Gemini summary.",
        "core_method": "TODO",
        "representation": "TODO",
        "inputs_and_assumptions": "TODO",
        "training_and_inference": "TODO",
        "evaluation": "TODO",
        "main_strengths": "TODO",
        "limitations": "TODO",
        "relevance_to_liteavatar": "TODO",
        "possible_citation_use": "TODO",
    }
    return TEMPLATE.format(**values)

def clean_markdown_output(text: str) -> str:
    text = text.strip()
    match = re.match(r"^```(?:markdown|md)?\s*(.*?)\s*```$", text, flags=re.S)
    if match:
        text = match.group(1).strip()
    return text + "\n"

def run_gemini(command: str, prompt: str, timeout: int, cwd: Path) -> tuple[int, str, str]:
    cmd = shlex.split(command) + ["-p", prompt]
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


# --- Main Logic ---

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

def ensure_key(paper: dict) -> str:
    if paper.get("bibtex_key"):
        return paper["bibtex_key"]
    assign_bibtex_keys([paper])
    return paper["bibtex_key"]

def download_one(paper: dict, pdf_path: Path, timeout: int, user_agent: str, force: bool) -> dict:
    record = {
        "pdf_status": "missing",
        "pdf_path": "",
        "pdf_source": "",
        "pdf_url": "",
        "pdf_message": "",
    }
    if pdf_path.exists() and not force:
        record.update({
            "pdf_status": "existing",
            "pdf_path": str(pdf_path),
            "pdf_message": f"{pdf_path.stat().st_size} bytes",
        })
        return record

    urls = candidate_urls(paper)
    if not urls:
        record["pdf_message"] = "no open-access PDF URL in metadata"
        return record

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    for source, url in urls:
        ok, message = fetch_pdf(url, pdf_path, timeout, user_agent)
        record.update({
            "pdf_source": source,
            "pdf_url": url,
            "pdf_message": message,
        })
        if ok:
            record["pdf_status"] = "downloaded"
            record["pdf_path"] = str(pdf_path)
            return record

    return record

def summarize_one(
    paper: dict,
    key: str,
    workspace: Path,
    summary_path: Path,
    prompt_path: Path,
    pdf_path: Path,
    gemini_command: str,
    timeout: int,
    force: bool,
    dry_run: bool,
    fallback_on_error: bool,
) -> dict:
    record = {
        "summary_status": "missing",
        "summary_md_path": "",
        "prompt_path": "",
        "one_word_summary": "",
        "summary_message": "",
    }

    if summary_path.exists() and not force:
        fields = parse_frontmatter(summary_path.read_text(errors="replace"))
        record.update({
            "summary_status": fields.get("summary_status", "existing"),
            "summary_md_path": str(summary_path),
            "prompt_path": str(prompt_path) if prompt_path.exists() else "",
            "one_word_summary": fields.get("one_word_summary", ""),
            "summary_message": "existing summary",
        })
        return record

    pdf_for_prompt = str(pdf_path.resolve()) if pdf_path.exists() else ""
    metadata = json.dumps(paper, indent=2, ensure_ascii=False)
    prompt = PROMPT.format(metadata=metadata, pdf_path=pdf_for_prompt or "(no local PDF available)")
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt)

    if dry_run:
        summary_path.write_text(fallback_summary(paper, key, pdf_for_prompt, "prompt_only"))
        record.update({
            "summary_status": "prompt_only",
            "summary_md_path": str(summary_path),
            "prompt_path": str(prompt_path),
            "summary_message": "dry-run placeholder written",
        })
        return record

    try:
        code, stdout, stderr = run_gemini(gemini_command, prompt, timeout, workspace)
    except Exception as exc:
        code, stdout, stderr = 1, "", str(exc)

    if code == 0 and stdout.strip():
        md = clean_markdown_output(stdout)
        fields = parse_frontmatter(md)
        if fields.get("bibtex_key") != key:
            summary_path.write_text(fallback_summary(paper, key, pdf_for_prompt, "needs_review"))
            record.update({
                "summary_status": "needs_review",
                "summary_md_path": str(summary_path),
                "prompt_path": str(prompt_path),
                "summary_message": "Gemini output bibtex_key did not match canonical key",
            })
            return record

        summary_path.write_text(md)
        fields = parse_frontmatter(md)
        if not fields.get("one_word_summary"):
            fields["one_word_summary"] = ""
        record.update({
            "summary_status": fields.get("summary_status", "complete"),
            "summary_md_path": str(summary_path),
            "prompt_path": str(prompt_path),
            "one_word_summary": fields.get("one_word_summary", ""),
            "summary_message": "Gemini summary written",
        })
        return record

    if fallback_on_error:
        summary_path.write_text(fallback_summary(paper, key, pdf_for_prompt, "gemini_failed"))
        record.update({
            "summary_status": "gemini_failed",
            "summary_md_path": str(summary_path),
            "prompt_path": str(prompt_path),
            "summary_message": stderr.strip()[:500],
        })
        return record

    raise SystemExit(f"ERROR: Gemini failed for {key}: {stderr.strip()[:500]}")

def read_index(csv_path: Path) -> list[dict]:
    if not csv_path.exists():
        return []
    with csv_path.open(newline="") as f:
        return list(csv.DictReader(f))

def write_index(ref_dir: Path, rows: list[dict]) -> None:
    ref_dir.mkdir(parents=True, exist_ok=True)
    csv_path = ref_dir / "index.csv"
    json_path = ref_dir / "index.json"

    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=INDEX_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    json_path.write_text(json.dumps({"papers": rows}, indent=2, ensure_ascii=False) + "\n")

def upsert_index_row(
    ref_dir: Path,
    key: str,
    title: str,
    location: str,
    year: object,
    venue: str,
    summary_md_path: str,
    pdf_path: str,
    one_word_summary: str,
    status: str,
) -> dict:
    rows = read_index(ref_dir / "index.csv")
    rows_by_key = {row.get("bibtex_key", ""): row for row in rows if row.get("bibtex_key")}
    row = {
        "bibtex_key": key,
        "title": title,
        "location": location,
        "year": str(year or ""),
        "venue": venue or "",
        "summary_md_path": summary_md_path,
        "pdf_path": pdf_path,
        "one_word_summary": one_word_summary or "other",
        "status": status,
    }
    rows_by_key[key] = row
    ordered = sorted(rows_by_key.values(), key=lambda item: (item.get("title", ""), item.get("bibtex_key", "")))
    write_index(ref_dir, ordered)
    return row

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workspace", required=True)
    p.add_argument("--pool", required=True)
    p.add_argument("--gemini-command", default="gemini")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--download-timeout", type=int, default=45)
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--fallback-on-error", action="store_true")
    p.add_argument(
        "--user-agent",
        default="PaperOrchestra literature-review-agent reference enrichment",
    )
    args = p.parse_args()

    workspace = Path(args.workspace)
    pool_path = Path(args.pool)

    if not pool_path.exists():
        sys.exit(f"ERROR: Pool file not found: {pool_path}")

    with open(pool_path, "r", encoding="utf-8") as f:
        pool_data = json.load(f)
    
    papers = pool_data.get("papers", [])
    if not papers:
        papers = pool_data if isinstance(pool_data, list) else []

    if not papers:
        print("No papers found in pool.")
        return 0

    ref_dir = workspace / "reference_database"

    for i, paper in enumerate(papers):
        title = paper.get("title", "Unknown Title")
        print(f"\n--- Enriching paper {i+1}/{len(papers)}: {title} ---")

        key = ensure_key(paper)
        pdf_path = ref_dir / "papers" / f"{key}.pdf"
        summary_path = ref_dir / "summaries" / f"{key}.md"
        prompt_path = ref_dir / "prompts" / f"{key}.prompt.txt"

        pdf_record = download_one(
            paper=paper,
            pdf_path=pdf_path,
            timeout=args.download_timeout,
            user_agent=args.user_agent,
            force=args.force,
        )
        summary_record = summarize_one(
            paper=paper,
            key=key,
            workspace=workspace,
            summary_path=summary_path,
            prompt_path=prompt_path,
            pdf_path=pdf_path,
            gemini_command=args.gemini_command,
            timeout=args.timeout,
            force=args.force,
            dry_run=args.dry_run,
            fallback_on_error=args.fallback_on_error,
        )

        result = {
            "bibtex_key": key,
            "title": paper.get("title", ""),
            "year": paper.get("year", ""),
            "venue": paper.get("venue", ""),
            "paper_id": paper.get("paperId", ""),
            "semantic_scholar_url": s2_url(paper),
            **pdf_record,
            **summary_record,
        }

        for field in ("pdf_path", "summary_md_path", "prompt_path"):
            if result.get(field):
                try:
                    result[field] = os.path.relpath(result[field], workspace)
                except ValueError:
                    pass

        one_word = result.get("one_word_summary") or "other"
        location = result.get("summary_md_path") or ""
        status = ";".join([
            result.get("summary_status", "summary_missing"),
            result.get("pdf_status", "pdf_missing"),
        ])
        upsert_index_row(
            ref_dir=ref_dir,
            key=key,
            title=paper.get("title", ""),
            location=location,
            year=paper.get("year", ""),
            venue=paper.get("venue", ""),
            summary_md_path=result.get("summary_md_path", ""),
            pdf_path=result.get("pdf_path", ""),
            one_word_summary=one_word,
            status=status,
        )

        print(f"[{key}] PDF: {pdf_record['pdf_status']} | Summary: {summary_record['summary_status']}")

    print("\nBatch enrichment complete.")
    return 0

if __name__ == "__main__":
    sys.exit(main())