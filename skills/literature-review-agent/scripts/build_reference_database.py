#!/usr/bin/env python3
"""
build_reference_database.py — Batch wrapper for reference enrichment.

The literature-review agent should call this script with a citation pool.
The workspace is inferred as the directory containing that pool.
For every verified paper the script:

1. assigns or preserves a stable bibtex_key,
2. summarizes the (already downloaded) PDF into structured Markdown — via the
   litellm API by default (--mode llm), or a CLI subagent such as agy
   (--mode agent),
3. runs reference-database maintenance so index.json reflects the summaries.

PDFs themselves are fetched separately by download_reference_pdfs.py.

Usage:
    python build_reference_database.py --pool workspace/citation_pool.json
"""
import argparse
import concurrent.futures
import datetime
import enum
import functools
import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from bibtex_format import assign_bibtex_keys
from common_subagent import run_subagent


# --- Types ---

class Mode(str, enum.Enum):
    """Summarization backend. Subclasses str so values compare/serialize plainly."""
    LLM = "llm"
    AGENT = "agent"


def backend_label(mode: "Mode") -> str:
    """Human-facing backend name used in messages and status strings."""
    return "LLM" if mode is Mode.LLM else "agy"


class SummaryError(RuntimeError):
    """Raised when a summary cannot be produced and fallback is disabled."""


@dataclass
class Problem:
    """A maintenance problem, optionally attributable to a specific bibtex_key."""
    message: str
    key: str | None = None

    def __str__(self) -> str:
        return self.message


@dataclass
class SummaryResult:
    summary_status: str = "missing"
    summary_md_path: str = ""
    one_word_summary: str = ""
    summary_message: str = ""


@dataclass
class SummaryHealth:
    key: str
    path: Path
    ok: bool
    reason: str


@dataclass
class MaintenanceResult:
    index_path: Path
    rows: list[dict]
    changed: bool
    problems: list[Problem]
    regenerate_keys: set[str]


# --- Pool & frontmatter helpers ---

