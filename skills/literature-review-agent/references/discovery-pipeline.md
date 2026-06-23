# Discovery Pipeline (Phase 1 + Phase 2)

Source: arXiv:2604.05018, App. D.3 ("Citation Verification") and App. B
(LLM-call distribution).

## Phase 1 — Parallel Candidate Discovery

The paper uses 10 concurrent workers to fan out search-grounded LLM calls
("Gemini-3-Flash with Google Search grounding"). For our host-agent
implementation, the equivalent is: spawn up to 10 concurrent search queries
using the host's native web search tool.

### Inputs

From `outline.json`:

```
introduction_strategy:
  search_directions: [q1, q2, q3]              # 3-5 queries
related_work_strategy:
  subsections:
    - methodology_cluster: "..."
      sota_investigation_mission: "..."        # 1 derived query
      limitation_search_queries: [q4, q5]      # 1-3 queries
    - ...
```

Total query budget: typically 10-20 queries per paper.

### Per-query procedure

For each search query, instruct your host's search tool:

```
search("<query>", num_results=10)
```

Or, if you've enabled the optional Exa backend (see `exa-search-cookbook.md`):

```bash
python scripts/exa_search.py --query "<query>" --num-results 10
```

Both paths produce the same normalized candidate format. Collect the top
10 results per query. Each result should yield:

- `title` — the paper's title from the search snippet
- `snippet` — the abstract preview from the search snippet
- `source_url` — the result URL (often the arXiv abstract page)

Tag each result with `discovered_for: ["intro"]` or
`discovered_for: ["related_work[2.1]"]` so you can later trace which cluster
each citation supports.

Combine all results across all queries into a single `raw_candidates.json`:

```json
{
  "candidates": [
    {
      "title": "Attention Is All You Need",
      "snippet": "The dominant sequence transduction models...",
      "source_url": "https://arxiv.org/abs/1706.03762",
      "discovered_for": ["intro"]
    },
    ...
  ]
}
```

### Pre-dedup before Phase 2

**Always run this before starting Phase 2.** Multiple search queries routinely
return the same papers. Verifying duplicates wastes 30-40% of S2 quota at
1 QPS.

```bash
python scripts/pre_dedup_candidates.py \
    --in workspace/raw_candidates.json \
    --out workspace/deduped_candidates.json
```

Use `workspace/deduped_candidates.json` as input to Phase 2.

## Phase 2 — Sequential Verification via Semantic Scholar

The paper enforces strict sequential verification at ≤1 QPS via the public
Semantic Scholar API. We follow the same constraint.

### S2 response cache

Before each live S2 request, check the local cache to avoid redundant calls:

```bash
# Check cache first (no S2 call, no throttle needed):
python scripts/s2_cache.py --cache workspace/cache/s2_cache.json --check "<title>"
# exit 0 + prints JSON → use cached response, skip live request
# exit 1 → proceed to live request below
```

After every successful live S2 request, store the response:

```bash
python scripts/s2_cache.py --cache workspace/cache/s2_cache.json \
    --store "<title>" --response '<full S2 JSON>'
```

### Per-candidate procedure

1. **Search S2 by title**. Use the bundled helper or the host's URL fetch tool:
   ```
   GET https://api.semanticscholar.org/graph/v1/paper/search
       ?query=<URL-encoded(title)>
       &limit=5
       &fields=title,abstract,year,authors,venue,externalIds,openAccessPdf
   ```
   No API key required for the public endpoint. Be polite: 1 QPS.

2. **Take the top hit**. Compare `title` to the candidate `title` via the
    helper:
    ```bash
    python scripts/levenshtein_match.py --candidate "..." --found "..."
    ```
    The helper prints `<ratio> PASS` or `<ratio> FAIL` (e.g., `85 PASS`).
    - **≤ 70 → discard the candidate.** Move on.
    - **> 70 → continue to checks 3-5.**

3. **Year-alignment bonus**. If the candidate's `discovered_for` query
    mentioned a specific year and the S2 hit's year matches exactly, record
    `match_score = ratio + 5`. (This is a soft bonus used for tie-breaking
    when two candidates dedup to similar entries.)

4. **Check abstract presence**. If `abstract` is null or empty → discard.
    The paper requires every cited entity to have a retrievable abstract for
    downstream context enrichment in the Section Writing Agent.

5. **Check temporal cutoff**:
    ```bash
    python scripts/check_cutoff.py \
        --paper-year <year> \
        --paper-month <month or omit> \
        --cutoff <YYYY-MM-DD>
    ```
    Exit 0 if strictly predates; exit 1 if not. Discard on exit 1.

6. **Append to verified pool** if all checks pass. Record:
    ```json
    {
      "paperId": "abc123...",
      "title": "...",
      "abstract": "...",
      "year": 2017,
      "venue": "NeurIPS",
      "authors": [{"name": "A. Vaswani"}, ...],
      "externalIds": {"DOI": "...", "ArXiv": "1706.03762"},
      "openAccessPdf": {"url": "..."},
      "match_score": 100,
      "discovered_for": ["intro"]
    }
    ```

### Rate-limit etiquette

The S2 public endpoint enforces ~1 QPS without an API key. If you receive
HTTP 429, sleep 5 seconds and retry. Do not parallelize Phase 2 — verification
must be strictly sequential.

If your host has the patience for it, the paper measures ~20-30 LLM/API calls
total per Lit Review Agent invocation. With ~30 candidates that's roughly
30 seconds of verification wall-time. With 100 candidates it's ~100 seconds.

## Why two phases

The split exists because:

- **Discovery is high-throughput, low-stakes**. You want to cast a wide net
  fast. Search APIs accept high concurrency.
- **Verification is low-throughput, high-stakes**. The S2 API protects
  itself with QPS limits, and the verification step is what keeps the paper
  honest. Faking a citation is trivially easy without it.

The paper's design "successfully combines the high-concurrency tolerance of
the LLM API with the strict throughput limits of the Semantic Scholar API to
prevent quota-induced latency" (App. B).
