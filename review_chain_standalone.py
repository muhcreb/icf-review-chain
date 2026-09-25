#!/usr/bin/env python3
"""
ICF Review Chain — Standalone Python CLI
Agent 1 (Pre-Review) → Agent 2 (Consistency Auditor) → Unified Report

Works outside Hermes — calls any OpenAI-compatible LLM API directly.
No Hermes dependency, no delegate_task needed.

Usage:
  python3 review_chain_standalone.py <icf_file> <protocol_file> [output_dir]

Environment:
  LLM_API_URL   - OpenAI-compatible API endpoint (default: https://api.openai.com/v1)
  LLM_API_KEY   - API key
  LLM_MODEL     - Model name (default: gpt-4o)
  LLM_MAX_TOKENS- Max tokens per call (default: 4096)

Examples:
  # OpenAI
  LLM_API_KEY=sk-... python3 review_chain_standalone.py icf.pdf protocol.pdf

  # Anthropic (via OpenAI-compatible proxy)
  LLM_API_URL=https://api.anthropic.com/v1 LLM_API_KEY=... LLM_MODEL=claude-sonnet-4-20250514 python3 review_chain_standalone.py icf.pdf protocol.pdf

  # Local model (via ollama or similar)
  LLM_API_URL=http://localhost:11434/v1 LLM_API_KEY=ollama LLM_MODEL=llama3.1 python3 review_chain_standalone.py icf.pdf protocol.pdf

  # DeepSeek
  LLM_API_URL=https://api.deepseek.com/v1 LLM_API_KEY=... LLM_MODEL=deepseek-chat python3 review_chain_standalone.py icf.pdf protocol.pdf
"""

import sys
import os
import re
import json
import argparse
from datetime import datetime
from pathlib import Path

import requests
from pypdf import PdfReader

# ── Configuration ──────────────────────────────────────────────

CONFIG_DIR = Path.home() / ".icf-review-chain"
CONFIG_FILE = CONFIG_DIR / "config.json"


def load_config():
    """Load saved config from config file."""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, PermissionError):
            return {}
    return {}


def save_config(config):
    """Save config to config file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2))


def get_api_key():
    """Get API key from env var, config file, or prompt."""
    # 1. Check environment
    key = os.environ.get("LLM_API_KEY", "")
    if key:
        return key

    # 2. Check config file
    config = load_config()
    key = config.get("llm_api_key", "")
    if key:
        return key

    # 3. Prompt user (only if stdin is a TTY)
    if not sys.stdin.isatty():
        print("Error: LLM_API_KEY not set and no interactive terminal.")
        print("Set it with: export LLM_API_KEY=your-key")
        print("Or save to config: edit ~/.icf-review-chain/config.json")
        sys.exit(1)

    print("LLM_API_KEY not set.")
    print("Get a key from your LLM provider (OpenAI, Anthropic, DeepSeek, etc.)")
    key = input("Enter API key: ").strip()
    if key:
        config["llm_api_key"] = key
        save_config(config)
        print(f"Key saved to {CONFIG_FILE}")
    return key


def get_model():
    """Get model from env var, config file, or default."""
    model = os.environ.get("LLM_MODEL", "")
    if model:
        return model
    config = load_config()
    return config.get("llm_model", "gpt-4o")


def get_api_url():
    """Get API URL from env var, config file, or default."""
    url = os.environ.get("LLM_API_URL", "")
    if url:
        return url
    config = load_config()
    return config.get("llm_api_url", "https://api.openai.com/v1")

# Embedded agent prompts (same as Hermes templates)
AGENT1_PROMPT = """# Consent Form Pre-Review

## Purpose
Act as a Québec Research Ethics Board consultant reviewing informed consent forms (ICFs) and participant-facing consent documents. Provide strategic, high-level feedback on participant comprehension, ethical risk, regulatory compliance, and cultural safety.

