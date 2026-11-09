#!/usr/bin/env python3
"""
ICF Review Chain - Standalone Python CLI

Agent 1 (Pre-Review) -> Agent 2 (Consistency Auditor) -> Unified Report

Calls any OpenAI-compatible chat-completions API directly.

Usage:
    python3 review_chain_standalone.py <icf_file> <protocol_file> [output_dir]

Settings (environment variable first, then ~/.icf-review-chain/config.json):

    LLM_API_URL          llm_api_url          default https://api.openai.com/v1
    LLM_API_KEY          llm_api_key          required
    LLM_MODEL            llm_model            default gpt-4o
    LLM_MAX_TOKENS       llm_max_tokens       default 8192   (output tokens per call)
    LLM_TEMPERATURE      llm_temperature      default 0.3
    LLM_MAX_INPUT_CHARS  llm_max_input_chars  default 300000 (characters per request)
    LLM_TIMEOUT          llm_timeout          default 600    (seconds per request)

How coverage works (nothing is silently dropped):

  * The ICF is always sent complete. If it cannot fit in one request, the run
    stops with an explanation instead of reviewing part of it.
  * The Protocol is split on section headings into parts that fit the request
    size. Agent 2 compares every part against the complete ICF, and the tool
    merges the results.
  * Every quotation in the findings is checked in code against the extracted
    document text. Quotes that cannot be found are flagged in the report.
  * Readability (Flesch-Kincaid grade level) is calculated in code, not by the LLM.
  * The report states whether the review was COMPLETE or INCOMPLETE, and the
    process exits with status 2 when it was incomplete.
"""

import argparse
import getpass
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests
from pypdf import PdfReader

# -- Configuration ------------------------------------------------------

CONFIG_DIR = Path.home() / ".icf-review-chain"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_API_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o"
DEFAULT_MAX_TOKENS = 8192
DEFAULT_TEMPERATURE = 0.3
DEFAULT_MAX_INPUT_CHARS = 300000
DEFAULT_TIMEOUT = 600

# Characters reserved per request for the wrapper text around the documents.
SCAFFOLD_CHARS = 3000
# Smallest Protocol part worth sending; below this the ICF leaves no room.
MIN_PART_CHARS = 4000


class ReviewError(Exception):
    """A problem the user must resolve. Reported without a traceback."""


class LLMError(ReviewError):
    """An API failure. fatal=True means retrying other calls is pointless."""

    def __init__(self, message, fatal=False):
        super().__init__(message)
        self.fatal = fatal


@dataclass
class Settings:
    api_url: str
    api_key: str
    model: str
    max_tokens: int
    temperature: float
    max_input_chars: int
    timeout: int


def load_config():
    """Load saved config from the config file."""
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, PermissionError, OSError):
            return {}
    return {}


def save_config(config):
    """Save config; the file may contain an API key, so keep it private."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(CONFIG_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
    try:
        os.chmod(str(CONFIG_FILE), 0o600)
    except OSError:
        pass


def _setting(env_name, config_key, default, cast, config):
    """Resolve one setting: environment, then config file, then default."""
    raw = os.environ.get(env_name, "")
    source = env_name
    if raw == "":
        value = config.get(config_key)
        if value in (None, ""):
            return default
        raw = value
        source = f"{CONFIG_FILE} ({config_key})"
    try:
        return cast(raw)
    except (TypeError, ValueError):
        raise ReviewError(
            f"Invalid value {raw!r} for {source}; expected {cast.__name__}."
        )


def get_api_key(config):
    """Get the API key from the environment, the config file, or a prompt."""
    key = os.environ.get("LLM_API_KEY", "")
    if key:
        return key
    key = config.get("llm_api_key", "")
    if key:
        return key
    if not sys.stdin.isatty():
        raise ReviewError(
            "LLM_API_KEY is not set and there is no interactive terminal.\n"
            "Set it with: export LLM_API_KEY=your-key\n"
            f"Or save it as llm_api_key in {CONFIG_FILE}"
        )
    print("LLM_API_KEY not set.")
    print("Get a key from your LLM provider (OpenAI, Anthropic, DeepSeek, etc.)")
    key = getpass.getpass("Enter API key (input is hidden): ").strip()
    if not key:
        raise ReviewError("No API key provided.")
    print(
        f"\nThe key can be saved to {CONFIG_FILE} so you are not asked again.\n"
        "It would be stored as PLAIN TEXT. Anyone who can read your user folder, or a "
        "backup of it, could use the key."
    )
    if sys.platform.startswith("win"):
        print(
            "On Windows this tool cannot restrict who can read that file. On a shared or "
            "managed computer, set the LLM_API_KEY environment variable instead."
        )
    answer = input("Save the key to that file? [y/N]: ").strip().lower()
    if answer in ("y", "yes"):
        config["llm_api_key"] = key
        save_config(config)
        print(f"Key saved to {CONFIG_FILE}")
    else:
        print("Key not saved. It is used for this run only.")
    return key


def load_settings():
    """Build the Settings object, validating every value."""
    config = load_config()
    settings = Settings(
        api_url=str(_setting("LLM_API_URL", "llm_api_url", DEFAULT_API_URL, str, config)),
        api_key=get_api_key(config),
        model=str(_setting("LLM_MODEL", "llm_model", DEFAULT_MODEL, str, config)),
        max_tokens=_setting("LLM_MAX_TOKENS", "llm_max_tokens", DEFAULT_MAX_TOKENS, int, config),
        temperature=_setting("LLM_TEMPERATURE", "llm_temperature", DEFAULT_TEMPERATURE, float, config),
        max_input_chars=_setting(
            "LLM_MAX_INPUT_CHARS", "llm_max_input_chars", DEFAULT_MAX_INPUT_CHARS, int, config
        ),
        timeout=_setting("LLM_TIMEOUT", "llm_timeout", DEFAULT_TIMEOUT, int, config),
    )
    if settings.max_tokens <= 0:
        raise ReviewError("LLM_MAX_TOKENS must be greater than 0.")
    if not 0.0 <= settings.temperature <= 2.0:
        raise ReviewError("LLM_TEMPERATURE must be between 0 and 2.")
    if settings.max_input_chars < 20000:
        raise ReviewError("LLM_MAX_INPUT_CHARS must be at least 20000.")
    if settings.timeout <= 0:
        raise ReviewError("LLM_TIMEOUT must be greater than 0.")
    return settings


# -- Agent prompts ------------------------------------------------------

AGENT1_PROMPT = """# Consent Form Pre-Review

## Purpose

Act as a Qu\u00e9bec Research Ethics Board consultant reviewing informed consent forms (ICFs) and participant-facing consent documents. Provide strategic, high-level feedback on participant comprehension, ethical risk, regulatory compliance, and cultural safety.

## Scope

Assess only:

- **Clarity and readability:** Grade 6-8 plain language.
- **Completeness:** TCPS 2 (2022), especially Chapter 3.
- **Ethical risks:** coercion, voluntariness, therapeutic misconception, power imbalances.
- **Purpose clarity:** whether consent covers the primary study only or also future use/data banking; when both apply, verify separate choices.
- **Cultural safety:** Indigenous research ethics, OCAP\u00ae principles, and data sovereignty when Indigenous participants, communities, knowledge, or perspectives are involved.
- **Qu\u00e9bec legal requirements:** Civil Code of Qu\u00e9bec arts. 10-25; privacy law applicability sequence: R-22.1 for health information, A-2.1 for public-body records, P-39.1 for private-sector entities only. Quebec jurisdiction established by MUHC REB submission.
- **Data governance:** collection, storage, retention, destruction, withdrawal, secondary use, and international transfer.
- **Participant burden:** comprehension, time, accessibility, and technology requirements.

## General Guidelines

- Do not rewrite text unless the user explicitly asks to rewrite a named section.
- Match the document language, using Canadian English or DataFranca French.
- Use a collegial, precise, REB-aware, non-directive tone.
- Make every recommendation concrete and actionable.
- Include only issues requiring changes in the findings table; do not list strengths there.
- Do not flag missing elements unless required by TCPS 2, applicable Qu\u00e9bec law, or applicable MUHC REB requirements for the context described.
- Do not infer study type, risk level, data flows, jurisdiction, or institutional practices beyond the ICF.
- Do not flag minor typos, fonts, or layout issues as findings.

## Step-by-Step Review

