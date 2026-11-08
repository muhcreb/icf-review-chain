# icf-review-chain

Chains two LLM agents to audit a research informed consent form (ICF) against its study protocol:

1. **Agent 1 (Pre-Review)** reviews the ICF for clarity, completeness, ethical risk, and Quebec/TCPS 2 requirements, and produces a handoff block.
2. **Agent 2 (Consistency Auditor)** compares the protocol with the ICF in both directions and reports each comparison item as CONSISTENT, INCONSISTENT, VAGUE, ABSENT, NON-COMPLIANT, or JARGON ALERT.

The output is a unified Markdown report. **It is advisory only. It is not an REB determination.**

## Install

```bash
pip install -r requirements.txt
```

Run from source. There is no prebuilt executable: a binary you cannot inspect has no place in a tool that reads consent documents.

## Configure

Each setting is read from the environment first, then from `~/.icf-review-chain/config.json` (use the lowercase key), then the default.

| Environment variable | Config key | Default | Meaning |
|---|---|---|---|
| `LLM_API_KEY` | `llm_api_key` | required | API key. Prefer the environment variable. If it is missing and you are at a terminal, you are asked to type it (hidden), and it is saved **only if you answer yes**, as plain text in the config file. That file is owner-only on macOS and Linux; Windows cannot restrict it, so on a shared or managed computer use the environment variable. |
| `LLM_API_URL` | `llm_api_url` | `https://api.openai.com/v1` | Any OpenAI-compatible endpoint |
| `LLM_MODEL` | `llm_model` | `gpt-4o` | Model name |
| `LLM_MAX_TOKENS` | `llm_max_tokens` | `8192` | Maximum output tokens per call |
| `LLM_TEMPERATURE` | `llm_temperature` | `0.3` | Sampling temperature |
| `LLM_MAX_INPUT_CHARS` | `llm_max_input_chars` | `300000` | Maximum characters in one request (prompt plus documents). About 4 characters per token for English. **Set this to fit your model's context window.** Models served locally often have a much smaller window than the default assumes. |
| `LLM_TIMEOUT` | `llm_timeout` | `600` | Seconds to wait for each response |

## Run

```bash
python3 review_chain_standalone.py icf.pdf protocol.pdf            # report in ./deliberations
python3 review_chain_standalone.py icf.pdf protocol.pdf ./output
cat icf.txt | python3 review_chain_standalone.py --stdin-icf - protocol.pdf   # plain-text ICF on stdin
python3 review_chain_standalone.py                                  # prompts for the file paths
python3 review_chain_standalone.py icf.pdf protocol.pdf --yes       # do not pause on input warnings
```

Inputs are PDF or plain text (a form-feed character is treated as a page break). Word files must be saved as PDF first.

## Input checks (before anything is sent)

These run first and are advisory. At a terminal, a warning pauses the run and asks whether to send the documents anyway (`--yes` skips the question; without a terminal the run continues and the warning goes in the report).

- Pages with **no extractable text**, and PDFs that are entirely image-based, are detected. Empty pages make the review `INCOMPLETE`. There is no built-in OCR: run a scanned file through an OCR tool such as OCRmyPDF first.
- Pages whose text **looks garbled** (a poor scan's hidden text layer), and documents averaging under 200 characters per page, are flagged. This is a heuristic prompt to look at the page; a table of numbers can trip it.
- **Mismatched study pair**: the ICF and protocol are compared for shared study identifiers (protocol, REB, NCT numbers) and for shared study-specific vocabulary. The identifiers found are listed in the report so you can compare them by eye. These checks are rough and can be wrong in either direction.
- Binary files with a text extension, and non-PDFs with a `.pdf` extension, are rejected.

## What it does about coverage

Nothing is silently dropped.

- **The ICF is always sent complete.** If it cannot fit in one request, the run stops *before sending anything* and says how much room is needed. Reviewing part of a consent form would give a misleading result.
- **The protocol is split on section headings** into parts that fit the request size. Agent 2 compares each part with the complete ICF, and the results are merged. Every line of the protocol lands in exactly one part.
- **Page markers** (`[[PAGE n]]`) are added to the text so citations can be checked.
- **Unreadable pages** (scans with no text layer) and password-protected PDFs are detected and reported. Run OCR on scans first.
- **Output cut off at the token limit** is detected and reported.

The report begins with `Review status: COMPLETE` or `INCOMPLETE - HUMAN REVIEW REQUIRED`, followed by any problems. The process exits with status `0` (complete), `1` (error), or `2` (finished but incomplete).

## What it verifies in code

- **Every quotation** in Agent 2's table (and every sizeable quotation in Agent 1's output) is checked against the extracted document text, ignoring case, spacing, hyphenation, and line breaks. Each is marked `VERIFIED`, `PARTIAL`, `NOT FOUND`, or `VERIFIED, PAGE MISMATCH`. `[...]` and `[REDACTED]` in a quote are treated as gaps. Critical alerts that rest on an unverified quote are labelled `[QUOTE NOT VERIFIED]` in the summary.
- **Readability** (Flesch-Kincaid grade level) is calculated in code, for the whole ICF and for each quoted ICF passage of 15 or more words. It is an English-language formula; French text is reported as `n/a`.
- **Summary counts, critical alerts, and the human-review list** are computed from the parsed table rows, not written by the model.
- Table lines that do not match the nine-column format are kept verbatim at the end of the Agent 2 section, never discarded.

## Output

Each run writes `review-<timestamp>/` containing:

- `unified_report.md` (status, coverage, Agent 1, merged Agent 2, handoff, unverified quotations)
- `agent1_output.md`, `agent2_output.md`, `agent2_parts/` (raw model output for each protocol part)
- `run_metadata.json` (model, settings, SHA-256 of both input files, every call's finish reason, problems and warnings)

If a run fails after Agent 1 (for example an authentication or context-length error), the error message names the `review-<timestamp>/` folder, which keeps Agent 1's output, any completed protocol parts, and a `run_metadata.json` with status `FAILED`. Transient errors (rate limits, timeouts, server errors) are retried three times with backoff before giving up.

## Limits

- A verified quote proves the words exist in the document. It does not prove the model interpreted them correctly. Every finding still needs a human reader.
- Findings for one protocol part are matched across parts by item name. An item described differently in two parts may appear twice; the same item reported with different statuses is flagged `CONFLICT`.
- Section headings are detected heuristically. A poor guess only changes where a part begins; it never loses text.
- A protocol too small to need splitting is sent whole. A very long protocol means many calls, so check your provider's cost and rate limits.
- **Privacy:** the text of both documents is sent to the LLM provider you configure. Confirm that this is permitted for the documents involved (institutional policy, applicable privacy law, provider terms) before running real submissions.

## Tests

```bash
pip install reportlab
python3 tests/unit_tests.py
python3 tests/e2e_tests.py
```

The end-to-end tests run the full pipeline against a local stand-in for an OpenAI-compatible server. They cover chunking, fabricated quotes, wrong page numbers, truncated output, an ICF that is too large, blank pages, retries, and authentication failure. They do not call a real model.
