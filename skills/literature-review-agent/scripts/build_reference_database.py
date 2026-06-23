#!/usr/bin/env python3
"""
build_reference_database.py — Batch wrapper for reference enrichment.

The literature-review agent should call this script with a citation pool.
The workspace is inferred as the directory containing that pool.
The script handles all mechanics for every verified paper:

1. assign or preserve a stable bibtex_key,
2. download/cache the PDF when an open PDF URL is available,
3. call agy to create the structured Markdown summary,
4. run reference-database maintenance so index.json reflects summaries.

Usage:
    python build_reference_database.py --pool workspace/citation_pool.json
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from bibtex_format import assign_bibtex_keys
from maintain_reference_database import maintain_reference_database, pool_papers

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


# --- Inlined from summarize_papers_gemini.py; this wrapper defaults to agy ---
SCRIPT_DIR = Path(__file__).parent.resolve()
try:
    TEMPLATE = (SCRIPT_DIR.parent / "references" / "summary_template.md").read_text(encoding="utf-8")
    PROMPT = (SCRIPT_DIR.parent / "references" / "summary_prompt.txt").read_text(encoding="utf-8")
except FileNotFoundError:
    print("ERROR: Could not find summary_template.md or summary_prompt.txt in references/", file=sys.stderr)
    sys.exit(1)

def yaml_scalar(value: object) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'

def guess_one_word(paper: dict) -> str:
    text = " ".join([
        str(paper.get("title") or ""),
        str(paper.get("abstract") or ""),
        str(paper.get("venue") or ""),
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
    body = TEMPLATE.replace("# <title>", f"# {title}", 1)
    body = body.replace(
        "(A concise but technical summary, including only the technical details and results, and it can include more technical details that the original abstract does not contain.)",
        abstract,
        1,
    )
    body = body.replace(
        "(Make this detailed, self-contained, mathematically rich and rigorous.)",
        "TODO: Replace this metadata-only placeholder with a detailed agy summary.",
        1,
    )
    body = body.replace("(Include concrete numbers and metrics.)", "TODO", 1)
    return apply_summary_frontmatter(body, paper, key, pdf_path, status)

def summary_frontmatter(paper: dict, key: str, pdf_path: str, status: str) -> str:
    lines = [
        f"bibtex_key: {key}",
        f"title: {yaml_scalar(paper.get('title', key))}",
        f"year: {paper.get('year', '')}",
        f"venue: {yaml_scalar(paper.get('venue', ''))}",
        f"paper_id: {paper.get('paperId', '')}",
        f"pdf_path: {yaml_scalar(pdf_path)}",
        f"semantic_scholar_url: {yaml_scalar(s2_url(paper))}",
        f"one_word_summary: {guess_one_word(paper)}",
        f"summary_status: {status}",
    ]
    return "\n".join(lines)

def apply_summary_frontmatter(md: str, paper: dict, key: str, pdf_path: str, status: str) -> str:
    header = f"---\n{summary_frontmatter(paper, key, pdf_path, status)}\n---"
    body = md.strip()
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            body = body[end + len("\n---"):].lstrip()
    title = paper.get("title", key)
    if body.startswith("# "):
        body = re.sub(r"^# .*$", f"# {title}", body, count=1, flags=re.M)
    else:
        body = f"# {title}\n\n{body}"
    return f"{header}\n\n{body}\n"

def clean_markdown_output(text: str) -> str:
    text = text.strip()
    match = re.match(r"^```(?:markdown|md)?\s*(.*?)\s*```$", text, flags=re.S)
    if match:
        text = match.group(1).strip()
    return text + "\n"

from common_subagent import run_subagent


# --- Main Logic ---

def extract_technical_summary(md_path: Path) -> str:
    if not md_path.exists():
        return ""
    content = md_path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"## Technical Summary\s*\n+(.*?)\n+## Problem", content, flags=re.S)
    if match:
        return match.group(1).strip()
    return ""

def ensure_key(paper: dict) -> str:
    if paper.get("bibtex_key"):
        return paper["bibtex_key"]
    assign_bibtex_keys([paper])
    return paper["bibtex_key"]

def ensure_keys(papers: list[dict]) -> None:
    used = {paper["bibtex_key"] for paper in papers if paper.get("bibtex_key")}
    for paper in papers:
        if paper.get("bibtex_key"):
            continue
        assign_bibtex_keys([paper])
        base_key = paper["bibtex_key"]
        key = base_key
        suffix_index = 0
        while key in used:
            key = f"{base_key}{chr(ord('a') + suffix_index)}"
            suffix_index += 1
        paper["bibtex_key"] = key
        used.add(key)

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
    summary_command: str,
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
    rel_pdf_yaml = f"papers/{key}.pdf" if pdf_path.exists() else ""
    metadata = json.dumps(paper, indent=2, ensure_ascii=False)
    prompt = PROMPT.format(
        summary_template=TEMPLATE.rstrip(),
        metadata=metadata,
        pdf_path=pdf_for_prompt or "(no local PDF available)",
    )
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    if dry_run:
        summary_path.write_text(fallback_summary(paper, key, rel_pdf_yaml, "prompt_only"))
        record.update({
            "summary_status": "prompt_only",
            "summary_md_path": str(summary_path),
            "prompt_path": str(prompt_path),
            "summary_message": "dry-run placeholder written",
        })
        return record

    try:
        code, stdout, stderr = run_subagent(summary_command, prompt, timeout, workspace, f"summary_{key}")
    except Exception as exc:
        code, stdout, stderr = 1, "", str(exc)

    if code == 0 and stdout.strip():
        md = apply_summary_frontmatter(
            clean_markdown_output(stdout),
            paper=paper,
            key=key,
            pdf_path=rel_pdf_yaml,
            status="complete",
        )
        fields = parse_frontmatter(md)
        if fields.get("bibtex_key") != key:
            summary_path.write_text(fallback_summary(paper, key, rel_pdf_yaml, "needs_review"))
            record.update({
                "summary_status": "needs_review",
                "summary_md_path": str(summary_path),
                "prompt_path": str(prompt_path),
                "summary_message": "summary output bibtex_key did not match canonical key",
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
            "summary_message": "agy summary written",
        })
        return record

    if fallback_on_error:
        summary_path.write_text(fallback_summary(paper, key, rel_pdf_yaml, "agy_failed"))
        record.update({
            "summary_status": "agy_failed",
            "summary_md_path": str(summary_path),
            "prompt_path": str(prompt_path),
            "summary_message": stderr.strip()[:500],
        })
        return record

    raise SystemExit(f"ERROR: summary command failed for {key}: {stderr.strip()[:500]}")

def build_reference_database(
    pool_path: Path,
    summary_command: str,
    timeout: int,
    download_timeout: int,
    force: bool,
    dry_run: bool,
    fallback_on_error: bool,
    user_agent: str,
) -> int:
    pool_path = pool_path.resolve()
    workspace = pool_path.parent

    if not pool_path.exists():
        sys.exit(f"ERROR: Pool file not found: {pool_path}")

    with pool_path.open("r", encoding="utf-8") as f:
        pool_data = json.load(f)

    papers = pool_papers(pool_data)
    if not papers:
        print("No papers found in pool.")
        return 0

    ensure_keys(papers)

    ref_dir = workspace / "reference_database"
    maintenance = maintain_reference_database(workspace=workspace, papers=papers, fix=True)
    keys_to_regenerate = {paper["bibtex_key"] for paper in papers} if force else maintenance.regenerate_keys

    if maintenance.changed:
        print(f"Maintenance: synchronized {maintenance.index_path}")
    if keys_to_regenerate:
        print(f"Regenerating {len(keys_to_regenerate)} missing or corrupt references.")
    else:
        print("No missing or corrupt references found.")

    for problem in maintenance.problems:
        key = Path(problem.split(":", 1)[0]).stem if ":" in problem else ""
        if key in keys_to_regenerate:
            print(f"WARN: {problem}", file=sys.stderr)

    for i, paper in enumerate(papers):
        title = paper.get("title", "Unknown Title")
        key = ensure_key(paper)

        if key not in keys_to_regenerate:
            print(f"\n--- Skipping paper {i+1}/{len(papers)}: {title} ({key}) ---")
            print(f"[{key}] Reference summary is already valid.")
            continue

        print(f"\n--- Enriching paper {i+1}/{len(papers)}: {title} ---")

        pdf_path = ref_dir / "papers" / f"{key}.pdf"
        summary_path = ref_dir / "summaries" / f"{key}.md"
        prompt_path = ref_dir / "prompts" / f"{key}.prompt.txt"

        pdf_record = download_one(
            paper=paper,
            pdf_path=pdf_path,
            timeout=download_timeout,
            user_agent=user_agent,
            force=force,
        )
        summary_record = summarize_one(
            paper=paper,
            key=key,
            workspace=workspace,
            summary_path=summary_path,
            prompt_path=prompt_path,
            pdf_path=pdf_path,
            summary_command=summary_command,
            timeout=timeout,
            force=True,
            dry_run=dry_run,
            fallback_on_error=fallback_on_error,
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

        print(f"[{key}] PDF: {pdf_record['pdf_status']} | Summary: {summary_record['summary_status']}")

    final_maintenance = maintain_reference_database(workspace=workspace, papers=papers, fix=True)
    if final_maintenance.changed:
        print(f"\nMaintenance: synchronized {final_maintenance.index_path}")

    print("\nBatch enrichment complete.")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pool", required=True)
    p.add_argument("--summary-command", default="agy")
    p.add_argument("--gemini-command", help="Deprecated alias for --summary-command.")
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

    return build_reference_database(
        pool_path=Path(args.pool),
        summary_command=args.gemini_command or args.summary_command,
        timeout=args.timeout,
        download_timeout=args.download_timeout,
        force=args.force,
        dry_run=args.dry_run,
        fallback_on_error=args.fallback_on_error,
        user_agent=args.user_agent,
    )

if __name__ == "__main__":
    sys.exit(main())
