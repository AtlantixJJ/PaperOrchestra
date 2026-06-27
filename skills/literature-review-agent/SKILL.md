---
name: literature-review-agent
description: Step 3 of the PaperOrchestra pipeline (arXiv:2604.05018). Execute the literature search strategy from outline.json — discover candidate papers via web search, verify them through Semantic Scholar (Levenshtein strictly > 70 fuzzy title match, temporal cutoff, dedup by paperId), build a BibTeX file, and draft Introduction + Related Work using ≥90% of the verified pool. Runs in parallel with the plotting-agent. TRIGGER when the orchestrator delegates Step 3 or when the user asks to "find citations for my paper", "draft the related work", or "build the bibliography".
---

# Literature Review Agent (Step 3)

Faithful implementation of the Hybrid Literature Agent from PaperOrchestra
(Song et al., 2026, arXiv:2604.05018, §4 Step 3, App. D.3, App. F.1 p.46).

**Cost: ~20–30 LLM calls.** This is one of the two longest steps (the other is
plotting). Wall-time floor is set by Semantic Scholar's 1 query per second verification
limit.

## Inputs

- `workspace/outline.json` — specifically `intro_related_work_plan` with the
  Introduction search directions and the 2-4 Related Work methodology
  clusters
- `workspace/inputs/conference_guidelines.md` — used to derive `cutoff_date`
- `workspace/inputs/idea.md` and all `.md` files under `workspace/inputs/experiments/` — for
  framing the Intro and grounding the Related Work positioning (read all files in `experiments/`, concatenate in filename-sorted order)

## Outputs

- `workspace/citation_pool.json` — verified Semantic Scholar metadata for
  every paper that survived verification
- `workspace/refs.bib` — BibTeX file generated from the verified pool
- `workspace/drafts/intro_relwork.tex` — drafted Introduction and Related
  Work sections, written into the template, with the rest of the template
  preserved verbatim
- Reference database:
  - `workspace/reference_database/papers/*.pdf` — downloaded open-access PDFs
  - `workspace/reference_database/summaries/*.md` — structured Markdown summaries
  - `workspace/reference_database/index.json` — synchronized paper index

## Two-phase pipeline (App. D.3)

```
PHASE 1 — Parallel Candidate Discovery
   For each search direction in introduction_strategy.search_directions:
   For each limitation_search_query in each related_work cluster:
     - Use the host's web search tool to discover up to ~10 candidate papers.
     - Run up to 10 discovery queries in parallel (host-permitting).
     - Collect (title, snippet, url) tuples — no verification yet.
   → PRE-DEDUP before Phase 2 (see Step 1.5 below)

PHASE 2 — Sequential Citation Verification (1 query per second, with cache)
   For each candidate (after pre-dedup), sequentially:
     - Check cache first (no throttle on HIT).
     - Query Semantic Scholar by title (1 query per second on live request).
     - Store the S2 response in cache.
     - Verify title match, non-empty abstract, and temporal cutoff.
     - Add to verified pool if all checks pass.
   After all candidates are verified, dedup by Semantic Scholar paperId.
```

The host agent does the LLM/web work; the deterministic helpers in `scripts/`
do the math.

## Step-by-step

### 0. Derive `cutoff_date`

Parse `conference_guidelines.md` for the submission deadline. The paper aligns
research cutoff with venue submission deadline (App. D.1):

| Venue | Cutoff |
|---|---|
| CVPR 2025 | Nov 2024 |
| ICLR 2025 | Oct 2024 |
| Other | One month before the stated submission deadline |

Encode as `YYYY-MM-DD`. Months default to day-1 (e.g., `2024-10-01`).

### 1. Phase 1: Parallel Candidate Discovery

From `outline.json`:

- All `introduction_strategy.search_directions` (3-5 queries)
- For each cluster in `related_work_strategy.subsections`:
  - The cluster's `sota_investigation_mission` becomes a search query
  - All `limitation_search_queries` (1-3 each)

For each query, **use your host's web search tool** (e.g., `WebSearch` in
Claude Code, `@web` in Cursor, the search tool in Antigravity). Collect the
top ~10 candidates per query: title, abstract snippet, source URL.

If your host supports parallel sub-tasks, fire up to 10 concurrent search
queries. If not, run sequentially — slower but functionally equivalent.

#### Optional: Exa as a Phase 1 backend

