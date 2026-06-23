#!/usr/bin/env python3
import json
import sys
import os
import argparse
import concurrent.futures
from pathlib import Path

# Add script directory to path to import local modules
sys.path.append(str(Path(__file__).parent))
from common_subagent import run_subagent
from build_reference_database import pool_papers
from bibtex_format import assign_bibtex_keys
import urllib.request
import urllib.error

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

def ensure_key(paper: dict, used_keys: set) -> str:
    if paper.get("bibtex_key"):
        return paper["bibtex_key"]
    assign_bibtex_keys([paper])
    base_key = paper["bibtex_key"]
    key = base_key
    suffix_index = 0
    while key in used_keys:
        key = f"{base_key}{chr(ord('a') + suffix_index)}"
        suffix_index += 1
    paper["bibtex_key"] = key
    used_keys.add(key)
    return key

def download_paper(args):
    paper, key, workspace = args
    pdf_path = workspace / "reference_database" / "papers" / f"{key}.pdf"
    if pdf_path.exists():
        return key, True, "Already exists"

    title = paper.get("title", "Unknown")
    authors = paper.get("authors", [])
    author_names = ", ".join(a.get("name", "") for a in authors) if authors else "Unknown"

    urls = candidate_urls(paper)
    if urls:
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        for source, url in urls:
            ok, msg = fetch_pdf(url, pdf_path, timeout=45, user_agent="PaperOrchestra literature-review-agent")
            if ok:
                return key, True, f"Downloaded from metadata {source} URL: {url}"
    
    return key, False, "No valid URLs in metadata or download failed."

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pool", required=True)
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args()

    pool_path = Path(args.pool).resolve()
    workspace = pool_path.parent
    
    with open(pool_path, "r", encoding="utf-8") as f:
        pool_data = json.load(f)
        
    papers = pool_papers(pool_data)
    used_keys = {p["bibtex_key"] for p in papers if p.get("bibtex_key")}
    
    # Ensure reference database papers dir exists
    (workspace / "reference_database" / "papers").mkdir(parents=True, exist_ok=True)
    
    missing = []
    for paper in papers:
        key = ensure_key(paper, used_keys)
        pdf_path = workspace / "reference_database" / "papers" / f"{key}.pdf"
        if not pdf_path.exists():
            missing.append((paper, key, workspace))
            
    print(f"Found {len(missing)} missing PDFs out of {len(papers)} total papers.")
    if not missing:
        return 0

    success_count = 0
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        for key, success, msg in executor.map(download_paper, missing):
            results.append((key, success, msg))
            print(f"[{key}] {msg}")
            if success:
                success_count += 1
                
    print(f"\nCompleted. Downloaded {success_count}/{len(missing)} missing PDFs.")
    
    if success_count < len(missing):
        print(f"WARN: {len(missing) - success_count} PDFs could not be downloaded by the agent.")
        failed_keys = {missing_tuple[1] for missing_tuple in missing if missing_tuple[1] not in [k for k, s, m in results if s]}
        failed_papers = [missing_tuple[0] for missing_tuple in missing if missing_tuple[1] in failed_keys]
        failed_log = workspace / "failed_downloads.json"
        failed_log.write_text(json.dumps(failed_papers, indent=2, ensure_ascii=False))
        print(f"Failed papers logged to {failed_log}")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