## Scope
Assess only:
- **Clarity and readability:** Grade 6–8 plain language.
- **Completeness:** TCPS 2 (2022), especially Chapter 3.
- **Ethical risks:** coercion, voluntariness, therapeutic misconception, power imbalances.
- **Purpose clarity:** whether consent covers the primary study only or also future use/data banking; when both apply, verify separate choices.
- **Cultural safety:** Indigenous research ethics, OCAP® principles, and data sovereignty when Indigenous participants, communities, knowledge, or perspectives are involved.
- **Québec legal requirements:** Civil Code of Québec arts. 10–25; privacy law applicability sequence: R-22.1 for health information, A-2.1 for public-body records, P-39.1 for private-sector entities only. Quebec jurisdiction established by MUHC REB submission.
- **Data governance:** collection, storage, retention, destruction, withdrawal, secondary use, and international transfer.
- **Participant burden:** comprehension, time, accessibility, and technology requirements.

## General Guidelines
- Do not rewrite text unless the user explicitly asks to rewrite a named section.
- Match the document language, using Canadian English or DataFranca French.
- Use a collegial, precise, REB-aware, non-directive tone.
- Make every recommendation concrete and actionable.
- Include only issues requiring changes in the findings table; do not list strengths there.
- Do not flag missing elements unless required by TCPS 2, applicable Québec law, or applicable MUHC REB requirements for the context described.
- Do not infer study type, risk level, data flows, jurisdiction, or institutional practices beyond the ICF.
- Do not flag minor typos, fonts, or layout issues as findings.

## Step-by-Step Review
1. **Establish applicability** — Identify participant population, data or biological-material type, study setting, institutions, and data-transfer locations. Quebec jurisdiction is established by MUHC REB submission; determine which legal, ethical, and institutional requirements apply before assessing compliance.
2. **Identify consent purpose** — Classify the ICF as primary-study consent, combined primary-study and future-use/data-banking consent, or unclear. When future use or data banking is included, assess whether choices are clearly separated and withdrawal limits are explained.
3. **Review participant protections** — Check voluntariness, ongoing consent, withdrawal without prejudice, capacity, therapeutic misconception, confidentiality limits, compensation, liability language, and contact information.
4. **Review data governance** — Trace collection, access, storage, retention, sharing, transfer, future use, withdrawal, and destruction or de-identification.
5. **Prioritize findings** — For each HIGH finding, identify the exact ICF section, heading, page, or quoted phrase when available. State the basis as binding legal requirement, TCPS 2 ethics guidance, or MUHC institutional practice, and name the relevant source or provision when known.
6. **Prepare the response** using only the required sections below, ending with the Handoff block.

## Review Criteria
### High Priority
Use HIGH when an issue could compromise valid consent, participant protection, or compliance. Check:
- Separate consent for future use/data banking when applicable.
- Withdrawal without prejudice and treatment of data or samples already collected or shared.
- No prohibition on oral withdrawal unless a justified requirement applies.
- No therapeutic misconception or exculpatory language.
- Clear data lifecycle and confidentiality limits.
- Compensation not contingent on study completion.
- Law 25, LRSSS/Law 5, and cross-border transfer requirements only where the organization, data, and transfer context make them applicable.

### Medium Priority
Use MEDIUM for important communication or good-practice issues unlikely to invalidate consent alone. Check:
- Grade 6–8 readability, jargon, and undefined acronyms.
- Separate contact information for the study team and REB office.
- Internal consistency in dates, versions, procedures, and terminology.

### Low Priority
Use LOW for useful but nonessential improvements in clarity, accessibility, or cultural safety.

### Cultural Safety
When Indigenous participants, communities, knowledge, or perspectives are involved, assess OCAP®, Indigenous data sovereignty, community engagement, culturally safe consent, and coercion risks.

### French Documents
Use "évaluation éthique," "consentement libre et éclairé," and appropriate OQLF/DataFranca terminology. Use "mode opératoire normalisé" for SOP.

## Required Output
### OVERALL IMPRESSION
- Begin: "Reviewed the provided ICF describing a [clinical/qualitative/other] study titled [title]."
- If unclear, write: "Reviewed the provided ICF. The study type is not clearly specified."
- Give 2–3 sentences summarizing key strengths and one or two priority gaps.
- State whether consent covers a primary study only, both primary study and future use/data banking, or is unclear.
- If future use/data banking appears, state whether the choices are separate.
- Confirm whether the full document was reviewed.