1. **Establish applicability** - Identify participant population, data or biological-material type, study setting, institutions, and data-transfer locations. Quebec jurisdiction is established by MUHC REB submission; determine which legal, ethical, and institutional requirements apply before assessing compliance.
2. **Identify consent purpose** - Classify the ICF as primary-study consent, combined primary-study and future-use/data-banking consent, or unclear. When future use or data banking is included, assess whether choices are clearly separated and withdrawal limits are explained.
3. **Review participant protections** - Check voluntariness, ongoing consent, withdrawal without prejudice, capacity, therapeutic misconception, confidentiality limits, compensation, liability language, and contact information.
4. **Review data governance** - Trace collection, access, storage, retention, sharing, transfer, future use, withdrawal, and destruction or de-identification.
5. **Prioritize findings** - For each HIGH finding, identify the exact ICF section, heading, page, or quoted phrase when available. State the basis as binding legal requirement, TCPS 2 ethics guidance, or MUHC institutional practice, and name the relevant source or provision when known.
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

- Grade 6-8 readability, jargon, and undefined acronyms.
- Separate contact information for the study team and REB office.
- Internal consistency in dates, versions, procedures, and terminology.

### Low Priority

Use LOW for useful but nonessential improvements in clarity, accessibility, or cultural safety.

### Cultural Safety

When Indigenous participants, communities, knowledge, or perspectives are involved, assess OCAP\u00ae, Indigenous data sovereignty, community engagement, culturally safe consent, and coercion risks.

### French Documents

Use "\u00e9valuation \u00e9thique," "consentement libre et \u00e9clair\u00e9," and appropriate OQLF/DataFranca terminology. Use "mode op\u00e9ratoire normalis\u00e9" for SOP.

## Required Output

### OVERALL IMPRESSION

- Begin: "Reviewed the provided ICF describing a [clinical/qualitative/other] study titled [title]."
- If unclear, write: "Reviewed the provided ICF. The study type is not clearly specified."
- Give 2-3 sentences summarizing key strengths and one or two priority gaps.
- State whether consent covers a primary study only, both primary study and future use/data banking, or is unclear.
- If future use/data banking appears, state whether the choices are separate.
- Confirm whether the full document was reviewed.

### KEY ISSUES & RECOMMENDATIONS

| Area | ICF location | Issue | Basis | Recommendation | Priority |
| :--- | :--- | :--- | :--- | :--- | :--- |

Use only HIGH, MEDIUM, or LOW priorities. For every HIGH item, complete both ICF location and Basis. If location cannot be identified, say so explicitly.

### OPTIONAL ENHANCEMENTS

Include 3-5 bullets only when they provide genuine improvements in clarity, accessibility, structure, or cultural safety.

### HANDOFF TO CONSISTENCY AUDITOR (machine-readable, do not rewrite)

**Jurisdiction:** [state]
**Population:** [state]
**Study type:** [clinical/qualitative/other]
**Consent purpose:** primary / combined-primary-future / unclear
**Future-use choices separated:** yes / no / cannot assess
**Privacy law applicable:** R-22.1 (health info) / A-2.1 (public-body records) / P-39.1 (private sector only)
**Cultural safety triggered:** yes / no

**Agent 1 findings to verify (copy exact):**

1. [ICF location] - [issue] - [basis] - [priority]
2. ...

Respond only with these sections and no additional commentary.

## Pipeline Notes (supplied by the tool; these override any conflicting instruction above)

- Page markers of the form [[PAGE n]] appear in the text. Cite ICF locations as page numbers (p.N), with the section heading where available.
- Coverage facts in the user message are supplied by the tool. Use them when you confirm whether the full document was reviewed.
- Quote exact phrases verbatim. The tool checks quoted text against the source documents and flags any quote it cannot find, so never paraphrase inside quotation marks.
"""

AGENT2_PROMPT = """# REB Informed Consent Consistency Auditor

## Purpose

Ensure each ICF is factually accurate, complete, and consistent with its source Study Protocol, protecting participants and minimizing false positives.

## Standards

