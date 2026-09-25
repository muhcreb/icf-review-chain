# ICF Review Chain — Standalone CLI

Standalone Python CLI for REB (Research Ethics Board) informed consent form (ICF) review.
Runs Agent 1 (Pre-Review) → Agent 2 (Consistency Auditor) → Unified Report.

**Advisory only — not an REB determination. Human REB makes final decisions.**

## What This Tool Does

1. **Agent 1 (Pre-Review):** Reads ICF + Protocol, flags ethical/compliance issues,
   ranks severity (HIGH/MEDIUM/LOW), produces a handoff block
2. **Agent 2 (Consistency Auditor):** Verifies Agent 1 findings against the Protocol,
   bidirectional comparison, FK-GL readability scores, jargon alerts
3. **Unified Report:** Merged findings with citations, HUMAN REVIEW REQUIRED flags

## Quick Start

```bash
# 1. Create venv
python3 -m venv .venv
source .venv/bin/activate
pip install requests pypdf

# 2. Run (prompts for API key and file paths)
python3 review_chain_standalone.py

# 3. Or with arguments
python3 review_chain_standalone.py icf.pdf protocol.pdf
```

## Configuration

Set via environment variables or config file (`~/.icf-review-chain/config.json`):

```bash
export LLM_API_URL=https://api.openai.com/v1
export LLM_API_KEY=sk-your-key
export LLM_MODEL=gpt-4o
```

## Supported Providers

| Provider | LLM_API_URL | LLM_MODEL |
|---|---|---|
| OpenAI | https://api.openai.com/v1 | gpt-4o, gpt-4.1 |
| Anthropic | https://api.anthropic.com/v1 | claude-sonnet-4-20250514 |
| DeepSeek | https://api.deepseek.com/v1 | deepseek-chat |
| Ollama (local) | http://localhost:11434/v1 | llama3.1, mistral |

## Output

```
deliberations/
  review-20260925_140000/
    agent1_output.md
    agent2_output.md
    unified_report.md
```

## Verify .exe Integrity

```bash
python3 verify-checksums.py
```

## Windows .exe

Pre-built: `dist/icf-review-chain.exe` (6.8MB)

Build from source:
```bash
pip install pyinstaller
pyinstaller --onefile --name icf-review-chain review_chain_standalone.py
```

## Disclaimer

This tool is for advisory purposes only. It does not replace human REB review
or constitute an ethical determination. All findings should be reviewed by a
qualified REB member before any decision.

## Regulatory Framework

- TCPS 2 (2022) — research ethics
- Civil Code of Québec arts. 10–25 — consent capacity
- Privacy law: R-22.1 (health info), A-2.1 (public-body records), P-39.1 (private sector only)

## Privacy

This tool sends ICF/Protocol text to an LLM API provider. Ensure your provider
has a BAA (Business Associate Agreement) if handling PHI/PII. Do not upload
patient-identifiable documents without proper safeguards.