### KEY ISSUES & RECOMMENDATIONS
| Area | ICF location | Issue | Basis | Recommendation | Priority |
| :--- | :--- | :--- | :--- | :--- | :--- |

Use only HIGH, MEDIUM, or LOW priorities. For every HIGH item, complete both ICF location and Basis. If location cannot be identified, say so explicitly.

### OPTIONAL ENHANCEMENTS
Include 3–5 bullets only when they provide genuine improvements in clarity, accessibility, structure, or cultural safety.

### HANDOFF TO CONSISTENCY AUDITOR (machine-readable, do not rewrite)
**Jurisdiction:** [state]
**Population:** [state]
**Study type:** [clinical/qualitative/other]
**Consent purpose:** primary / combined-primary-future / unclear
**Future-use choices separated:** yes / no / cannot assess
**Privacy law applicable:** R-22.1 (health info) / A-2.1 (public-body records) / P-39.1 (private sector only)
**Cultural safety triggered:** yes / no
**Agent 1 findings to verify (copy exact):**
1. [ICF location] — [issue] — [basis] — [priority]
2. ...

Respond only with these sections and no additional commentary."""

AGENT2_PROMPT = """# REB Informed Consent Consistency Auditor

## Purpose
Ensure each ICF is factually accurate, complete, and consistent with its source Study Protocol, protecting participants and minimizing false positives.

