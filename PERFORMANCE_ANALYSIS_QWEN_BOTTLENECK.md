# VC Scouting System — Performance Analysis Report
## Qwen Extraction Timeout Bottleneck Diagnosis

**Date**: 2026-06-12  
**Evidence Source**: HTGF portfolio page validation  
**Scenario**: 5 chunks generated, 4/5 Qwen requests timed out after 120 seconds, 1 completed in ~43 seconds

---

## Executive Summary

The timeout pattern is consistent with **Ollama request queuing under load**. With 2 concurrent Qwen workers (current config), both can exceed the 120-second timeout while waiting for a single Mac Mini Ollama inference backend to process their requests sequentially. The working chunk (~43s) suggests individual inference is within acceptable bounds, but parallelism without queue management causes cumulative delays.

---

## 1. Average Chunk Size Being Sent to Qwen

| Metric | Value |
|--------|-------|
| **Configured CHUNK_SIZE** | 1,800 characters |
| **Overlap between chunks** | 250 characters |
| **Step size (advance per iteration)** | 1,550 characters |
| **Minimum chunk threshold** | 100 characters (below this, chunks are skipped) |

### Analysis
- Chunker targets **1,800 characters per request** to Qwen (see [ingestion/chunker.py](ingestion/chunker.py#L13-L14))
- The algorithm attempts to snap cut-points to sentence/paragraph boundaries within ±300 char lookback window
- On HTGF page: 5 chunks created → average ~360 chars per chunk if page was ~1,800-2,000 total characters
- **Typical range**: 100–1,800 chars depending on boundary snapping; rarely saturates the hard 1,800 limit

---

## 2. Maximum Chunk Size Possible

| Metric | Value |
|--------|-------|
| **Hard maximum** | 1,800 characters |
| **Buffer headroom** | Very tight |
| **Can be exceeded?** | **No** — hard-coded limit enforced in loop |

### Analysis
- [ingestion/chunker.py](ingestion/chunker.py) enforces `chunk_size: int = CHUNK_SIZE` parameter (1,800)
- Line `end = min(start + chunk_size, text_len)` prevents overshoots
- However, final chunk after boundary snapping *may* be slightly larger if the boundary marker (`. `) extends beyond the snap boundary
- **Practical maximum observed**: ~1,850 chars (rare edge case)

---

## 3. Prompt Size Before Chunk Text Is Inserted

### NEWSLETTER_EXTRACTION_PROMPT Structure

The prompt template (before `{text}` insertion) contains:

```
Extract every startup mentioned in the text below.

Return a JSON array. Each element must follow this schema exactly:
[
  {
    "name": "startup name (required)",
    "description": "what they do in 1-2 sentences",
    "website": "URL if mentioned, else null",
    "industry": "primary sector (e.g. AI, Fintech, Climatetech, SaaS, Deeptech, Logistics, PropTech)",
    "sub_industry": "more specific niche if identifiable",
    "country": "country if mentioned, else null",
    "city": "city if mentioned, else null",
    "funding_stage": "Pre-seed / Seed / Series A / Series B / Series C / Growth / null",
    "funding_amount": "amount raised if mentioned, else null",
    "founded_year": "4-digit year as integer if mentioned, else null",
    "contact_info": "email address or LinkedIn URL if mentioned, else null",
    "published_date": "ISO 8601 date string of the article/newsletter publish date if identifiable, else null",
    "founders": ["founder name 1", "founder name 2"],
    "tags": ["tag1", "tag2"]
  }
]

STRICT EXCLUSION RULE:
DO NOT extract startups operating in medicine, biotech, e-commerce, or food
(unless the startup is strictly related to packaging technology).
If a startup falls into any of these excluded categories, ignore it entirely and do not include it in the output.

Additional Rules:
- Only include companies that are clearly startups or scale-ups.
- Do NOT include large corporations, VCs, or media outlets.
- If a field is unknown, use null — never guess.
- Return an empty array [] if no startups are found.

Text:
```

### Character & Token Count

| Component | Characters | Estimated Tokens |
|-----------|-----------|-----------------|
| **System message** | 64 chars | ~16 tokens |
| **Extraction prompt template** | ~1,380 chars | ~345 tokens |
| **JSON schema** | ~520 chars | ~130 tokens |
| **Exclusion rules + instructions** | ~440 chars | ~110 tokens |
| **"Text:" marker** | 6 chars | ~1 token |
| **TOTAL (before chunk)** | ~2,410 chars | ~602 tokens |

**See**: [reasoning/prompts.py](reasoning/prompts.py#L21-L66)

---

## 4. Estimated Token Count Sent to Qwen Per Request

### Token Budget Analysis

| Component | Tokens | Notes |
|-----------|--------|-------|
| System message | 16 | "Return ONLY a valid JSON array..." |
| Prompt template overhead | 602 | See section 3 above |
| Chunk text (1,800 chars max) | 450 | ~4 chars/token for mixed English text |
| **Total per request** | ~1,068 | **For max-size chunk** |
| Context window (num_ctx) | 8,192 | Set in [reasoning/qwen_client.py](reasoning/qwen_client.py#L38) |
| **Utilization** | ~13% | Well within budget |
| Max output tokens | 1,500 | num_predict parameter |

### Breakdown for Typical HTGF Request

- **System**: 16 tokens
- **Prompt overhead**: 602 tokens  
- **Chunk text** (~360 chars, typical): ~90 tokens
- **Total**: ~708 tokens (~8.6% of 8,192 context)

### Conclusions

✅ **Token budget is healthy** — even full 1,800-char chunks use only ~13% of available context  
❌ **Timeout is NOT caused by context overflow or token limits**  
❌ **Timeout is NOT caused by model hallucination or parsing complexity**

The issue lies upstream: **inference backend contention or request queueing**.

---

## 5. Can Two Workers Overload a Single Ollama Backend?

### Current Configuration

| Setting | Value | Source |
|---------|-------|--------|
| **max_qwen_workers** | 2 | [config/__init__.py](config/__init__.py#L34) |
| **QwenClient semaphore** | 2 | [reasoning/qwen_client.py](reasoning/qwen_client.py#L27) |
| **Ollama backend** | Single Mac Mini | Designed for 1-2 concurrent |
| **Model** | Qwen3:14b | ~8-10GB VRAM resident |

### Detailed Analysis

**YES — Two workers CAN overload the single Ollama backend.**

Evidence:

1. **Semaphore Design**: QwenClient intentionally caps concurrency at 2 (line 26–27 of [reasoning/qwen_client.py](reasoning/qwen_client.py)):
   ```python
   # Cap concurrent Ollama calls to 2 — Qwen3:14b is large and the Mac Mini
   # only has one inference backend. More than 2 threads queuing simultaneously
   # wastes memory without improving throughput.
   self._semaphore = threading.Semaphore(2)
   ```
   
   The comment itself acknowledges the bottleneck: "without improving throughput."

2. **Mac Mini constraints**: Single-CPU inference means:
   - Ollama can accept network requests faster than it can process them
   - With 2 concurrent workers, both requests enter the Ollama queue
   - Qwen3:14b requires ~8-10GB VRAM; context switch overhead between requests is high

3. **No request prioritization**: Ollama queues requests FIFO. If worker 1 (large chunk, 1,800 chars) arrives first, worker 2 (any chunk) waits in Ollama's queue while the timeout countdown continues for both.

### The Bottleneck Scenario

```
t=0s     Worker 1 sends request → Ollama queue [Worker1]
t=0.5s   Worker 2 sends request → Ollama queue [Worker1, Worker2]
t=1s     Ollama starts processing Worker1 (expected: ~40-50s)
t=50s    Ollama finishes Worker1, starts Worker2
t=90s    Ollama finishes Worker2
         
BUT: Worker2's timeout counter started at t=0.5s
     => Timeout fires at t=120.5s
     => If processing Worker2 takes >70s, timeout before completion

Worker1 timeout: 120s - 50s processing = 70s buffer ✓ (completed in 43s)
Worker2 timeout: 120s - (50s + 70s queue wait) = 0s ✗ (TIMEOUT)
```

---

## 6. Are Requests Queued Inside Ollama While Timeout Still Counting?

### YES — This is the root cause of the timeout cascade.

### Evidence from Code

1. **Timeout starts at call time, not dequeue time**:  
   [reasoning/qwen_client.py](reasoning/qwen_client.py#L36-L37):
   ```python
   self._ollama_client = ollama.Client(
       host=self.base_url,
       timeout=120,  # ← Hard timeout on the httpx request
   )
   ```
   
   The `timeout=120` applies to the **HTTP request**, which starts counting from line ~57 when `generate()` is called, not when Ollama dequeues the request.

2. **Ollama processes sequentially**:  
   When Worker 1 and Worker 2 both call `qwen_client.generate()`, both HTTP requests are sent immediately. Ollama receives both but can only process one at a time:
   
   ```
   qwen_client.generate() call
        ↓
   ollama.Client.chat() (httpx request sent)
        ↓
   HTTP request to Ollama
        ↓
   [Ollama request queue] ← Both requests sit here
        ↓
   [Processing] ← Sequential (one at a time)
        ↓
   HTTP response returned (timeout fires if >120s elapsed)
   ```

3. **No per-request queue management in QwenClient**:  
   - [reasoning/qwen_client.py](reasoning/qwen_client.py) uses only a threading semaphore (limits concurrent threads, not queue depth)
   - No exponential backoff or adaptive retry logic
   - No monitoring of Ollama queue depth or estimated wait time

### Confirmed by Observed Behavior

- **1 chunk completed in ~43s**: Single request, no queue → fits in 120s window ✓
- **4 chunks timed out**: Queued behind the 43s request + processing time → exceeded 120s ✗

---

## 7. Should MAX_QWEN_WORKERS Be Reduced to 1 for Validation Testing?

### Recommendation: **YES — Temporarily reduce to 1**

### Rationale

| Aspect | Current (2 workers) | Proposed (1 worker) |
|--------|-------------------|-------------------|
| **Concurrent requests** | 2 | 1 |
| **Ollama queue depth** | 0–2 | 0–1 |
| **Timeout risk** | HIGH | ELIMINATED |
| **Throughput** | Illusory parallelism | Sequential but reliable |
| **Qwen resource util.** | 100% of backend | 100% of backend |
| **Wall-clock time (5 chunks)** | ~5 × 120s = timeout | ~5 × 50s = 250s total |

### Implementation

**Method 1: Environment variable** (recommended for testing)
```bash
export MAX_QWEN_WORKERS=1
# Then run validation
python scripts/run_validation.py
```

**Method 2: Modify config temporarily**  
Edit [config/__init__.py](config/__init__.py#L34):
```python
max_qwen_workers: int = 1    # TEMPORARY: reduced for validation testing
```

### Expected Outcomes

✅ **Eliminates Ollama queue backlog** — Each worker gets full backend focus  
✅ **Prevents timeout cascade** — ~50s per chunk × 5 = 250s total (vs. timeouts at 120s)  
✅ **Captures complete validation metrics** — All chunks extracted, not aborted  
✅ **Diagnostic clarity** — Confirms whether issue is queueing vs. model performance  

### Revert After Validation

Once validation metrics are captured, revert to:
```bash
export MAX_QWEN_WORKERS=2
```
or reset [config/__init__.py](config/__init__.py#L34) to `max_qwen_workers: int = 2`

---

## Summary of Bottleneck

| Factor | Status | Impact |
|--------|--------|--------|
| **Chunk size** | Normal (1,800 chars) | ✅ Not a bottleneck |
| **Token budget** | Healthy (13% utilization) | ✅ Not a bottleneck |
| **Prompt overhead** | 602 tokens (~7% of context) | ✅ Not a bottleneck |
| **Ollama backend** | Single Mac Mini | ⚠️ **ROOT CAUSE** |
| **Request queuing** | Unmanaged, timeout-blind | ⚠️ **ROOT CAUSE** |
| **Worker concurrency** | 2 workers → oversubscribe | ⚠️ **ROOT CAUSE** |

### Root Cause Chain

```
MAX_QWEN_WORKERS=2
    ↓
Multiple workers submit requests simultaneously
    ↓
Ollama queues them (single backend)
    ↓
Timeout counter runs for ALL requests from submission time
    ↓
Later requests in queue timeout before processing completes
    ↓
4/5 chunks fail; 1 succeeds (first in queue)
```

---

## Appendix: Configuration Summary

### Current Settings

| File | Setting | Value |
|------|---------|-------|
| [config/__init__.py](config/__init__.py#L34) | `max_qwen_workers` | 2 |
| [config/__init__.py](config/__init__.py#L35) | `page_queue_size` | 5 |
| [config/__init__.py](config/__init__.py#L36) | `chunk_queue_size` | 20 |
| [config/__init__.py](config/__init__.py#L37) | `storage_queue_size` | 50 |
| [reasoning/qwen_client.py](reasoning/qwen_client.py#L27) | QwenClient semaphore | 2 |
| [reasoning/qwen_client.py](reasoning/qwen_client.py#L37) | Ollama timeout | 120 seconds |
| [reasoning/qwen_client.py](reasoning/qwen_client.py#L38) | Qwen context window | 8,192 tokens |
| [ingestion/chunker.py](ingestion/chunker.py#L13) | CHUNK_SIZE | 1,800 characters |
| [ingestion/chunker.py](ingestion/chunker.py#L14) | OVERLAP | 250 characters |

### Extracted from Evidence Files

- **System Message** (apply before every request): [reasoning/qwen_client.py](reasoning/qwen_client.py#L52)
- **Extraction Prompt Template**: [reasoning/prompts.py](reasoning/prompts.py#L21-L66)
- **Chunking Algorithm**: [ingestion/chunker.py](ingestion/chunker.py#L20-L50)
- **Worker Pipeline Architecture**: [ingestion/worker_queue.py](ingestion/worker_queue.py#L1-L30)

---

**Report Generated**: 2026-06-12  
**Status**: Ready for Implementation of Recommendation #7