- Apply TCPS 2 (2022) and Civil Code of Qu\u00e9bec arts. 10-25; privacy law applicability sequence: R-22.1 for health information, A-2.1 for public-body records, P-39.1 for private-sector entities only. Quebec jurisdiction established by MUHC REB submission.
- Cite exact TCPS 2 article/section or Civil Code article for every legal/ethical conclusion.
- If a provision is ambiguous or depends on unestablished facts, mark **HUMAN REVIEW REQUIRED** and Confidence **LOW**.
- Accept accurate lay-language equivalents as **CONSISTENT**.
- Accept a prior-findings handoff block titled "HANDOFF TO CONSISTENCY AUDITOR" as input; treat listed items as already-flagged: verify each against the protocol, confirm/escalate/downgrade, do not re-discover.
- Cultural safety / OCAP\u00ae / Indigenous data sovereignty checks when Indigenous participants, communities, knowledge, or perspectives are involved.
- Future-use / data-banking separation check.
- Readability: reserve JARGON ALERT for specific terms that could be simplified (not the document's overall grade level). The overall grade level is calculated by the tool.
- Participant burden / accessibility check (comprehension, time, technology).

## Step 0 - Consume Handoff from Agent 1 (if present)

If the input includes a block titled "HANDOFF TO CONSISTENCY AUDITOR":

- Parse the key-value pairs (Population, Study type, Consent purpose, Future-use choices separated, Privacy law applicable R-22.1/A-2.1/P-39.1, Cultural safety triggered).
- Parse the numbered findings list under "Agent 1 findings to verify (copy exact)".
- Treat each listed item as already-flagged: verify it against the Protocol in Step 4, confirm/escalate/downgrade its status, do NOT re-discover it from scratch.
- If the handoff block is absent, proceed normally (no handoff).

## Step 1 - Identify Documents

The tool labels the PROTOCOL and the ICF in the input. Use those labels.

## Step 2 - Check Document Readability

The tool checks that every page has extractable text and reports unreadable pages itself. Do not infer content that is not in the text you were given.

## Step 3 - Extract Evidence

Read the supplied documents completely. Keep exact quotations to at most 50 words with page/section.

From Protocol, extract consent-critical: design/arms/randomization/blinding/placebo/duration; population; interventions; physical/psychological/reproductive/privacy risks (frequency, severity); daily-life restrictions; compensation per Protocol; data/specimen collection, retention, transfer, secondary use, withdrawal, confidentiality.

From ICF, select study-specific factual claims only (design, population, arms, randomization, duration, procedures, visits, interventions, risks, restrictions, benefits, data/specimen use/retention). Do not select ordinary ICF-only disclosures (compensation, injury language, contacts, complaints, participant rights, withdrawal admin, privacy boilerplate, institutional practices, signatures) unless Protocol contains an explicit incompatible statement.

Criticality: HIGH (could materially impair consent: omitted risks, randomization, invasive procedures, impermissible liability); MEDIUM (required disclosure, omission important but not alone invalidating); LOW (contextual/explanatory).

## Step 4 - Bidirectional Comparison

1. Protocol -> ICF: For every consent-critical Protocol element, find best ICF match; assess completeness, specificity, consistency.
2. ICF -> Protocol: Review selected study-specific claims. Report reverse discrepancy only when Protocol contains explicit, materially incompatible statement.

Safeguards: Protocol silence is not contradiction. Omitted detail, different wording, greater ICF specificity, accurate lay wording, ICF-only administrative/legal disclosures are not contradictions - except greater ICF specificity is a contradiction when it adds material facts the Protocol does not support (e.g., indefinite retention vs. fixed term). If ICF-only statement neither supported nor contradicted by Protocol, omit from report. INCONSISTENT only when both-quoted passages establish mutually incompatible facts. VAGUE when both address same topic without sufficient specificity.

Status: CONSISTENT (facts match; lay translation OK); INCONSISTENT (mutually incompatible facts); VAGUE (required topic, insufficient specificity); ABSENT (consent-critical Protocol element missing from ICF); JARGON ALERT (accurate meaning but unnecessary medical terminology; may accompany CONSISTENT or VAGUE); NON-COMPLIANT (impermissible waiver/exculpatory clause/liability limit, supported by applicable provision).

Confidence HIGH/MEDIUM/LOW. LOW + HUMAN REVIEW REQUIRED for uncertain semantic/legal interpretations.

## Step 5 - Output

### COMPARISON TABLE

Finish classifying every comparison item before rendering the table. Each item in exactly one row. One unified table. Sort by severity rank: 1.INCONSISTENT 2.ABSENT 3.VAGUE 4.NON-COMPLIANT 5.JARGON ALERT 6.CONSISTENT.

| Comparison Item | Status | Criticality | Protocol Text (Source) | ICF Text (Match) | Confidence | Readability Score (FK-GL) | Applicable Provision | Notes |
|---|---|---|---|---|---|---|---|---|
| [Element] | ABSENT | HIGH | "[quote]" (p.X) | ABSENT | HIGH | n/a | [Provision] | Missing critical information |
| [ICF claim] | INCONSISTENT | [Level] | "[contradictory quote]" (p.X) | "[quote]" (p.Z) | HIGH | n/a | [Provision or N/A] | Material contradiction |
| [Element] | CONSISTENT + JARGON ALERT | [Level] | "[quote]" (p.X) | "[quote]" (p.Z) | [Confidence] | n/a | [Provision or N/A] | Accurate meaning; simplify specific terms |
| [Element] | CONSISTENT | HIGH | "[quote]" (p.X) | "[quote]" (p.Z) | HIGH | n/a | [Provision or N/A] | Lay language appropriate |

If no rows, write None identified.

## Guardrails

1. Support every finding with quoted evidence; every legal/ethical conclusion with exact provision - if none verified, state PROVISION NOT VERIFIED + HUMAN REVIEW REQUIRED.
2. Do not infer sponsor/investigator/institutional intent.
3. Check Qu\u00e9bec-relevant disclosures: withdrawal without prejudice, international transfer, secondary use, liability limits; apply R-22.1 (health info), A-2.1 (public-body records), P-39.1 (private sector only).
4. Redact participant names/identifiers from quotations.
5. Provide only the Comparison Table; no chit-chat.

## Pipeline Notes (supplied by the tool; these override any conflicting instruction above)

- The tool has already identified the documents. Never ask questions or request a different file.
- The ICF is always complete. The PROTOCOL may be one numbered part of a longer protocol; the label says which. Compare only what is in the supplied part. Do not report ABSENT for protocol elements outside it, and do not write NOT FOUND IN PROTOCOL. The other parts are reviewed in separate calls and the tool merges the results.
- Page markers of the form [[PAGE n]] appear in the text. Cite pages as (p.N) using those markers.
- Quote exact text verbatim (at most 50 words). The tool checks every quote against the source documents and flags any it cannot find, so never paraphrase inside quotation marks. Use [...] to skip words and [REDACTED] for identifiers.
- The tool calculates the Flesch-Kincaid Grade Level in code. Do not calculate your own. Write n/a in the Readability Score column of every row; the tool fills it in.
- Output the nine-column Comparison Table only. The tool builds the Executive Summary from all parts.
"""


# -- Document model and extraction --------------------------------------

PAGE_MARKER_RE = re.compile(r"^\[\[PAGE (\d+)\]\]$")
PAGE_MARKER_ANYWHERE_RE = re.compile(r"\[\[PAGE \d+\]\]")
MIN_PAGE_CHARS = 20  # fewer extracted characters than this = page treated as unreadable


@dataclass
class SourceDoc:
    """One input document as a list of page texts."""

    label: str  # "ICF" or "PROTOCOL"
    path: str
    sha256: str
    pages: list
    empty_pages: list = field(default_factory=list)
    suspect_pages: list = field(default_factory=list)  # text present but looks garbled

    def marked_text(self):
        """Full text with a [[PAGE n]] marker before each page."""
        parts = []
        for number, text in enumerate(self.pages, start=1):
            parts.append(f"[[PAGE {number}]]\n{text.strip()}\n")
        return "\n".join(parts)

    def plain_text(self):
        return "\n".join(page.strip() for page in self.pages)


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def extract_document(filepath, label):
    """Read a PDF or text file into a SourceDoc, page by page."""
    path = Path(filepath)
    if not path.exists():
        raise ReviewError(f"File not found: {filepath}")
    data = path.read_bytes()

    if path.suffix.lower() == ".pdf":
        try:
            reader = PdfReader(str(path))
            if reader.is_encrypted:
                try:
                    unlocked = bool(reader.decrypt(""))
                except Exception:
                    unlocked = False
                if not unlocked:
                    raise ReviewError(
                        f"The {label} PDF ({filepath}) is password-protected. "
                        "Provide an unlocked copy."
                    )
            pages = []
            for page in reader.pages:
                try:
                    pages.append(page.extract_text() or "")
                except Exception:
                    pages.append("")
        except ReviewError:
            raise
        except Exception as exc:
            raise ReviewError(f"Could not read {label} PDF ({filepath}): {exc}")
    else:
        if b"\x00" in data[:4096]:
            raise ReviewError(
                f"{filepath} looks like a binary file. Convert the {label} to PDF "
                "or plain text first."
            )
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            # Not UTF-8: most likely a Windows-1252 file (common for French text on Windows).
            text = data.decode("cp1252", errors="replace")
        pages = text.split("\f") if "\f" in text else [text]

    return _build_doc(label, str(path), _sha256(data), pages)


def document_from_text(text, label, name):
    """Build a SourceDoc from text already in memory (for example from stdin)."""
    pages = text.split("\f") if "\f" in text else [text]
    return _build_doc(label, name, _sha256(text.encode("utf-8")), pages)


_VOWELS = set("aeiouy\u00e0\u00e2\u00e4\u00e9\u00e8\u00ea\u00eb\u00ee\u00ef\u00f4\u00f6\u00f9\u00fb\u00fc")


def looks_garbled(text):
    """Heuristic: does this page's text look like OCR noise rather than language?

    A scan with a poor hidden text layer can yield plenty of characters that are
    not real words. A page is flagged when too few of its words contain a vowel,
    or when letters are a small share of its characters. It is a prompt to look
    at the page, not a verdict (a table of numbers can trip it).
    """
    words = [t for t in re.findall(r"[^\W\d_]+", text) if len(t) >= 3]
    if len(words) < 20:
        return False
    plausible = sum(1 for w in words if len(w) <= 25 and any(c in _VOWELS for c in w.lower()))
    visible = [c for c in text if not c.isspace()]
    alpha_share = sum(1 for c in visible if c.isalpha()) / max(len(visible), 1)
    return plausible / len(words) < 0.8 or alpha_share < 0.45


def _build_doc(label, path, digest, pages):
    if not pages:
        raise ReviewError(f"{label} ({path}) has no pages.")
    empty = [n for n, text in enumerate(pages, start=1) if len(text.strip()) < MIN_PAGE_CHARS]
    if len(empty) == len(pages):
        raise ReviewError(
            f"No extractable text in the {label} ({path}). It may be a scanned "
            "document; run OCR (for example OCRmyPDF) to produce a searchable copy first."
        )
    suspect = [n for n, text in enumerate(pages, start=1)
               if n not in empty and looks_garbled(text)]
    return SourceDoc(label=label, path=path, sha256=digest, pages=pages,
                     empty_pages=empty, suspect_pages=suspect)


# -- Section-aware chunking ---------------------------------------------

HEADING_LINE_RE = re.compile(
    r"^(?:#{1,6}\s+\S.*"
    r"|(?:\d+(?:\.\d+){0,4}\.?|[A-Z]\.)\s+[A-Z]\S.*"
    r"|(?:SECTION|Section|CHAPTER|Chapter|APPENDIX|Appendix)\s+\w+.*"
    r"|[A-Z][A-Z0-9 ,;:&/()'-]{3,})$"
)


@dataclass
class Chunk:
    index: int
    total: int
    text: str
    page_start: int
    page_end: int


def is_heading_line(line):
    """Heuristic only: headings are preferred split points, never required."""
    stripped = line.strip()
    if not stripped or len(stripped) > 100:
        return False
    if PAGE_MARKER_RE.match(stripped):
        return False
    if stripped[-1] in ".,;":
        return False
    return bool(HEADING_LINE_RE.match(stripped))


def _doc_lines(doc):
    """(page number, line) pairs for the whole document, markers included."""
    lines = []
    for number, text in enumerate(doc.pages, start=1):
        lines.append((number, f"[[PAGE {number}]]"))
        for raw in text.strip().splitlines():
            lines.append((number, raw.rstrip()))
    return lines


def _size(items):
    return sum(len(line) + 1 for _, line in items)


def _segments(lines):
    """Split into segments, each starting at a heading line."""
    segments, current = [], []
    for item in lines:
        if is_heading_line(item[1]) and current:
            segments.append(current)
            current = []
        current.append(item)
    if current:
        segments.append(current)
    return segments


def _split_oversized(segment, limit):
    """Split a segment larger than the limit, preferring blank-line boundaries."""
    pieces, current, size = [], [], 0
    for item in segment:
        text = item[1]
        if len(text) + 1 > limit:  # a single absurdly long line: hard split
            if current:
                pieces.append(current)
                current, size = [], 0
            step = max(limit - 1, 1)
            for start in range(0, len(text), step):
                pieces.append([(item[0], text[start:start + step])])
            continue
        if size + len(text) + 1 > limit and current:
            cut = None
            for position in range(len(current) - 1, len(current) // 2, -1):
                if not current[position][1].strip():
                    cut = position + 1
                    break
            if cut:
                pieces.append(current[:cut])
                current = current[cut:]
                # The lines kept after the cut may still leave no room for this one.
                if _size(current) + len(text) + 1 > limit:
                    pieces.append(current)
                    current = []
            else:
                pieces.append(current)
                current = []
            size = _size(current)
        current.append(item)
        size += len(text) + 1
    if current:
        pieces.append(current)
    return pieces


def chunk_document(doc, max_chars):
    """Split a document into parts of at most max_chars, on section boundaries.

    Every line of the document lands in exactly one part. A page marker is
    added at the top of a part that begins mid-page so page citations work.
    """
    limit = max_chars - 40
    groups, current, size = [], [], 0
    for segment in _segments(_doc_lines(doc)):
        segment_size = _size(segment)
        if segment_size > limit:
            if current:
                groups.append(current)
                current, size = [], 0
            groups.extend(_split_oversized(segment, limit))
            continue
        if size + segment_size > limit and current:
            groups.append(current)
            current, size = [], 0
        current.extend(segment)
        size += segment_size
    if current:
        groups.append(current)

    chunks = []
    for number, group in enumerate(groups, start=1):
        body = [line for _, line in group]
        first_page = group[0][0]
        if not PAGE_MARKER_RE.match(body[0].strip()):
            body.insert(0, f"[[PAGE {first_page}]]")
        chunks.append(
            Chunk(
                index=number,
                total=len(groups),
                text="\n".join(body),
                page_start=first_page,
                page_end=group[-1][0],
            )
        )
    return chunks


# -- Readability (calculated in code) -----------------------------------

FRENCH_HINTS = {
    "le", "la", "les", "des", "une", "est", "pour", "dans", "vous", "nous",
    "que", "qui", "du", "au", "aux", "sur", "avec", "votre", "vos", "ne",
}
ENGLISH_HINTS = {
    "the", "and", "of", "to", "is", "you", "your", "for", "with", "that",
    "are", "will", "this", "be", "in",
}


def detect_language(text):
    """Return 'fr' when the text is clearly French, otherwise 'en'."""
    words = re.findall(r"[a-z\u00e0-\u00ff]+", text.lower())
    french = sum(1 for word in words if word in FRENCH_HINTS)
    english = sum(1 for word in words if word in ENGLISH_HINTS)
    return "fr" if french > english * 1.2 else "en"


def count_syllables(word):
    """Approximate English syllable count."""
    word = re.sub(r"[^a-z]", "", word.lower())
    if not word:
        return 0
    if len(word) <= 3:
        return 1
    word = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", word)
    word = re.sub(r"^y", "", word)
    return max(1, len(re.findall(r"[aeiouy]{1,2}", word)))


def flesch_kincaid_grade(text, min_words=30):
    """Flesch-Kincaid grade level, or None when there is too little text."""
    text = PAGE_MARKER_ANYWHERE_RE.sub(" ", text).replace("\u2019", "'")
    words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text)
    sentences = [s for s in re.split(r"[.!?]+(?:\s|$)", text) if re.search(r"[A-Za-z]", s)]
    if len(words) < min_words or not sentences:
        return None
    syllables = sum(count_syllables(word) for word in words)
    return 0.39 * len(words) / len(sentences) + 11.8 * syllables / len(words) - 15.59


def readability_label(text, min_words=30):
    """Printable FK-GL value, with an explanation when it does not apply."""
    if detect_language(text) == "fr":
        return "n/a (English-language metric; text appears to be French)"
    score = flesch_kincaid_grade(text, min_words)
    if score is None:
        return "n/a (too little text)"
    return f"{score:.1f}"


# -- Quote verification --------------------------------------------------


# Tokens for quote matching: numbers keep their internal decimal or thousands
# separator, so "1.5" never matches "15" and "$1,000" never matches "$10.00".
QUOTE_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)*|[^\W\d_]+|[$€£%]")
# Only these mark an omission inside a quote. Other bracketed text, such as
# "[not]", is treated as part of the quote and must be in the document.
QUOTE_GAP_RE = re.compile(r"\.{3}|…|\[\s*(?:\.{3}|…|redacted)\s*\]", re.IGNORECASE)
# Omitted text between two fragments of one quote may span at most this many
# key characters (roughly a long paragraph).
MAX_QUOTE_GAP = 1500
# A fragment shorter than this (after normalising) matches too easily to count.
MIN_FRAGMENT_CHARS = 8


def _quote_key(text):
    """(key, starts, ends): text reduced to matchable tokens, plus the key
    offsets where a token starts and where one ends. A match must begin and
    end on token boundaries, so "up to 10" does not match "up to 100"."""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = PAGE_MARKER_ANYWHERE_RE.sub(" ", text)
    pieces, starts, ends, offset = [], set(), set(), 0
    previous_was_number = False
    for match in QUOTE_TOKEN_RE.finditer(text):
        token = match.group()
        is_number = token[0].isdigit()
        if is_number and previous_was_number:
            # Keep two numbers apart, so "1-2 visits" never matches "12 visits".
            pieces.append("|")
            offset += 1
        previous_was_number = is_number
        pieces.append(token)
        starts.add(offset)
        offset += len(token)
        ends.add(offset)
    return "".join(pieces), starts, ends


class QuoteVerifier:
    """Checks quotations against the extracted text of one document."""

    def __init__(self, doc):
        self.page_count = len(doc.pages)
        keys, self._starts_ok, self._ends_ok, self._page_starts = [], set(), set(), []
        offset = 0
        for page in doc.pages:
            key, starts, ends = _quote_key(page)
            self._page_starts.append(offset)
            keys.append(key)
            self._starts_ok |= {offset + s for s in starts}
            self._ends_ok |= {offset + e for e in ends}
            offset += len(key)
        self._page_lengths = [len(k) for k in keys]
        self._full = "".join(keys)

    def _pages_for(self, start, end):
        pages = set()
        for number, page_start in enumerate(self._page_starts, start=1):
            page_end = page_start + self._page_lengths[number - 1]
            if page_start < end and start < page_end:
                pages.add(number)
        return pages

    def _find(self, key, start=0, stop=None):
        """First match of key at or after start, on token boundaries, beginning
        before stop. Returns its start offset or -1."""
        while True:
            where = self._full.find(key, start)
            if where < 0 or (stop is not None and where > stop):
                return -1
            if where in self._starts_ok and where + len(key) in self._ends_ok:
                return where
            start = where + 1

    def _chain(self, keys, first_at):
        """Match keys in order from first_at, each within MAX_QUOTE_GAP of the
        previous one. Returns the (start, end) span or None."""
        end = first_at + len(keys[0])
        for key in keys[1:]:
            where = self._find(key, end, end + MAX_QUOTE_GAP)
            if where < 0:
                return None
            end = where + len(key)
        return first_at, end

    def verify(self, quote, cited_pages=()):
        """Return a status string: VERIFIED, VERIFIED with PAGE MISMATCH,
        PARTIAL, NOT FOUND, or TOO SHORT TO CHECK."""
        fragments = [f for f in QUOTE_GAP_RE.split(quote) if _quote_key(f)[0]]
        keys = [_quote_key(f)[0] for f in fragments]
        if not keys or sum(len(k) for k in keys) < MIN_FRAGMENT_CHARS:
            return "TOO SHORT TO CHECK"

        spans = []
        where = self._find(keys[0])
        while where >= 0 and len(spans) < 50:
            span = self._chain(keys, where)
            if span:
                spans.append(span)
            where = self._find(keys[0], where + 1)

        if not spans:
            found = sum(1 for k in keys if self._find(k) >= 0)
            if found == 0:
                return "NOT FOUND"
            return f"PARTIAL ({found} of {len(keys)} fragments found, not in order or too far apart)"

        if len(keys) > 1:
            short = [f.strip() for f, k in zip(fragments, keys) if len(k) < MIN_FRAGMENT_CHARS]
            if short:
                return f'PARTIAL (fragment too short to check: "{short[0]}")'

        cited = set(cited_pages)
        found_pages = set()
        for start, end in spans:
            found_pages |= self._pages_for(start, end)
        if cited and self.page_count > 1 and found_pages and not (cited & found_pages):
            listed = ",".join(str(n) for n in sorted(found_pages)[:5])
            return f"VERIFIED, PAGE MISMATCH (found on p.{listed})"
        return "VERIFIED"


CITED_PAGE_RE = re.compile(r"\bpp?\.?\s*(\d+)(?:\s*[-\u2013]\s*(\d+))?", re.IGNORECASE)
QUOTED_SEGMENT_RE = re.compile(r'["\u201c]([^"\u201c\u201d]{8,}?)["\u201d]')
NO_QUOTE_RE = re.compile(r"(?i)^\W*(absent|n/?a|none|not found in protocol)\W*$")


def cited_pages(cell):
    """Page numbers cited in a table cell, e.g. (p.12) or (pp.12-14)."""
    pages = set()
    for first, last in CITED_PAGE_RE.findall(cell):
        low = int(first)
        high = int(last) if last else low
        pages.update(range(low, min(high, low + 200) + 1))
    return pages


def quotes_in_cell(cell):
    """Quoted passages in a table cell, or None if the cell holds no quote."""
    stripped = cell.strip()
    if not stripped or NO_QUOTE_RE.match(stripped):
        return None
    quotes = [q.strip() for q in QUOTED_SEGMENT_RE.findall(stripped)]
    if quotes:
        return quotes
    return [re.sub(r"\([^)]*\)\s*$", "", stripped).strip()]


def check_cell(cell, verifier):
    """Verify every quote in one cell. Returns a status, or None if no quote."""
    quotes = quotes_in_cell(cell)
    if quotes is None:
        return None
    pages = cited_pages(cell)
    results = [verifier.verify(quote, pages) for quote in quotes]
    if all(r == "VERIFIED" for r in results):
        return "VERIFIED"
    bad = [r for r in results if r != "VERIFIED"]
    return bad[0]


# -- Table parsing and merging -------------------------------------------

TABLE_COLUMNS = [
    "Comparison Item", "Status", "Criticality", "Protocol Text (Source)",
    "ICF Text (Match)", "Confidence", "Readability Score (FK-GL)",
    "Applicable Provision", "Notes",
]

STATUS_RANK = [
    ("INCONSISTENT", 1), ("ABSENT", 2), ("VAGUE", 3),
    ("NON-COMPLIANT", 4), ("JARGON ALERT", 5), ("CONSISTENT", 6),
]


def parse_table_rows(text):
    """Pull nine-column table rows out of model output.

    Returns (rows, unparsed_lines). Lines that look like table rows but do not
    have nine cells are returned untouched so no finding is ever lost.
    """
    rows, unparsed = [], []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells if cell) and any(cells):
            continue
        if cells and cells[0].lower().startswith("comparison item"):
            continue
        if len(cells) == len(TABLE_COLUMNS):
            rows.append(dict(zip(TABLE_COLUMNS, cells)))
        else:
            unparsed.append(stripped)
    return rows, unparsed


def status_rank(status):
    upper = status.upper()
    ranks = [rank for name, rank in STATUS_RANK if re.search(rf"\b{re.escape(name)}\b", upper)]
    return min(ranks) if ranks else 7


def has_status(status, name):
    return bool(re.search(rf"\b{re.escape(name)}\b", status.upper()))


def merge_rows(rows):
    """Merge duplicate items from different protocol parts and sort by severity."""
    merged, seen = [], {}
    for row in rows:
        key = re.sub(r"\W+", " ", row["Comparison Item"].lower()).strip()
        if key in seen:
            existing = seen[key]
            if existing["Status"].upper() == row["Status"].upper():
                existing["Notes"] += f" (also reported in protocol part {row['_part']})"
                if not row.get("_quotes_ok", True):
                    existing["_quotes_ok"] = False
                    existing["Quote check"] = (
                        f"{existing.get('Quote check', '')}; protocol part {row['_part']}: "
                        f"{row.get('Quote check', '')}"
                    ).lstrip("; ")
            else:
                for target in (existing, row):
                    target["Notes"] += " CONFLICT: same item reported with a different status in another protocol part - HUMAN REVIEW REQUIRED."
                merged.append(row)
            continue
        seen[key] = row
        merged.append(row)
    merged.sort(key=lambda r: (status_rank(r["Status"]), r["_part"]))
    return merged


def needs_human_review(row):
    blob = " ".join([row["Notes"], row["Applicable Provision"], row["Status"]]).upper()
    if "HUMAN REVIEW" in blob or "PROVISION NOT VERIFIED" in blob:
        return True
    if row["Confidence"].upper().startswith("LOW"):
        return True
    return not row.get("_quotes_ok", True)


def escape_cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def render_table(rows):
    columns = TABLE_COLUMNS[:6] + ["Readability Score (FK-GL)", "Applicable Provision", "Notes", "Quote check"]
    lines = [
        "| " + " | ".join(columns) + " |",
        "|" + "|".join(["---"] * len(columns)) + "|",
    ]
    for row in rows:
        cells = [
            row["Comparison Item"], row["Status"], row["Criticality"],
            row["Protocol Text (Source)"], row["ICF Text (Match)"], row["Confidence"],
            row["Readability Score (FK-GL)"], row["Applicable Provision"],
            row["Notes"], row["Quote check"],
        ]
        lines.append("| " + " | ".join(escape_cell(c) for c in cells) + " |")
    return "\n".join(lines)


def build_executive_summary(rows, icf, protocol, fk_text, part_count):
    counts = {name: sum(1 for r in rows if has_status(r["Status"], name))
              for name in ("INCONSISTENT", "ABSENT", "VAGUE", "NON-COMPLIANT", "JARGON ALERT")}
    critical = [
        r for r in rows
        if r["Criticality"].upper().startswith("HIGH")
        and any(has_status(r["Status"], n) for n in ("INCONSISTENT", "ABSENT", "NON-COMPLIANT"))
    ]
    review = [r for r in rows if needs_human_review(r)]
    lines = [
        "### EXECUTIVE SUMMARY",
        "",
        f"- Documents: {Path(protocol.path).name} -> Protocol ({part_count} part(s)); "
        f"{Path(icf.path).name} -> ICF.",
        f"- Assessment: {counts['INCONSISTENT']} inconsistencies | {counts['ABSENT']} absent elements | "
        f"{counts['VAGUE']} vague | {counts['NON-COMPLIANT']} non-compliant | "
        f"{counts['JARGON ALERT']} jargon alerts | ICF readability FK-GL {fk_text} (calculated in code).",
        "- CRITICAL ALERTS: " + (
            "; ".join(
                f"{r['Comparison Item']} ({r['Status']}; {r['Applicable Provision'] or 'no provision cited'})"
                + ("" if r.get("_quotes_ok", True) else " [QUOTE NOT VERIFIED]")
                for r in critical) if critical else "None."),
        "- HUMAN REVIEW REQUIRED: " + (
            "; ".join(r["Comparison Item"] for r in review) if review else "None."),
    ]
    return "\n".join(lines)


# -- LLM API -------------------------------------------------------------


@dataclass
class LLMResult:
    text: str
    finish_reason: str


def call_llm(settings, messages):
    """Call an OpenAI-compatible chat-completions API, with retries."""
    url = f"{settings.api_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.model,
        "messages": messages,
        "max_tokens": settings.max_tokens,
        "temperature": settings.temperature,
    }
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=settings.timeout)
        except requests.exceptions.RequestException as exc:
            if attempt == attempts:
                raise LLMError(f"Could not reach the LLM API: {exc}")
            time.sleep(2 ** attempt)
            continue
        if response.status_code in (429, 500, 502, 503, 504) and attempt < attempts:
            time.sleep(2 ** attempt)
            continue
        if response.status_code >= 400:
            fatal = response.status_code not in (429, 500, 502, 503, 504)
            raise LLMError(
                f"LLM API returned HTTP {response.status_code}: {response.text[:500]}",
                fatal=fatal,
            )
        try:
            choice = response.json()["choices"][0]
            return LLMResult(
                text=choice["message"]["content"] or "",
                finish_reason=choice.get("finish_reason") or "unknown",
            )
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise LLMError(f"Unexpected LLM response format: {exc}", fatal=True)
    raise LLMError("LLM call failed.")


# -- Handoff parsing -----------------------------------------------------


def parse_handoff_block(text):
    """Extract the HANDOFF TO CONSISTENCY AUDITOR block from Agent 1's output."""
    match = re.search(
        r"#{2,4}\s*HANDOFF TO CONSISTENCY AUDITOR.*?(?=\n#{1,2} |\Z)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    return match.group(0).strip() if match else None


# -- Input checks ----------------------------------------------------------

STUDY_ID_RE = re.compile(
    r"\b(?:NCT\d{8}"
    r"|[A-Z]{1,6}[-/]\d{2,}(?:[-/][A-Z0-9]+)*"
    r"|\d{4}[-/]\d{2,}[A-Za-z0-9-]*)\b"
)
DATE_LIKE_RE = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}$")

# Common consent/protocol words that say nothing about WHICH study this is.
GENERIC_TERMS = set("""
participant participants participation consent information research voluntary withdraw
withdrawal medical questions procedures procedure benefits benefit confidential
confidentiality following including identified personal collected collection healthcare
treatment relevant required approved committee ethics contact investigator investigators
sponsor results samples sample visits clinical conducted possible however because without
whether between during before another example provided receive received service services
decision decisions reasons records record institution hospital physician patients patient
medication medications laboratory laboratories general standard additional necessary
""".split())


def study_identifiers(doc):
    found = []
    for match in STUDY_ID_RE.findall(doc.plain_text()):
        if DATE_LIKE_RE.match(match) or match in found:
            continue
        found.append(match)
    return found


def distinctive_terms(text, limit=40):
    counts = {}
    for word in re.findall(r"[^\W\d_]{7,}", text.lower()):
        if word not in GENERIC_TERMS:
            counts[word] = counts.get(word, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [w for w, n in ranked if n >= 2][:limit]


@dataclass
class InputCheck:
    warnings: list
    notes: list


def assess_inputs(icf, protocol):
    """Sanity checks on the pair, run before anything is sent. Heuristic, advisory."""
    warnings, notes = [], []

    for doc in (icf, protocol):
        if doc.suspect_pages:
            shown = ", ".join(map(str, doc.suspect_pages[:15]))
            warnings.append(
                f"{doc.label} pages whose text looks garbled (possibly a poor scan): {shown}"
                f"{' ...' if len(doc.suspect_pages) > 15 else ''}. Check them, or re-OCR the file."
            )
        text_len = sum(len(p.strip()) for p in doc.pages)
        if len(doc.pages) >= 3 and text_len / len(doc.pages) < 200:
            warnings.append(
                f"{doc.label} averages under 200 characters of text per page; it may be "
                "mostly images (scanned) with little extractable text."
            )

    icf_ids, protocol_ids = study_identifiers(icf), study_identifiers(protocol)
    notes.append(
        "Identifiers found - ICF: " + (", ".join(icf_ids[:8]) or "none")
        + "; Protocol: " + (", ".join(protocol_ids[:8]) or "none")
        + ". Confirm by eye that these are the same study."
    )
    if icf_ids and protocol_ids and not set(icf_ids) & set(protocol_ids):
        warnings.append(
            "The ICF and the Protocol share no study identifier (protocol number, REB "
            "number, NCT number). Check that they belong to the same study."
        )

    icf_terms = distinctive_terms(icf.plain_text())
    if len(icf_terms) >= 10:
        protocol_words = set(re.findall(r"[^\W\d_]{7,}", protocol.plain_text().lower()))
        overlap = sum(1 for term in icf_terms if term in protocol_words) / len(icf_terms)
        notes.append(f"Study-specific vocabulary from the ICF found in the Protocol: {overlap:.0%}.")
        if overlap < 0.35:
            warnings.append(
                f"Only {overlap:.0%} of the ICF's study-specific vocabulary appears in the "
                "Protocol. They may be from different studies. This is a rough check and "
                "can be wrong."
            )
    return InputCheck(warnings=warnings, notes=notes)


# -- Review chain --------------------------------------------------------


def page_span(first, last):
    return f"p.{first}" if first == last else f"pp.{first}-{last}"


@dataclass
class CallRecord:
    name: str
    finish_reason: str
    chars_sent: int
    output_chars: int
    error: str = ""


@dataclass
class ChainResult:
    chain_dir: Path
    complete: bool
    problems: list
    warnings: list


def _request_chars(messages):
    return sum(len(m["content"]) for m in messages)


# Room kept in each Agent 2 request for the handoff block when planning ahead.
HANDOFF_RESERVE_CHARS = 8000


def _icf_too_large(icf_len, limit):
    return ReviewError(
        f"The ICF is {icf_len:,} characters, which does not fit in one request "
        f"(limit {limit:,}). Raise LLM_MAX_INPUT_CHARS if your model's context window "
        "allows it, or use a model with a larger context. The ICF is never sent in "
        "part, because reviewing only part of a consent form would give a misleading result."
    )


def _no_room_for_protocol(icf_len, room):
    return ReviewError(
        f"The ICF ({icf_len:,} characters) leaves only {max(room, 0):,} characters per "
        "request for the Protocol. Raise LLM_MAX_INPUT_CHARS if your model's context "
        "window allows it, or use a model with a larger context."
    )


def preflight(icf, settings):
    """Fail before anything is sent if the run could not cover both documents.

    Nothing is transmitted to the LLM provider for a run that is bound to fail.
    """
    icf_len = len(icf.marked_text())
    if len(AGENT1_PROMPT) + SCAFFOLD_CHARS + icf_len > settings.max_input_chars:
        raise _icf_too_large(icf_len, settings.max_input_chars)
    room = (settings.max_input_chars - icf_len - len(AGENT2_PROMPT)
            - SCAFFOLD_CHARS - HANDOFF_RESERVE_CHARS)
    if room < MIN_PART_CHARS:
        raise _no_room_for_protocol(icf_len, room)


def run_agent1(icf, protocol, settings, llm, calls, problems):
    icf_text = icf.marked_text()
    protocol_text = protocol.marked_text()
    base = len(AGENT1_PROMPT) + SCAFFOLD_CHARS
    if base + len(icf_text) > settings.max_input_chars:
        raise _icf_too_large(len(icf_text), settings.max_input_chars)
    include_protocol = base + len(icf_text) + len(protocol_text) <= settings.max_input_chars

    facts = [
        "COVERAGE FACTS (supplied by the tool, not by the documents):",
        f"- ICF: complete text supplied, {page_span(1, len(icf.pages))} ({len(icf_text):,} characters).",
    ]
    if icf.empty_pages:
        facts.append(f"- ICF pages with no extractable text: {', '.join(map(str, icf.empty_pages))}.")
    if include_protocol:
        facts.append(f"- Protocol: complete text supplied, {page_span(1, len(protocol.pages))}.")
    else:
        facts.append(
            "- Protocol: NOT supplied to this step because it exceeds the request size limit. "
            "Review the ICF alone and say so. The Protocol is compared with the ICF in the next step."
        )

    sections = ["Review the following ICF" + (" and Protocol documents." if include_protocol else "."), "\n".join(facts)]
    if include_protocol:
        sections.append(f"PROTOCOL:\n\n{protocol_text}")
    sections.append(f"ICF:\n\n{icf_text}")
    sections.append(
        "Produce the Pre-Review output with OVERALL IMPRESSION, KEY ISSUES & RECOMMENDATIONS "
        "table, OPTIONAL ENHANCEMENTS, and HANDOFF TO CONSISTENCY AUDITOR block. "
        "Respond only with these sections and no additional commentary."
    )
    messages = [
        {"role": "system", "content": AGENT1_PROMPT},
        {"role": "user", "content": "\n\n".join(sections)},
    ]
    result = llm(settings, messages)
    calls.append(CallRecord("agent1", result.finish_reason, _request_chars(messages), len(result.text)))
    if result.finish_reason == "length":
        problems.append(
            "Agent 1 output was cut off at the token limit (LLM_MAX_TOKENS). "
            "Its findings may be incomplete."
        )
    return result.text, include_protocol


def run_agent2(icf, protocol, handoff, fk_text, settings, llm, calls, problems, chain_dir):
    icf_text = icf.marked_text()
    handoff_text = handoff or "No handoff block provided."
    reserve = len(AGENT2_PROMPT) + len(handoff_text) + len(fk_text) + SCAFFOLD_CHARS
    part_budget = settings.max_input_chars - len(icf_text) - reserve
    if part_budget < MIN_PART_CHARS:
        raise _no_room_for_protocol(len(icf_text), part_budget)
    parts = chunk_document(protocol, part_budget)
    print(f"Protocol split into {len(parts)} part(s) of at most {part_budget:,} characters.")

    parts_dir = chain_dir / "agent2_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    all_rows, unparsed_lines, raw_outputs = [], [], []
    parts_done = 0

    for part in parts:
        if len(parts) == 1:
            scope = f"complete Protocol, {page_span(1, len(protocol.pages))}"
        else:
            scope = (
                f"part {part.index} of {part.total}, {page_span(part.page_start, part.page_end)} "
                f"of {len(protocol.pages)} pages; other parts are reviewed in separate calls"
            )
        user = (
            f"HANDOFF FROM AGENT 1:\n\n{handoff_text}\n\n"
            f"TOOL-CALCULATED READABILITY OF THE ICF (Flesch-Kincaid grade level): {fk_text}\n\n"
            f"PROTOCOL ({scope}):\n\n{part.text}\n\n"
            f"ICF (complete, {page_span(1, len(icf.pages))}):\n\n{icf_text}\n\n"
            "Follow the Agent 2 prompt instructions exactly. Step 0: consume the handoff block. "
            "Steps 3-4: bidirectional comparison. Step 5: output the Comparison Table only."
        )
        messages = [{"role": "system", "content": AGENT2_PROMPT}, {"role": "user", "content": user}]
        label = f"agent2 part {part.index}/{part.total}"
        print(f"  {label} ({page_span(part.page_start, part.page_end)}) ...")
        try:
            result = llm(settings, messages)
        except LLMError as exc:
            if exc.fatal:
                raise
            calls.append(CallRecord(label, "error", _request_chars(messages), 0, str(exc)))
            problems.append(f"Protocol part {part.index} of {part.total} ({page_span(part.page_start, part.page_end)}) failed: {exc}")
            continue

        calls.append(CallRecord(label, result.finish_reason, _request_chars(messages), len(result.text)))
        (parts_dir / f"part_{part.index:02d}_raw.md").write_text(result.text, encoding="utf-8")
        raw_outputs.append((part, result.text))
        if result.finish_reason == "length":
            problems.append(
                f"Agent 2 output for protocol part {part.index} of {part.total} was cut off at the "
                "token limit (LLM_MAX_TOKENS). Rows are missing."
            )
        rows, unparsed = parse_table_rows(result.text)
        if not rows and not unparsed and not re.search(r"none identified", result.text, re.IGNORECASE):
            problems.append(
                f"Agent 2 output for protocol part {part.index} of {part.total} contained no "
                "readable table; see agent2_parts/ for the raw text."
            )
        for row in rows:
            row["_part"] = part.index
        all_rows.extend(rows)
        unparsed_lines.extend(unparsed)
        parts_done += 1

    return all_rows, unparsed_lines, raw_outputs, len(parts), parts_done


def verify_agent2_rows(rows, icf_verifier, protocol_verifier):
    """Attach quote checks and code-calculated readability to each row."""
    for row in rows:
        protocol_check = check_cell(row["Protocol Text (Source)"], protocol_verifier)
        icf_check = check_cell(row["ICF Text (Match)"], icf_verifier)
        pieces = []
        if protocol_check:
            pieces.append(f"Protocol: {protocol_check}")
        if icf_check:
            pieces.append(f"ICF: {icf_check}")
        row["Quote check"] = "; ".join(pieces) if pieces else "no quotes to check"
        row["_quotes_ok"] = all(c == "VERIFIED" for c in (protocol_check, icf_check) if c)
        icf_quotes = quotes_in_cell(row["ICF Text (Match)"])
        if icf_quotes:
            row["Readability Score (FK-GL)"] = readability_label(" ".join(icf_quotes), min_words=15)
        else:
            row["Readability Score (FK-GL)"] = "n/a"


def verify_agent1_quotes(text, icf_verifier, protocol_verifier):
    """Check every sizeable quotation in Agent 1's output."""
    results = []
    for quote in QUOTED_SEGMENT_RE.findall(text):
        quote = quote.strip()
        if len(quote.split()) < 6:
            continue
        status = icf_verifier.verify(quote)
        where = "ICF"
        if not status.startswith("VERIFIED"):
            other = protocol_verifier.verify(quote)
            if other.startswith("VERIFIED") or (status == "NOT FOUND" and other != "NOT FOUND"):
                status, where = other, "Protocol"
        results.append((quote, status, where))
    return results


def run_review_chain(icf, protocol, settings, output_dir, llm=call_llm, input_check=None):
    """Run Agent 1 -> Agent 2 -> unified report. Never reviews part of the ICF."""
    preflight(icf, settings)
    input_check = input_check or assess_inputs(icf, protocol)
    problems = []  # coverage gaps: the review is INCOMPLETE
    warnings = list(input_check.warnings)  # nothing lost, but a person should know
    calls = []
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    chain_dir = Path(output_dir) / f"review-{timestamp}"
    chain_dir.mkdir(parents=True, exist_ok=True)

    icf_text_plain = icf.plain_text()
    fk_text = readability_label(icf_text_plain)
    icf_verifier = QuoteVerifier(icf)
    protocol_verifier = QuoteVerifier(protocol)

    for doc in (icf, protocol):
        if doc.empty_pages:
            problems.append(
                f"{doc.label} pages with no extractable text (possibly scanned): "
                f"{', '.join(map(str, doc.empty_pages))}. Those pages were not reviewed."
            )

    try:
        print("=== Step 1: Agent 1 (Pre-Review) ===")
        agent1_output, agent1_had_protocol = run_agent1(icf, protocol, settings, llm, calls, problems)
        print(f"Agent 1 output: {len(agent1_output)} chars")
        handoff = parse_handoff_block(agent1_output)
        print(f"Handoff block: {'found' if handoff else 'NOT FOUND'}")
        if not handoff:
            warnings.append(
                "Agent 1 produced no handoff block, so Agent 2 re-derived context from the documents."
            )
        (chain_dir / "agent1_output.md").write_text(agent1_output, encoding="utf-8")

        print("\n=== Step 2: Agent 2 (Consistency Auditor) ===")
        rows, unparsed, raw_outputs, part_count, parts_done = run_agent2(
            icf, protocol, handoff, fk_text, settings, llm, calls, problems, chain_dir
        )
        if parts_done < part_count:
            problems.append(
                f"Only {parts_done} of {part_count} protocol parts were reviewed successfully."
            )


    except ReviewError as exc:
        failure = {
            "generated": datetime.now().isoformat(timespec="seconds"),
            "review_status": "FAILED",
            "error": str(exc),
            "problems": problems,
            "warnings": warnings,
            "calls": [call.__dict__ for call in calls],
        }
        (chain_dir / "run_metadata.json").write_text(json.dumps(failure, indent=2), encoding="utf-8")
        saved = sorted(p.name for p in chain_dir.rglob("*") if p.is_file())
        raise ReviewError(
            f"{exc}\nWork completed before the failure is saved in {chain_dir} "
            f"({', '.join(saved)})."
        ) from exc

    verify_agent2_rows(rows, icf_verifier, protocol_verifier)
    merged = merge_rows(rows)
    unverified = [r for r in merged if not r["_quotes_ok"]]

    summary = build_executive_summary(merged, icf, protocol, fk_text, part_count)
    agent2_output = f"{summary}\n\n### COMPARISON TABLE\n\n"
    agent2_output += render_table(merged) if merged else "None identified."
    if unparsed:
        agent2_output += (
            "\n\n### UNPARSED TABLE LINES (model output that did not match the nine-column "
            "format; review manually)\n\n" + "\n".join(unparsed)
        )
        warnings.append(
            f"{len(unparsed)} table line(s) from Agent 2 did not match the nine-column format. "
            "They are reproduced verbatim at the end of the Agent 2 section and are not in the "
            "summary counts."
        )
    (chain_dir / "agent2_output.md").write_text(agent2_output, encoding="utf-8")

    agent1_quotes = verify_agent1_quotes(agent1_output, icf_verifier, protocol_verifier)
    agent1_bad = [q for q in agent1_quotes if q[1] != "VERIFIED"]

    complete = not problems
    unified = merge_reports(
        icf, protocol, settings, complete, problems, warnings, input_check.notes, calls, fk_text, part_count, parts_done,
        agent1_had_protocol, bool(handoff), agent1_output, agent2_output, handoff,
        merged, unverified, agent1_quotes, agent1_bad,
    )
    (chain_dir / "unified_report.md").write_text(unified, encoding="utf-8")

    metadata = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "model": settings.model,
        "api_url": settings.api_url,
        "max_tokens": settings.max_tokens,
        "temperature": settings.temperature,
        "max_input_chars": settings.max_input_chars,
        "review_status": "COMPLETE" if complete else "INCOMPLETE",
        "problems": problems,
        "warnings": warnings,
        "inputs": {
            doc.label: {
                "path": doc.path, "sha256": doc.sha256, "pages": len(doc.pages),
                "empty_pages": doc.empty_pages,
            }
            for doc in (icf, protocol)
        },
        "calls": [call.__dict__ for call in calls],
    }
    (chain_dir / "run_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    print("\n=== Step 3: Unified Report ===")
    print(f"Review status: {'COMPLETE' if complete else 'INCOMPLETE'}")
    for problem in problems:
        print(f"  PROBLEM: {problem}")
    for warning in warnings:
        print(f"  WARNING: {warning}")
    print(f"Output directory: {chain_dir}")
    for item in sorted(chain_dir.iterdir()):
        if item.is_file():
            print(f"  {item.name} ({item.stat().st_size} bytes)")
    return ChainResult(chain_dir=chain_dir, complete=complete, problems=problems, warnings=warnings)


def merge_reports(icf, protocol, settings, complete, problems, warnings, notes, calls, fk_text, part_count,
                  parts_done, agent1_had_protocol, had_handoff, agent1_output, agent2_output,
                  handoff, merged, unverified, agent1_quotes, agent1_bad):
    """Assemble the unified report, with coverage stated up front."""
    status = "COMPLETE" if complete else "INCOMPLETE - HUMAN REVIEW REQUIRED"
    lines = [
        "# ICF Review Report - Unified",
        "",
        "**Advisory only - not an REB determination. Human REB makes final decisions per "
        "TCPS 2 Art. 6.4, 6.8-6.11 and MUHC SOP 401.001-1.**",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Model: {settings.model}",
        "",
        f"## Review status: {status}",
        "",
    ]
    if problems:
        lines.append("Problems that limit this review:")
        lines.append("")
        lines.extend(f"- {p}" for p in problems)
        lines.append("")
    if warnings:
        lines.append("Warnings:")
        lines.append("")
        lines.extend(f"- {w}" for w in warnings)
        lines.append("")
    lines += [
        "## Coverage and Verification",
        "",
        f"- ICF: {Path(icf.path).name} - {len(icf.pages)} page(s), sent complete to every call. SHA-256 {icf.sha256}",
        f"- Protocol: {Path(protocol.path).name} - {len(protocol.pages)} page(s), reviewed in "
        f"{parts_done} of {part_count} part(s). SHA-256 {protocol.sha256}",
        f"- Agent 1 saw the Protocol: {'yes' if agent1_had_protocol else 'no (too large; ICF-only pre-review)'}",
        f"- Handoff block found: {'yes' if had_handoff else 'no'}",
        f"- ICF readability, Flesch-Kincaid grade level (calculated in code): {fk_text}",
        f"- Agent 2 rows: {len(merged)}; rows with a quote that could not be fully verified: {len(unverified)}",
        f"- Agent 1 quotations checked: {len(agent1_quotes)}; not verified: {len(agent1_bad)}",
        f"- LLM calls: {len(calls)}",
        *[f"- {note}" for note in notes],
        "",
        "## Agent 1: Pre-Review",
        "",
        agent1_output,
        "",
        "## Agent 2: Consistency Auditor (merged across Protocol parts)",
        "",
        agent2_output,
        "",
        "## Handoff Block (Agent 1 -> Agent 2)",
        "",
        handoff or "No handoff block found.",
        "",
    ]
    if agent1_bad:
        lines += [
            "## Agent 1 quotations not verified in the source documents",
            "",
            "These may be paraphrases or suggested wording rather than quotations. Check before relying on them.",
            "",
        ]
        lines.extend(f'- "{quote}" - {status}' for quote, status, _ in agent1_bad)
        lines.append("")
    return "\n".join(lines)


# -- Command line --------------------------------------------------------


def get_file_path(prompt):
    path = input(f"{prompt}: ").strip()
    if not path:
        raise ReviewError("No file provided.")
    return path


def build_parser():
    parser = argparse.ArgumentParser(
        description="ICF Review Chain - Agent 1 (Pre-Review) -> Agent 2 (Consistency Auditor) -> Unified Report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Interactive mode (prompts for everything)
  python3 review_chain_standalone.py

  # With file paths
  python3 review_chain_standalone.py icf.pdf protocol.pdf

  # With custom output directory
  python3 review_chain_standalone.py icf.pdf protocol.pdf ./output

  # ICF text from stdin (plain text only, not PDF)
  cat icf.txt | python3 review_chain_standalone.py --stdin-icf - protocol.pdf

Environment variables:
  LLM_API_URL          OpenAI-compatible API endpoint (default: https://api.openai.com/v1)
  LLM_API_KEY          API key (can also be saved to ~/.icf-review-chain/config.json)
  LLM_MODEL            Model name (default: gpt-4o)
  LLM_MAX_TOKENS       Max output tokens per call (default: 8192)
  LLM_TEMPERATURE      Sampling temperature (default: 0.3)
  LLM_MAX_INPUT_CHARS  Max characters in one request (default: 300000)
  LLM_TIMEOUT          Seconds to wait for each response (default: 600)

Exit status: 0 = review complete, 1 = error, 2 = review finished but INCOMPLETE.
""",
    )
    parser.add_argument("icf_file", nargs="?", help="ICF file (PDF or text)")
    parser.add_argument("protocol_file", nargs="?", help="Protocol file (PDF or text)")
    parser.add_argument("output_dir", nargs="?", default=None,
                        help="Output directory (default: ./deliberations)")
    parser.add_argument("--stdin-icf", action="store_true",
                        help="Read the ICF as plain text from stdin instead of a file")
    parser.add_argument("--yes", action="store_true",
                        help="Do not pause to confirm when the input checks raise warnings")
    return parser


def resolve_paths(args):
    """Work out (icf_path, protocol_path, output_dir) from the positional arguments.

    With --stdin-icf the ICF comes from stdin, so a leading "-" is optional:
    both "--stdin-icf - protocol.pdf out" and "--stdin-icf protocol.pdf out" work.
    """
    values = [v for v in (args.icf_file, args.protocol_file, args.output_dir) if v is not None]
    if args.stdin_icf:
        if values and values[0] == "-":
            values = values[1:]
        protocol_path = values[0] if values else None
        output_dir = values[1] if len(values) > 1 else None
        return None, protocol_path, output_dir or "./deliberations"
    return args.icf_file, args.protocol_file, args.output_dir or "./deliberations"


def run_cli(args):
    settings = load_settings()
    icf_path, protocol_path, output_dir = resolve_paths(args)

    if args.stdin_icf:
        icf = document_from_text(sys.stdin.read(), "ICF", "<stdin>")
    else:
        icf_path = icf_path or get_file_path("Enter ICF file path")
        print(f"Reading ICF: {icf_path}")
        icf = extract_document(icf_path, "ICF")
    print(f"  {len(icf.pages)} page(s), {len(icf.plain_text()):,} chars")

    protocol_path = protocol_path or get_file_path("Enter Protocol file path")
    print(f"Reading Protocol: {protocol_path}")
    protocol = extract_document(protocol_path, "PROTOCOL")
    print(f"  {len(protocol.pages)} page(s), {len(protocol.plain_text()):,} chars")

    input_check = assess_inputs(icf, protocol)
    if input_check.warnings:
        print("\nInput checks raised warnings:")
        for warning in input_check.warnings:
            print(f"  - {warning}")
        interactive = False
        try:
            interactive = sys.stdin.isatty()
        except (AttributeError, ValueError):
            pass
        if interactive and not args.yes:
            answer = input("Send these documents to the LLM anyway? [y/N]: ").strip().lower()
            if answer not in ("y", "yes"):
                raise ReviewError("Stopped before sending anything, at your request.")
        print()

    result = run_review_chain(icf, protocol, settings, output_dir, input_check=input_check)
    print(f"\nDone. Unified report: {result.chain_dir / 'unified_report.md'}")
    return 0 if result.complete else 2


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return run_cli(args)
    except ReviewError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