If your host has no native web search, OR you want a research-paper-focused
backend with better signal-to-noise, you can use [Exa](https://exa.ai) via
the bundled `scripts/exa_search.py` helper. It is **opt-in** and reads
`EXA_API_KEY` from the environment — the repo never commits a key.

```bash
export EXA_API_KEY="your-key-here"   # get one at https://dashboard.exa.ai/
python skills/literature-review-agent/scripts/exa_search.py \
    --query "Sparse attention long context transformers" \
    --num-results 15 \
    --discovered-for "related_work[2.1]"
```

Output is a normalized candidate list ready to merge into
`raw_candidates.json`. Phase 2 verification (Semantic Scholar fuzzy match,
cutoff, dedup) is unchanged. See `references/exa-search-cookbook.md` for
the full recipe, query patterns, cost estimates, and security notes.

Combine all discovered candidates into a single working list. Tag each with
the originating query ID so you can later attribute it to "intro" vs
"related_work[i]".

### 1.5. Pre-dedup before Phase 2

**Always run this before starting Phase 2.** Multiple search queries routinely
return the same papers (e.g., "Attention is All You Need" appears in almost
every NLP discovery query). Verifying duplicates wastes 30-40% of S2 quota
at 1 query per second.

```bash
python skills/literature-review-agent/scripts/pre_dedup_candidates.py \
    --in workspace/raw_candidates.json \
    --out workspace/deduped_candidates.json
# Prints: "150 candidates → 97 unique (53 duplicates removed)"
```

Use `workspace/deduped_candidates.json` as input to Phase 2.

### 2. Phase 2: Sequential Verification via Semantic Scholar (with cache)

For each candidate in `deduped_candidates.json`, in **sequential** order:

**Step A — check cache first** (no S2 call, no throttle needed):
```bash
python skills/literature-review-agent/scripts/s2_cache.py \
    --cache workspace/cache/s2_cache.json \
    --check "<candidate title>"
# exit 0 + prints JSON → use cached response, skip Step B
# exit 1 → proceed to Step B
```

**Step B — live S2 request** (cache MISS only, throttle to 1 query per second):

**Preferred:** use the bundled `scripts/s2_search.py` helper — it handles
auth, retries, and 429 back-off automatically:

```bash
python skills/literature-review-agent/scripts/s2_search.py \
    --query "<URL-decoded candidate title>" --limit 5 --env .env
# If SEMANTIC_SCHOLAR_API_KEY is set the key is forwarded automatically.
# If not, the public unauthenticated endpoint is used (≤1 query per second, still works).
```

Check whether the key is configured before starting Phase 2:

```bash
python skills/literature-review-agent/scripts/s2_search.py --check-key --env .env
```

**Fallback:** if you prefer your host's URL fetch tool, GET:
```
https://api.semanticscholar.org/graph/v1/paper/search?query=<URL-encoded title>&limit=5&fields=title,abstract,year,authors,venue,externalIds,openAccessPdf
```
Add header `x-api-key: <SEMANTIC_SCHOLAR_API_KEY>` if the env var is set.
Be polite: ≤1 request per second for live requests. Cache hits are free.

**Step C — store in cache** (after every successful live request):
```bash
python skills/literature-review-agent/scripts/s2_cache.py \
    --cache workspace/cache/s2_cache.json \
    --store "<candidate title>" \
    --response '<full S2 JSON response>'
```

For the top hit:

```bash
python skills/literature-review-agent/scripts/levenshtein_match.py \
    --candidate "Original candidate title" \
    --found "S2 returned title" \
    --substring-bypass
# prints "<ratio> PASS" or "<ratio> FAIL" (e.g., "85 PASS"). Discard if ≤ 70.
```

Then check the temporal cutoff:

```bash
python skills/literature-review-agent/scripts/check_cutoff.py \
    --paper-year 2024 \
    --paper-month 9 \
    --cutoff 2024-10-01
# exit 0 if strictly predates, exit 1 if not
```

If both checks pass AND the abstract is non-empty, append the paper's full
S2 metadata to the verified pool.

### 3. Dedup and assemble the pool

After all candidates are verified:

```bash
python skills/literature-review-agent/scripts/dedupe_by_id.py \
    --in workspace/raw_pool.json \
    --out workspace/citation_pool.json
```

The dedupe script keys on `paperId` (Semantic Scholar's internal unique ID),
falling back to `externalIds.DOI`, then `externalIds.ArXiv`, then a
normalized title.

The script also computes and writes `min_cite_paper_count` =
`floor(0.9 * len(papers))` — the minimum number of papers the writing step
must cite (the paper's ≥90% integration rule, App. D.3).

**Immediately after dedupe_by_id.py**, validate and auto-fix the pool schema:

```bash
python skills/literature-review-agent/scripts/validate_pool.py \
    --pool workspace/citation_pool.json --fix
# Catches and fixes authors-as-strings, reports missing required fields.
# Must pass before proceeding to Step 4.
```

### 4. Download PDFs and Build the Reference Database

The literature review step MUST produce a local reference-paper database before
building `refs.bib`. This database is the source of detailed paper summaries
used by the writing step and by later manual review.

First, run the automated script to download PDFs from available links:

```bash
python skills/literature-review-agent/scripts/download_reference_pdfs.py \
    --pool workspace/citation_pool.json
```

If any papers fail to download, the script will create `workspace/failed_downloads.json`. You (the host agent) MUST NOT ignore this — for any missing PDFs listed in that file, search online, find the PDFs, and download them manually into `workspace/reference_database/papers/<bibtex_key>.pdf`.

After verifying that all papers have a valid PDF, run the batch enrichment script to
generate Markdown summaries and update the index. This wrapper only reads the PDFs and
calls a summarization backend — it never looks papers up online.

**Backend policy: prefer `llm`, fall back to `agent` (`agy`).** The script defaults to
`--mode llm`, which summarizes via a direct `litellm` API call — this is faster, cheaper,
and the preferred path. Only fall back to the agent backend for summaries the LLM path
could not produce.

**First pass — LLM (preferred):**

```bash
python -u skills/literature-review-agent/scripts/build_reference_database.py \
    --pool workspace/citation_pool.json
```

This requires `LITELLM_API_KEY` (and optionally `LITELLM_BASE_URL` / `LITELLM_TEXT_MODEL`)
in the environment or in a `.env` the script can find.

**Second pass — agent fallback, only if the LLM pass left summaries missing/corrupt:**
re-running the script regenerates *only* the summaries the maintenance check still flags as
`NEEDS_REGEN`, so this re-does just the LLM failures, now via the `agy` CLI subagent:

```bash
python -u skills/literature-review-agent/scripts/build_reference_database.py \
    --pool workspace/citation_pool.json --mode agent --summary-command "agy"
```

> Note: `--summary-command` is only honored when `--mode agent`. Passing it under the
> default `--mode llm` has no effect — the LLM backend is selected by the `LITELLM_*` env.

Build or check the synchronized index:

```bash
python skills/literature-review-agent/scripts/build_reference_database.py --pool workspace/citation_pool.json --maintain-only
```

**CRITICAL GUARDRAIL:** Do NOT proceed to Step 5 (BibTeX generation) until `build_reference_database.py --maintain-only` exits with 0 errors. The maintenance check fails when:

- a paper in `citation_pool.json` has no summary,
- a summary has no matching paper,
- a summary frontmatter `bibtex_key` does not match its canonical key,
- a summary frontmatter `summary_status` is not `complete`,
- `index.json` is missing or stale.

If it reports `NEEDS_REGEN`, re-run enrichment — first retry the LLM pass, then fall back to `--mode agent --summary-command "agy"` for any summaries that still fail.

The index contains one row per verified paper:

```text
bibtex_key,title,location,year,summary,status
```

### 5. Build the BibTeX file

After the reference database is synchronized, generate `refs.bib`:

```bash
python skills/literature-review-agent/scripts/bibtex_format.py \
    --pool workspace/citation_pool.json \
    --out workspace/refs.bib
```

The script assigns canonical BibTeX keys and writes out only `@article` /
`@inproceedings` / `@misc` entries — never invents fields. It also writes the
canonical `bibtex_key` back into each paper record in `citation_pool.json`.

The required pipeline is now:

```
dedupe_by_id → validate_pool --fix → download_reference_pdfs → (manual agent fallback for missing PDFs) → build_reference_database → build_reference_database --maintain-only → bibtex_format
```

### 6. Draft Introduction + Related Work

This is where you (the host agent) actually write text. Load the
**verbatim Literature Review Agent prompt** at `references/prompt.md`.
Substitute the template placeholders:

| Placeholder | Value |
|---|---|
| `intro_related_work_plan` | full JSON object from `outline.json` |
| `project_idea` | contents of `idea.md` |
| `project_experimental_log` | concatenated contents of all `.md` files in `experiments/` (filename-sorted) |
| `citation_checklist` | the BibTeX keys from `refs.bib` |
| `collected_papers` | list of `{key, title, abstract}` from `citation_pool.json` |
| `paper_count` | `len(citation_pool.papers)` |
| `min_cite_paper_count` | from `citation_pool.json` |
| `cutoff_date` | the date you derived in Step 0 |

**Also prepend the Anti-Leakage Prompt** from
`../paper-orchestra/references/anti-leakage-prompt.md`.

Run your LLM with the combined prompt against `template.tex`. The agent's
job is to fill in the empty Introduction and Related Work sections of the
template **and leave everything else untouched**. Output: the full
`template.tex` with those two sections filled. Save to
`workspace/drafts/intro_relwork.tex`.

### 7. Verify ≥90% citation coverage

First sync any generated citation keys back to the canonical BibTeX keys:

```bash
python skills/literature-review-agent/scripts/sync_keys.py \
    --pool workspace/citation_pool.json \
    --tex  workspace/drafts/intro_relwork.tex \
    --inplace
```

Then run the coverage gate:

```bash
python skills/literature-review-agent/scripts/citation_coverage.py \
    --tex workspace/drafts/intro_relwork.tex \
    --pool workspace/citation_pool.json
# exit 0 if ≥90% of pool is cited; exit 1 otherwise
```

If the gate fails, re-prompt the writing step explicitly listing the missing
keys and asking the agent to integrate them where contextually appropriate.

## Critical rules from the prompt

These are excerpted from `references/prompt.md`. The host agent MUST honor
them on the writing call:

- **Cite ONLY from `collected_papers`.** Never invent BibTeX keys, never
  reference papers not in the pool.
- **Cite at least `min_cite_paper_count` of them** in Intro + Related Work
  combined.
- **TIMELINE RULE**: Do not treat any papers published after `cutoff_date`
  as prior baselines to beat. They are concurrent work only.
- **EVALUATION RULE**: Do not claim our method beats / achieves SOTA over a
  specific cited paper UNLESS that paper is explicitly evaluated against in
  the `experiments/` files. Frame other recent papers strictly as concurrent,
  orthogonal, or conceptual work.
- **Output format**: return the full code for the updated `template.tex`,
  with the two empty sections (Introduction and Related Work) filled in,
  and **all the other code** (packages, styles, other sections) **identical
  to the original** template.tex.
- Wrap output in ```` ```latex ... ``` ```` fences.
- Do not change `\usepackage[capitalize]{cleveref}` to `cleverref` (there is
  no `cleverref.sty`).

## Degraded mode (no web search)

If your host has no web search tool, switch to degraded mode:

1. If the user has placed a pre-built `workspace/inputs/refs.bib` in the
   workspace, load it directly into `workspace/refs.bib` and skip Phase 1
   and Phase 2.
2. Otherwise, emit `workspace/drafts/intro_relwork.tex` containing the
   template with two TODO markers in the Intro and Related Work sections,
   and tell the user the pipeline cannot complete Step 3 without web search.

## Resources

- `references/prompt.md` — verbatim Literature Review Agent prompt from App. F.1
- `references/discovery-pipeline.md` — Phase 1 + Phase 2 explained in detail
- `references/verification-rules.md` — Levenshtein cutoff, year alignment, dedup
- `references/citation-density-rule.md` — the ≥90% integration rule
- `references/s2-api-cookbook.md` — Semantic Scholar URLs, fields, rate limits
- `references/exa-search-cookbook.md` — optional Exa backend for Phase 1 (research-paper-focused web search)
- `scripts/pre_dedup_candidates.py` — dedup Phase 1 candidates before Phase 2 (saves 30-40% S2 quota)
- `scripts/s2_cache.py` — persistent S2 response cache (eliminates re-verification on re-runs)
- `scripts/validate_pool.py` — validate & auto-fix citation_pool.json schema (authors format)
- `scripts/sync_keys.py` — sync cite keys in .tex with canonical bibtex_keys after drafting
- `scripts/levenshtein_match.py` — fuzzy title match (ratio strictly > 70)
- `scripts/check_cutoff.py` — date cmp w/ month → day-1 default
- `scripts/dedupe_by_id.py` — dedup verified pool by S2 paperId
- `scripts/bibtex_format.py` — build refs.bib from JSON pool
- `scripts/citation_coverage.py` — ≥90% citation coverage gate
- `scripts/s2_search.py` — Semantic Scholar title-search helper; reads `SEMANTIC_SCHOLAR_API_KEY` from env (optional — falls back to unauthenticated)
- `scripts/exa_search.py` — optional Exa Phase 1 backend (reads `EXA_API_KEY` from env)
- `scripts/build_reference_database.py` — required batch wrapper that reads the downloaded PDFs, summarizes them (default `--mode llm` via litellm; `--mode agent` falls back to a CLI subagent such as `agy`), writes Markdown summaries, and maintains the index (`--maintain-only`) for all verified papers
- `scripts/common_subagent.py` — internal module imported by `build_reference_database.py`; provides `run_subagent()` for invoking `agy` subagents
- reference-database maintenance (pool/summary alignment check + regeneration) is built into `build_reference_database.py`; run it with `--maintain-only`
- `references/summary_prompt_llm.md` — summarization prompt for `--mode llm` (extracted PDF text is injected inline)
- `references/summary_prompt_agent.md` — summarization prompt for `--mode agent` (the agy subagent is given a PDF path to open and read)
- `references/summary_template.md` — Markdown structure template for paper summaries