## Standards
- Apply TCPS 2 (2022) and Civil Code of Québec arts. 10–25; privacy law applicability sequence: R-22.1 for health information, A-2.1 for public-body records, P-39.1 for private-sector entities only. Quebec jurisdiction established by MUHC REB submission.
- Cite exact TCPS 2 article/section or Civil Code article for every legal/ethical conclusion.
- If a provision is ambiguous or depends on unestablished facts, mark **HUMAN REVIEW REQUIRED** and Confidence **LOW**.
- Accept accurate lay-language equivalents as **CONSISTENT**.
- Accept a prior-findings handoff block titled "HANDOFF TO CONSISTENCY AUDITOR" as input; treat listed items as already-flagged: verify each against the protocol, confirm/escalate/downgrade, do not re-discover.
- Cultural safety / OCAP® / Indigenous data sovereignty checks when Indigenous participants, communities, knowledge, or perspectives are involved.
- Future-use / data-banking separation check.
- Readability: compute Flesch-Kincaid Grade Level; report score in a dedicated **Readability Score** field; reserve JARGON ALERT for specific terms that could be simplified (not the document's overall grade level).
- Participant burden / accessibility check (comprehension, time, technology).

## Step 0 — Consume Handoff from Agent 1 (if present)
If the input includes a block titled "HANDOFF TO CONSISTENCY AUDITOR":
- Parse the key-value pairs (Population, Study type, Consent purpose, Future-use choices separated, Privacy law applicable R-22.1/A-2.1/P-39.1, Cultural safety triggered).
- Parse the numbered findings list under "Agent 1 findings to verify (copy exact)".
- Treat each listed item as already-flagged: verify it against the Protocol in Step 4, confirm/escalate/downgrade its status, do NOT re-discover it from scratch.
- If the handoff block is absent, proceed normally (no handoff).

## Step 1 — Identify Documents
1. Review first five pages of each file.
2. Identify Protocol and ICF from titles, headers, content.
3. If unclear, ask one concise question naming the files and requesting classification; do not produce a report yet.

## Step 2 — Check Document Readability
Confirm every page opens and reads reliably. If password-protected, corrupted, incomplete, or scanned pages with unreliable text, identify filename and affected pages. Ask for an unlocked/complete/searchable copy before continuing. Do not infer unreadable content. Resume only when readable; if impossible, state DOCUMENT REVIEW INCOMPLETE and HUMAN REVIEW REQUIRED.

## Step 3 — Extract Evidence
Read documents completely. Keep exact quotations ≤50 words with page/section.

From Protocol, extract consent-critical: design/arms/randomization/blinding/placebo/duration; population; interventions; physical/psychological/reproductive/privacy risks (frequency, severity); daily-life restrictions; compensation per Protocol; data/specimen collection, retention, transfer, secondary use, withdrawal, confidentiality.

From ICF, select study-specific factual claims only (design, population, arms, randomization, duration, procedures, visits, interventions, risks, restrictions, benefits, data/specimen use/retention). Do not select ordinary ICF-only disclosures (compensation, injury language, contacts, complaints, participant rights, withdrawal admin, privacy boilerplate, institutional practices, signatures) unless Protocol contains an explicit incompatible statement.

Criticality: HIGH (could materially impair consent: omitted risks, randomization, invasive procedures, impermissible liability); MEDIUM (required disclosure, omission important but not alone invalidating); LOW (contextual/explanatory).

## Step 4 — Bidirectional Comparison
1. Protocol → ICF: For every consent-critical Protocol element, find best ICF match; assess completeness, specificity, consistency.
2. ICF → Protocol: Review selected study-specific claims. Report reverse discrepancy only when Protocol contains explicit, materially incompatible statement.

Safeguards: Protocol silence ≠ contradiction. Omitted detail, different wording, greater ICF specificity, accurate lay wording, ICF-only administrative/legal disclosures ≠ contradictions — except greater ICF specificity is a contradiction when it adds material facts the Protocol does not support (e.g., indefinite retention vs. fixed term). If ICF-only statement neither supported nor contradicted by Protocol, omit from report. INCONSISTENT only when both-quoted passages establish mutually incompatible facts. VAGUE when both address same topic without sufficient specificity.

Status: CONSISTENT (facts match; lay translation OK); INCONSISTENT (mutually incompatible facts); VAGUE (required topic, insufficient specificity); ABSENT (consent-critical Protocol element missing from ICF); JARGON ALERT (accurate meaning but unnecessary medical terminology; may accompany CONSISTENT or VAGUE); NON-COMPLIANT (impermissible waiver/exculpatory clause/liability limit, supported by applicable provision).

Confidence HIGH/MEDIUM/LOW. LOW + HUMAN REVIEW REQUIRED for uncertain semantic/legal interpretations.

## Step 5 — Output

### EXECUTIVE SUMMARY
- Documents: [Protocol] → Protocol; [ICF] → ICF.
- Assessment: X inconsistencies · Y absent critical elements · J jargon alerts · W non-compliant items · readability FK-GL [score].
- CRITICAL ALERTS: All HIGH-criticality ABSENT/INCONSISTENT/NON-COMPLIANT items with applicable provisions.
- HUMAN REVIEW REQUIRED: Every uncertain interpretation, or "None."

Finish classifying every comparison item before rendering tables. Each item in exactly one table.

### COMPARISON TABLE
One unified table. Sort by severity rank: 1.INCONSISTENT 2.ABSENT 3.VAGUE 4.NON-COMPLIANT 5.JARGON ALERT 6.CONSISTENT.

| Comparison Item | Status | Criticality | Protocol Text (Source) | ICF Text (Match) | Confidence | Readability Score (FK-GL) | Applicable Provision | Notes |
|---|---|---|---|---|---|---|---|---|
| [Element] | ABSENT | HIGH | "[quote]" (p.X) | ABSENT | HIGH | [FK-GL score] | [Provision] | Missing critical information |
| [ICF claim] | INCONSISTENT | [Level] | "[contradictory quote]" (p.X) | "[quote]" (p.Z) | HIGH | [FK-GL score] | [Provision or N/A] | Material contradiction |
| [Element] | CONSISTENT · JARGON ALERT | [Level] | "[quote]" (p.X/Sec Y) | "[quote]" (p.Z) | [Confidence] | [FK-GL score] | [Provision or N/A] | Accurate meaning; simplify specific terms |
| [Element] | CONSISTENT | HIGH | "[quote]" (p.X/Sec Y) | "[quote]" (p.Z) | HIGH | [FK-GL score] | [Provision or N/A] | Lay language appropriate |

If no rows, write None identified.

## Guardrails
1. Support every finding with quoted evidence; every legal/ethical conclusion with exact provision — if none verified, state PROVISION NOT VERIFIED + HUMAN REVIEW REQUIRED.
2. Do not infer sponsor/investigator/institutional intent.
3. If a Protocol-led item expected from the referenced section cannot be located, state NOT FOUND IN PROTOCOL — never for ordinary ICF-only disclosures.
4. Check Québec-relevant disclosures: withdrawal without prejudice, international transfer, secondary use, liability limits; apply R-22.1 (health info), A-2.1 (public-body records), P-39.1 (private sector only).
5. Redact participant names/identifiers from quotations.
6. Except when requesting document classification or a readable replacement file, provide only the Executive Summary and Comparison Table; no chit-chat."""


# ── PDF / Text Extraction ─────────────────────────────────────────────

def extract_text(filepath):
    """Extract text from a PDF or read a text file."""
    path = Path(filepath)
    if not path.exists():
        print(f"Error: File not found: {filepath}")
        sys.exit(1)

    if path.suffix.lower() == ".pdf":
        try:
            reader = PdfReader(str(path))
            text = ""
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
            return text
        except Exception as e:
            print(f"Error extracting PDF: {e}")
            sys.exit(1)
    else:
        return path.read_text(encoding="utf-8", errors="replace")


def split_protocol_icf(text):
    """Split combined Protocol+ICF document into separate texts.
    
    Looks for the ICF boundary marker '## Consent and Authorization Form'
    or similar patient-facing headers.
    """
    lines = text.split('\n')
    
    # Look for ICF start markers
    icf_markers = [
        "Consent and Authorization Form",
        "INFORMED CONSENT",
        "Informed Consent",
        "voluntary participation",
        "signature",
    ]
    
    split_line = None
    for i, line in enumerate(lines):
        line_lower = line.lower().strip()
        for marker in icf_markers:
            if marker.lower() in line_lower and len(line.strip()) > 10:
                # Check this is actually the ICF start (not just a mention)
                if i > len(lines) * 0.3:  # ICF is usually in the latter half
                    split_line = i
                    break
        if split_line:
            break
    
    if split_line:
        # Go back to find the actual start of the ICF section
        while split_line > 0 and not lines[split_line].strip().startswith("#"):
            split_line -= 1
        
        protocol_text = '\n'.join(lines[:split_line])
        icf_text = '\n'.join(lines[split_line:])
        return protocol_text, icf_text
    
    # No split found — treat as ICF only
    return "", text


# ── LLM API Calls ─────────────────────────────────────────────────────

def call_llm(messages, max_tokens=None, temperature=None):
    """Call an OpenAI-compatible LLM API."""
    if max_tokens is None:
        max_tokens = LLM_MAX_TOKENS
    if temperature is None:
        temperature = LLM_TEMPERATURE

    api_url = get_api_url()
    api_key = get_api_key()
    model = get_model()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    url = f"{api_url.rstrip('/')}/chat/completions"

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=120)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]
    except requests.exceptions.RequestException as e:
        print(f"Error calling LLM API: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Response: {e.response.text[:500]}")
        sys.exit(1)
    except (KeyError, IndexError) as e:
        print(f"Error parsing LLM response: {e}")
        sys.exit(1)


# ── Handoff Parsing ───────────────────────────────────────────────────

def parse_handoff_block(text):
    """Parse the HANDOFF TO CONSISTENCY AUDITOR block from Agent 1's output."""
    match = re.search(
        r'### HANDOFF TO CONSISTENCY AUDITOR.*?(?=\n## |\n# |$)',
        text, re.DOTALL
    )
    if match:
        return match.group(0)
    return None


def parse_handoff_kv(handoff_text):
    """Parse key-value pairs from handoff block."""
    kv = {}
    for line in handoff_text.split('\n'):
        m = re.match(r'\*\*(\w+):\*\*\s*(.+)', line)
        if m:
            kv[m.group(1)] = m.group(2).strip()
    return kv


# ── Report Generation ─────────────────────────────────────────────────

def merge_reports(agent1_output, agent2_output, handoff_block):
    """Merge Agent 1 and Agent 2 outputs into a unified report."""
    report = f"""# ICF Review Report — Unified

**Advisory only — not an REB determination. Human REB makes final decisions per TCPS 2 Art. 6.4, 6.8–6.11 and MUHC SOP 401.001-1.**

Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}
| Model: {get_model()}

---

## Agent 1: Pre-Review
{agent1_output}

## Agent 2: Consistency Auditor
{agent2_output}

## Handoff Block (Agent 1 → Agent 2)
{handoff_block or 'No handoff block found.'}
"""
    return report


# ── Main Workflow ──────────────────────────────────────────────────────

def run_review_chain(icf_text, protocol_text, output_dir):
    """Run the full Agent 1 → Agent 2 → unified report chain."""

    # Prepare documents for the LLM
    # Truncate if too long (keep most important parts)
    max_doc_chars = 25000  # Per-document limit to stay within context

    if len(protocol_text) > max_doc_chars:
        protocol_text = protocol_text[:max_doc_chars] + f"\n\n[Protocol truncated — original {len(protocol_text)} chars, showing first {max_doc_chars}]"

    if len(icf_text) > max_doc_chars:
        icf_text = icf_text[:max_doc_chars] + f"\n\n[ICF truncated — original {len(icf_text)} chars, showing first {max_doc_chars}]"

    # Step 1: Agent 1 (Pre-Review)
    print("=== Step 1: Agent 1 (Pre-Review) ===")
    agent1_messages = [
        {"role": "system", "content": AGENT1_PROMPT},
        {"role": "user", "content": f"""Review the following ICF and Protocol documents.

PROTOCOL:
{protocol_text}

ICF:
{icf_text}

Produce the Pre-Review output with OVERALL IMPRESSION, KEY ISSUES & RECOMMENDATIONS table, OPTIONAL ENHANCEMENTS, and HANDOFF TO CONSISTENCY AUDITOR block. Respond only with these sections and no additional commentary."""},
    ]

    agent1_output = call_llm(agent1_messages)
    print(f"Agent 1 output: {len(agent1_output)} chars")

    # Parse handoff
    handoff_block = parse_handoff_block(agent1_output)
    print(f"Handoff block: {'found' if handoff_block else 'NOT FOUND'}")

    # Step 2: Agent 2 (Consistency Auditor)
    print("\n=== Step 2: Agent 2 (Consistency Auditor) ===")
    agent2_messages = [
        {"role": "system", "content": AGENT2_PROMPT},
        {"role": "user", "content": f"""HANDOFF FROM AGENT 1:
{handoff_block or 'No handoff block provided.'}

PROTOCOL:
{protocol_text}

ICF:
{icf_text}

Follow the Agent 2 prompt instructions exactly. Step 0: consume the handoff block. Step 1-4: bidirectional comparison. Step 5: output Executive Summary + Comparison Table. Provide only the Executive Summary and Comparison Table; no chit-chat."""},
    ]

    agent2_output = call_llm(agent2_messages)
    print(f"Agent 2 output: {len(agent2_output)} chars")

    # Step 3: Generate unified report
    print("\n=== Step 3: Unified Report ===")
    unified_report = merge_reports(agent1_output, agent2_output, handoff_block)

    # Save outputs
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    chain_dir = Path(output_dir) / f"review-{timestamp}"
    chain_dir.mkdir(parents=True, exist_ok=True)

    (chain_dir / "agent1_output.md").write_text(agent1_output, encoding="utf-8")
    (chain_dir / "agent2_output.md").write_text(agent2_output, encoding="utf-8")
    (chain_dir / "unified_report.md").write_text(unified_report, encoding="utf-8")

    # Summary
    print(f"\n=== Review Chain Complete ===")
    print(f"Output directory: {chain_dir}")
    for f in sorted(chain_dir.iterdir()):
        print(f"  {f.name} ({f.stat().st_size} bytes)")

    return chain_dir


def get_file_path(prompt, default=None):
    """Prompt user for a file path, with optional default."""
    if default:
        msg = f"{prompt} [{default}]: "
    else:
        msg = f"{prompt}: "

    path = input(msg).strip()
    if not path and default:
        path = default
    if not path:
        print("Error: No file provided.")
        sys.exit(1)

    p = Path(path)
    if not p.exists():
        print(f"Error: File not found: {path}")
        sys.exit(1)
    return str(p)


def main():
    parser = argparse.ArgumentParser(
        description="ICF Review Chain — Agent 1 (Pre-Review) → Agent 2 (Consistency Auditor) → Unified Report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Interactive mode (prompts for everything)
  python3 review_chain_standalone.py

  # With file paths
  python3 review_chain_standalone.py icf.pdf protocol.pdf

  # With custom output directory
  python3 review_chain_standalone.py icf.pdf protocol.md ./output

  # Paste ICF from stdin (pipe or redirect)
  cat icf.pdf | python3 review_chain_standalone.py --stdin-icf - protocol.pdf

Environment variables:
  LLM_API_URL       - OpenAI-compatible API endpoint (default: https://api.openai.com/v1)
  LLM_API_KEY       - API key (can also be saved to ~/.icf-review-chain/config.json)
  LLM_MODEL         - Model name (default: gpt-4o)
  LLM_MAX_TOKENS    - Max tokens per call (default: 4096)
  LLM_TEMPERATURE   - Sampling temperature (default: 0.3)

Config file:
  ~/.icf-review-chain/config.json — saves API key and model between runs
        """,
    )
    parser.add_argument("icf_file", nargs="?", help="ICF file (PDF or text)")
    parser.add_argument("protocol_file", nargs="?", help="Protocol file (PDF or text)")
    parser.add_argument("output_dir", nargs="?", default="./deliberations",
                        help="Output directory (default: ./deliberations)")
    parser.add_argument("--stdin-icf", action="store_true",
                        help="Read ICF from stdin instead of file")

    args = parser.parse_args()

    # Check API key
    api_key = get_api_key()
    if not api_key:
        print("Error: LLM_API_KEY not set.")
        print("Set it with: export LLM_API_KEY=your-key")
        print("Or save to config: edit ~/.icf-review-chain/config.json")
        sys.exit(1)

    # Get ICF text
    if args.stdin_icf:
        icf_text = sys.stdin.read()
        print(f"ICF from stdin: {len(icf_text)} chars")
    elif args.icf_file:
        print(f"Reading ICF: {args.icf_file}")
        icf_text = extract_text(args.icf_file)
        print(f"  {len(icf_text)} chars")
    else:
        icf_path = get_file_path("Enter ICF file path")
        icf_text = extract_text(icf_path)
        print(f"  {len(icf_text)} chars")

    # Get Protocol text
    if args.protocol_file:
        print(f"Reading Protocol: {args.protocol_file}")
        protocol_text = extract_text(args.protocol_file)
        print(f"  {len(protocol_text)} chars")
    else:
        proto_path = get_file_path("Enter Protocol file path")
        protocol_text = extract_text(proto_path)
        print(f"  {len(protocol_text)} chars")

    # Check if documents are combined (Protocol + ICF in one file)
    if args.icf_file and not args.stdin_icf:
        if len(icf_text) > len(protocol_text) * 0.5:
            proto_part, icf_part = split_protocol_icf(icf_text)
            if proto_part and len(proto_part) > 1000:
                print("\nDetected combined document — splitting Protocol/ICF...")
                protocol_text = proto_part
                icf_text = icf_part
                print(f"  Protocol: {len(protocol_text)} chars")
                print(f"  ICF: {len(icf_text)} chars")

    # Run the review chain
    chain_dir = run_review_chain(icf_text, protocol_text, args.output_dir)
    print(f"\nDone. Unified report: {chain_dir}/unified_report.md")


if __name__ == "__main__":
    main()