def load_pool(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def pool_papers(pool_data: object) -> list[dict]:
    if isinstance(pool_data, dict):
        papers = pool_data.get("papers", [])
        return papers if isinstance(papers, list) else []
    return pool_data if isinstance(pool_data, list) else []


def parse_frontmatter_text(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fields: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip('"')
    return fields


def first_heading(text: str) -> str:
    match = re.search(r"^#\s+(.+?)\s*$", text, flags=re.M)
    return match.group(1).strip() if match else ""


def relative_to_workspace(path: Path, workspace: Path) -> str:
    try:
        return os.path.relpath(path, workspace)
    except ValueError:
        return str(path)


def pdf_status_for_summary(workspace: Path, key: str, fields: dict) -> str:
    pdf_path = fields.get("pdf_path", "")
    candidates: list[Path] = []
    if pdf_path:
        candidate = Path(pdf_path)
        candidates.append(candidate if candidate.is_absolute() else workspace / candidate)
    candidates.append(workspace / "reference_database" / "papers" / f"{key}.pdf")
    return "existing" if any(path.exists() for path in candidates) else "missing"


def summary_status(fields: dict) -> str:
    return fields.get("summary_status", "").strip()


# --- Reference index maintenance ---

def check_summary(summary_path: Path, expected_key: str) -> SummaryHealth:
    if not summary_path.exists():
        return SummaryHealth(expected_key, summary_path, False, "missing summary")

    text = summary_path.read_text(encoding="utf-8", errors="replace")
    fields = parse_frontmatter_text(text)
    if not fields:
        return SummaryHealth(expected_key, summary_path, False, "missing frontmatter")
    if fields.get("bibtex_key") != expected_key:
        return SummaryHealth(expected_key, summary_path, False, "frontmatter bibtex_key mismatch")
    if summary_status(fields) != "complete":
        return SummaryHealth(expected_key, summary_path, False, f"summary_status={summary_status(fields)!r}")
    if not extract_technical_summary(summary_path):
        return SummaryHealth(expected_key, summary_path, False, "missing technical summary")

    return SummaryHealth(expected_key, summary_path, True, "")


def row_from_summary(summary_path: Path, workspace: Path) -> tuple[dict, Problem | None]:
    text = summary_path.read_text(encoding="utf-8", errors="replace")
    fields = parse_frontmatter_text(text)
    key = fields.get("bibtex_key") or summary_path.stem
    status = summary_status(fields) or "unknown"
    row = {
        "bibtex_key": key,
        "title": fields.get("title") or first_heading(text) or key,
        "location": relative_to_workspace(summary_path, workspace),
        "year": str(fields.get("year", "")),
        "summary": extract_technical_summary(summary_path),
        "status": f"{status};{pdf_status_for_summary(workspace, key, fields)}",
    }
    problem = None if fields.get("bibtex_key") else Problem(
        f"{summary_path}: missing frontmatter bibtex_key", key=key
    )
    return row, problem


def index_rows_from_summaries(workspace: Path) -> tuple[list[dict], list[Problem]]:
    summary_dir = workspace / "reference_database" / "summaries"
    summary_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    problems: list[Problem] = []
    seen_keys: set[str] = set()
    for summary_path in sorted(summary_dir.glob("*.md")):
        row, problem = row_from_summary(summary_path, workspace)
        key = row.get("bibtex_key", "")
        if key in seen_keys:
            problems.append(Problem(f"duplicate summary bibtex_key: {key}", key=key))
            continue

        if not row.get("summary"):
            problems.append(Problem(f"omitted {summary_path.name}: missing technical summary", key=key))
            continue

        seen_keys.add(key)
        rows.append(row)
        if problem:
            problems.append(problem)

    rows.sort(key=lambda item: (item.get("title", ""), item.get("bibtex_key", "")))
    return rows, problems


def read_index(index_path: Path) -> list[dict]:
    if not index_path.exists():
        return []
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    papers = data.get("papers", [])
    return papers if isinstance(papers, list) else []


def write_index(index_path: Path, rows: list[dict]) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps({"papers": rows}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def maintain_reference_database(
    workspace: Path,
    papers: list[dict] | None = None,
    fix: bool = False,
) -> MaintenanceResult:
    workspace = workspace.resolve()
    index_path = workspace / "reference_database" / "index.json"
    rows, problems = index_rows_from_summaries(workspace)
    existing_rows = read_index(index_path)
    changed = existing_rows != rows

    if changed and fix:
        write_index(index_path, rows)
    elif changed:
        problems.append(Problem(f"index out of sync: {index_path}"))

    regenerate_keys: set[str] = set()
    if papers is not None:
        summary_dir = workspace / "reference_database" / "summaries"
        seen_keys: set[str] = set()
        for paper in papers:
            key = paper.get("bibtex_key")
            if not key:
                problems.append(Problem(f"pool paper missing bibtex_key: {paper.get('title', 'unknown title')}"))
                continue
            if key in seen_keys:
                problems.append(Problem(f"duplicate bibtex_key in pool: {key}", key=key))
            seen_keys.add(key)

            health = check_summary(summary_dir / f"{key}.md", key)
            if not health.ok:
                regenerate_keys.add(key)
                problems.append(Problem(f"{health.path}: {health.reason}", key=health.key))

        summary_keys = {row.get("bibtex_key", "") for row in rows if row.get("bibtex_key")}
        for key in sorted(summary_keys - seen_keys):
            problems.append(Problem(f"{summary_dir / (key + '.md')}: orphan summary with no pool entry", key=key))

    return MaintenanceResult(
        index_path=index_path,
        rows=rows,
        changed=changed,
        problems=problems,
        regenerate_keys=regenerate_keys,
    )


# --- Summary generation ---

SCRIPT_DIR = Path(__file__).parent.resolve()
REFERENCES_DIR = SCRIPT_DIR.parent / "references"


@functools.lru_cache(maxsize=1)
def load_summary_assets() -> tuple[str, str]:
    """Return the (required) summary template and prompt, cached after first use.

    Loaded lazily rather than at import time so that importing this module for
    its helpers (e.g. pool_papers) never depends on these files existing.
    """
    try:
        template = (REFERENCES_DIR / "summary_template.md").read_text(encoding="utf-8")
        prompt = (REFERENCES_DIR / "summary_prompt.txt").read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Required summary asset missing in {REFERENCES_DIR}: {exc.filename}"
        ) from exc
    return template, prompt


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

def fallback_summary(paper: dict, key: str, pdf_path: str, status: str) -> str:
    template, _ = load_summary_assets()
    abstract = paper.get("abstract") or "No abstract is available in the citation pool."
    title = paper.get("title", key)
    body = template.replace("# <title>", f"# {title}", 1)
    body = body.replace(
        "(A concise but technical summary, including only the technical details and results, and it can include more technical details that the original abstract does not contain.)",
        abstract,
        1,
    )
    body = body.replace(
        "(Make this detailed, self-contained, mathematically rich and rigorous.)",
        "TODO: Replace this metadata-only placeholder with a detailed LLM summary.",
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


def extract_technical_summary(md_path: Path) -> str:
    if not md_path.exists():
        return ""
    content = md_path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"## Technical Summary\s*\n+(.*?)\n+## Problem", content, flags=re.S)
    if match:
        return match.group(1).strip()
    return ""

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

def summarize_one(
    paper: dict,
    key: str,
    workspace: Path,
    summary_path: Path,
    pdf_path: Path,
    summary_command: str,
    timeout: int,
    dry_run: bool,
    fallback_on_error: bool,
    mode: Mode,
) -> SummaryResult:
    rel_pdf_yaml = f"papers/{key}.pdf" if pdf_path.exists() else ""
    metadata = json.dumps(paper, indent=2, ensure_ascii=False)

    if mode is Mode.LLM:
        if pdf_path.exists():
            try:
                import pypdf
                reader = pypdf.PdfReader(str(pdf_path))
                pdf_text = "".join(page.extract_text() or "" for page in reader.pages)
                pdf_text = pdf_text.encode("utf-8", "replace").decode("utf-8")
                pdf_info = f"Extracted PDF Text:\n{pdf_text[:150000]}"
            except Exception as e:
                pdf_info = f"(Failed to extract text from PDF: {e})"
        else:
            pdf_info = "(no local PDF available)"
    else:
        pdf_info = str(pdf_path.resolve()) if pdf_path.exists() else "(no local PDF available)"

    template, prompt_template = load_summary_assets()
    prompt = prompt_template.format(
        summary_template=template.rstrip(),
        metadata=metadata,
        pdf_path=pdf_info,
    )
    prompt += "\n\nCRITICAL: DO NOT call any tools to search online. You must only use the provided metadata and PDF text to generate the summary."
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    if dry_run:
        summary_path.write_text(fallback_summary(paper, key, rel_pdf_yaml, "prompt_only"))
        return SummaryResult(
            summary_status="prompt_only",
            summary_md_path=str(summary_path),
            summary_message="dry-run placeholder written",
        )

    # Broad except is intentional: any backend failure (network, auth, parsing,
    # subprocess) is converted into a non-zero code so we can fall back below.
    if mode is Mode.LLM:
        try:
            from dotenv import load_dotenv, find_dotenv
            import litellm

            # Load a .env discovered from the current working directory upward,
            # if the key isn't already in the environment.
            if not os.environ.get("LITELLM_API_KEY"):
                load_dotenv(find_dotenv(usecwd=True))

            base_url = os.environ.get("LITELLM_BASE_URL")
            llm_model = os.environ.get("LITELLM_TEXT_MODEL", "gemini-2.5-pro")
            api_key = os.environ.get("LITELLM_API_KEY")

            model_name = f"openai/{llm_model}" if base_url else llm_model

            response = litellm.completion(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                api_base=base_url,
                api_key=api_key,
                timeout=timeout,
            )
            code, stdout, stderr = 0, response.choices[0].message.content, ""
        except Exception as exc:
            code, stdout, stderr = 1, "", str(exc)
    else:
        try:
            code, stdout, stderr = run_subagent(summary_command, prompt, timeout, workspace, f"summary_{key}")
        except Exception as exc:
            code, stdout, stderr = 1, "", str(exc)

    label = backend_label(mode)
    if code == 0 and stdout.strip():
        md = apply_summary_frontmatter(
            clean_markdown_output(stdout),
            paper=paper,
            key=key,
            pdf_path=rel_pdf_yaml,
            status="complete",
        )
        fields = parse_frontmatter_text(md)
        if fields.get("bibtex_key") != key:
            summary_path.write_text(fallback_summary(paper, key, rel_pdf_yaml, "needs_review"))
            return SummaryResult(
                summary_status="needs_review",
                summary_md_path=str(summary_path),
                summary_message="summary output bibtex_key did not match canonical key",
            )

        summary_path.write_text(md)
        return SummaryResult(
            summary_status=fields.get("summary_status", "complete"),
            summary_md_path=str(summary_path),
            one_word_summary=fields.get("one_word_summary", ""),
            summary_message=f"{label} summary written",
        )

    if fallback_on_error:
        summary_path.write_text(fallback_summary(paper, key, rel_pdf_yaml, f"{label.lower()}_failed"))
        return SummaryResult(
            summary_status=f"{label.lower()}_failed",
            summary_md_path=str(summary_path),
            summary_message=stderr.strip()[:500],
        )

    raise SummaryError(f"summary command failed for {key}: {stderr.strip()[:500]}")

# --- Batch driver & CLI ---

def build_reference_database(
    pool_path: Path,
    summary_command: str,
    timeout: int,
    force: bool,
    dry_run: bool,
    fallback_on_error: bool,
    mode: Mode,
    workers: int,
) -> int:
    pool_path = pool_path.resolve()
    workspace = pool_path.parent

    if not pool_path.exists():
        print(f"ERROR: Pool file not found: {pool_path}", file=sys.stderr)
        return 1

    papers = pool_papers(load_pool(pool_path))
    if not papers:
        print("No papers found in pool.")
        return 0

    # Summarization needs the template/prompt; fail fast with a clear message.
    try:
        load_summary_assets()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    ensure_keys(papers)

    ref_dir = workspace / "reference_database"

    missing_pdfs = []
    for paper in papers:
        key = paper["bibtex_key"]
        pdf_path = ref_dir / "papers" / f"{key}.pdf"
        if not pdf_path.exists():
            missing_pdfs.append(key)

    if missing_pdfs:
        print(f"ERROR: Verification failed. {len(missing_pdfs)} PDFs are missing. All papers must have a valid PDF before summarization.", file=sys.stderr)
        for key in missing_pdfs:
            print(f"  Missing: {key}.pdf", file=sys.stderr)
        return 1

    maintenance = maintain_reference_database(workspace=workspace, papers=papers, fix=True)
    keys_to_regenerate = {paper["bibtex_key"] for paper in papers} if force else maintenance.regenerate_keys

    if maintenance.changed:
        print(f"Maintenance: synchronized {maintenance.index_path}")
    if keys_to_regenerate:
        print(f"Regenerating {len(keys_to_regenerate)} missing or corrupt references.")
    else:
        print("No missing or corrupt references found.")

    for problem in maintenance.problems:
        if problem.key in keys_to_regenerate:
            print(f"WARN: {problem.message}", file=sys.stderr)

    skipped_papers = []
    papers_to_enrich = []
    for i, paper in enumerate(papers):
        key = paper["bibtex_key"]
        if key not in keys_to_regenerate:
            skipped_papers.append((i, paper, key))
        else:
            papers_to_enrich.append((i, paper, key))

    for i, paper, key in skipped_papers:
        title = paper.get("title", "Unknown Title")
        print(f"--- Skipping paper {i+1}/{len(papers)}: {title} ({key}) ---")
        print(f"[{key}] Reference summary is already valid.")

    total_to_enrich = len(papers_to_enrich)
    if total_to_enrich > 0:
        print(f"\nStarting multithreaded enrichment for {total_to_enrich} papers...")
        completed = 0
        counter_lock = threading.Lock()

        def process_paper(args_tuple):
            nonlocal completed
            i, paper, key = args_tuple
            title = paper.get("title", "Unknown Title")

            print(f"\n--- Enriching paper {i+1}/{len(papers)}: {title} ---")

            pdf_path = ref_dir / "papers" / f"{key}.pdf"
            summary_path = ref_dir / "summaries" / f"{key}.md"
            pdf_status = "existing" if pdf_path.exists() else "missing"

            summary_record = summarize_one(
                paper=paper,
                key=key,
                workspace=workspace,
                summary_path=summary_path,
                pdf_path=pdf_path,
                summary_command=summary_command,
                timeout=timeout,
                dry_run=dry_run,
                fallback_on_error=fallback_on_error,
                mode=mode,
            )

            with counter_lock:
                completed += 1
                done = completed

            print(f"[{key}] PDF: {pdf_status} | Summary: {summary_record.summary_status}")
            print(f"finished summarizing {key}. ... {done}/{total_to_enrich}")

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                list(executor.map(process_paper, papers_to_enrich))
        except SummaryError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    final_maintenance = maintain_reference_database(workspace=workspace, papers=papers, fix=True)
    if final_maintenance.changed:
        print(f"\nMaintenance: synchronized {final_maintenance.index_path}")

    print("\nBatch enrichment complete.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", required=True)
    parser.add_argument("--summary-command", default="agy")
    parser.add_argument("--gemini-command", help="Deprecated alias for --summary-command.")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fallback-on-error", action="store_true")
    parser.add_argument("--mode", choices=[m.value for m in Mode], default=Mode.LLM.value,
                        help="Summarization backend. 'llm' (default, preferred): direct litellm API call. "
                             "'agent': CLI subagent named by --summary-command (e.g. agy) — use as a fallback when llm fails.")
    parser.add_argument("--workers", type=int, default=None, help="Number of concurrent workers (default: 4 for agent, 100 for llm)")
    parser.add_argument("--maintain-only", action="store_true", help="Only run reference database maintenance and check, skipping summarization")
    args = parser.parse_args()

    mode = Mode(args.mode)
    if args.workers is None:
        args.workers = 4 if mode is Mode.AGENT else 100

    workspace = Path(args.pool).resolve().parent

    log_dir = workspace / "reference_database" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"build_{timestamp}.log"

    log_handle = open(log_file, "a", encoding="utf-8")
    log_lock = threading.Lock()

    class TeeLogger:
        def __init__(self, stream):
            self.stream = stream

        def write(self, data):
            with log_lock:
                self.stream.write(data)
                self.stream.flush()
                log_handle.write(data)
                log_handle.flush()

        def flush(self):
            self.stream.flush()

    sys.stdout = TeeLogger(sys.stdout)
    sys.stderr = TeeLogger(sys.stderr)

    print(f"Logging unbuffered output to {log_file}")

    if args.maintain_only:
        papers = pool_papers(load_pool(Path(args.pool)))
        result = maintain_reference_database(workspace=workspace, papers=papers, fix=True)
        if result.changed:
            print(f"OK: wrote {len(result.rows)} rows -> {result.index_path}")
        else:
            print(f"OK: reference index synchronized ({len(result.rows)} summaries)")
        if result.regenerate_keys:
            print(f"NEEDS_REGEN: {', '.join(sorted(result.regenerate_keys))}")
        if result.problems:
            for problem in result.problems:
                print(f"WARN: {problem.message}", file=sys.stderr)
            return 1
        return 0

    return build_reference_database(
        pool_path=Path(args.pool),
        summary_command=args.gemini_command or args.summary_command,
        timeout=args.timeout,
        force=args.force,
        dry_run=args.dry_run,
        fallback_on_error=args.fallback_on_error,
        mode=mode,
        workers=args.workers,
    )

if __name__ == "__main__":
    sys.exit(main())
