import os, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import make_fixtures
os.chdir(tempfile.mkdtemp(prefix="icf-chain-tests-"))   # fixtures and outputs live here
make_fixtures.build_all()
import re
import review_chain_standalone as rc

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond: fails.append(name)

protocol = rc.extract_document("protocol.pdf", "PROTOCOL")
icf = rc.extract_document("icf.pdf", "ICF")
check("PDF pages extracted", len(protocol.pages) == 30 and len(icf.pages) == 5, f"{len(protocol.pages)}/{len(icf.pages)}")
check("no empty pages in good PDFs", not protocol.empty_pages and not icf.empty_pages)

# --- chunking never loses text, respects the budget ---
full = rc.PAGE_MARKER_ANYWHERE_RE.sub(" ", protocol.marked_text())
for budget in (60000, 20000, 8000, 4000):
    chunks = rc.chunk_document(protocol, budget)
    joined = rc.PAGE_MARKER_ANYWHERE_RE.sub(" ", "\n".join(c.text for c in chunks))
    same = rc._alnum_key(full) == rc._alnum_key(joined)
    biggest = max(len(c.text) for c in chunks)
    check(f"chunking budget={budget}: no text lost", same, f"{len(chunks)} parts")
    check(f"chunking budget={budget}: every part within budget", biggest <= budget, f"largest {biggest}")
    check(f"chunking budget={budget}: every part starts with a page marker",
          all(rc.PAGE_MARKER_RE.match(c.text.splitlines()[0]) for c in chunks))
chunks = rc.chunk_document(protocol, 20000)
check("chunks cover pages 1..30 in order",
      chunks[0].page_start == 1 and chunks[-1].page_end == 30 and
      all(chunks[i].page_end <= chunks[i+1].page_start + 1 for i in range(len(chunks)-1)))
heading_starts = sum(1 for c in chunks[1:] if rc.is_heading_line(c.text.splitlines()[1]))
check("parts tend to begin at a section heading", heading_starts >= len(chunks) - 2, f"{heading_starts}/{len(chunks)-1}")

# --- a single enormous line is hard-split, not dropped ---
big = rc._build_doc("PROTOCOL", "x", "0", ["A" * 50000 + " end"])
bc = rc.chunk_document(big, 10000)
check("oversized single line is split, nothing dropped",
      sum(len(re.sub(r"\[\[PAGE \d+\]\]\n?", "", c.text)) for c in bc) >= 50000, f"{len(bc)} parts")

# --- quote verification ---
v = rc.QuoteVerifier(protocol)
real = "Participants randomised to the placebo arm will receive an inactive tablet and will not be told which arm they have been assigned to"
check("verbatim quote across PDF line breaks verified", v.verify(real, {17}) == "VERIFIED", v.verify(real, {17}))
check("curly quotes/case/spacing do not matter", v.verify("PARTICIPANTS randomised  to the PLACEBO arm will receive an inactive tablet", {17}) == "VERIFIED")
check("fabricated quote NOT FOUND", v.verify("Participants will be paid 500 dollars per visit in cash", {17}) == "NOT FOUND")
check("one altered word is caught", v.verify(real.replace("inactive", "active"), {17}) == "NOT FOUND")
check("wrong page flagged", v.verify(real, {3}).startswith("VERIFIED, PAGE MISMATCH"), v.verify(real, {3}))
check("ellipsis fragments each checked",
      v.verify("Participants randomised to the placebo arm ... until the study is unblinded", {17}) == "VERIFIED")
check("[REDACTED] treated as a gap",
      v.verify("Participants randomised to [REDACTED] the placebo arm will receive an inactive tablet", {17}) == "VERIFIED"
      or v.verify("Participants randomised to [REDACTED] the placebo arm will receive an inactive tablet", {17}).startswith("PARTIAL"))
check("half-real, half-invented is PARTIAL",
      v.verify("Participants randomised to the placebo arm ... and will receive a monthly cash bonus of fifty dollars", {17}).startswith("PARTIAL"))
check("very short quote not trusted", v.verify("the", set()) == "TOO SHORT TO CHECK")

# --- cell parsing ---
check("cited pages parsed", rc.cited_pages('"x" (p.12)') == {12} and rc.cited_pages('(pp.12-14)') == {12, 13, 14})
check("ABSENT cell has no quote", rc.quotes_in_cell("ABSENT") is None)
check("two quotes in a cell both returned",
      len(rc.quotes_in_cell('"first quote here" and "second quote here" (p.2)')) == 2)

# --- readability ---
easy = "The cat sat on the mat. The dog ran to the park. We like to play. It is a nice day. " * 10
hard = ("Participants undergoing pharmacokinetic evaluation must acknowledge comprehensive documentation "
        "regarding anticipated physiological complications and subsequent therapeutic interventions. " * 10)
fe, fh = rc.flesch_kincaid_grade(easy), rc.flesch_kincaid_grade(hard)
check("FK: easy text low, hard text high", fe is not None and fh is not None and fe < 4 and fh > 14, f"{fe:.1f} vs {fh:.1f}")
check("FK: too little text -> None", rc.flesch_kincaid_grade("Short text.") is None)
french = "Vous pouvez poser des questions sur cette etude et votre participation est volontaire pour la recherche. " * 5
check("French text not given an English FK score", rc.readability_label(french).startswith("n/a"), rc.readability_label(french)[:40])
check("English text gets a number", re.fullmatch(r"-?\d+\.\d", rc.readability_label(easy)) is not None)

# --- table parsing ---
sample = """| Comparison Item | Status | Criticality | Protocol Text (Source) | ICF Text (Match) | Confidence | Readability Score (FK-GL) | Applicable Provision | Notes |
|---|---|---|---|---|---|---|---|---|
| Blinding | CONSISTENT | HIGH | "a" (p.1) | "b" (p.2) | HIGH | n/a | N/A | ok |
| Bad row | only | three cells |
"""
rows, unparsed = rc.parse_table_rows(sample)
check("good row parsed, header and separator skipped", len(rows) == 1 and rows[0]["Comparison Item"] == "Blinding")
check("malformed row kept as unparsed, not lost", len(unparsed) == 1)
check("INCONSISTENT is not mistaken for CONSISTENT",
      rc.has_status("INCONSISTENT", "CONSISTENT") is False and rc.status_rank("INCONSISTENT") == 1)
check("status ranking", rc.status_rank("CONSISTENT + JARGON ALERT") == 5 and rc.status_rank("ABSENT") == 2)

# --- handoff parsing ---
h = rc.parse_handoff_block("blah\n\n### HANDOFF TO CONSISTENCY AUDITOR (machine-readable)\n\n**Jurisdiction:** QC\n\n1. x\n")
check("handoff block parsed", h is not None and "Jurisdiction" in h)

# --- settings ---
for k in ("LLM_API_KEY","LLM_MAX_TOKENS","LLM_TEMPERATURE","LLM_MAX_INPUT_CHARS","LLM_TIMEOUT","LLM_MODEL","LLM_API_URL"):
    os.environ.pop(k, None)
rc.CONFIG_FILE = rc.Path("/nonexistent/config.json")
os.environ["LLM_API_KEY"] = "k"
s = rc.load_settings()
check("settings defaults defined (the original NameError)", s.max_tokens == 8192 and s.temperature == 0.3 and s.max_input_chars == 300000)
os.environ["LLM_MAX_TOKENS"] = "abc"
try:
    rc.load_settings(); check("invalid LLM_MAX_TOKENS rejected", False)
except rc.ReviewError as e:
    check("invalid LLM_MAX_TOKENS rejected with a clear message", "LLM_MAX_TOKENS" in str(e), str(e))
os.environ["LLM_MAX_TOKENS"] = "1000"; os.environ["LLM_TEMPERATURE"] = "0.0"
s = rc.load_settings()
check("env overrides honoured", s.max_tokens == 1000 and s.temperature == 0.0)

# --- blank page detection ---
blank = rc.extract_document("icf_with_blank_page.pdf", "ICF")
check("blank/scanned page detected", blank.empty_pages == [3], str(blank.empty_pages))


# --- garbled-text detection (poor scans) ---
clean = ("Participation in this study is voluntary and you may withdraw at any time without penalty. " * 4)
noise = " ".join(["xqzt brkw pltnk grmbl shrdl qwrty zxcvb nmbrs ktchn"] * 6)
check("normal prose is not flagged as garbled", rc.looks_garbled(clean) is False)
check("vowel-less noise is flagged as garbled", rc.looks_garbled(noise) is True)
check("a short page is never flagged", rc.looks_garbled("xqzt brkw") is False)

# --- study identifiers and pairing check ---
doc = rc.document_from_text("Protocol ABC-123, REB 2026-041, registry NCT01234567, signed 2026-03-15.", "P", "p")
ids = rc.study_identifiers(doc)
check("identifiers found, dates ignored", ids == ["ABC-123", "2026-041", "NCT01234567"], str(ids))

def mk(label, text):
    return rc.document_from_text(text, label, label)
MEL = "vemurafenib cardiotoxicity melanoma oncology chemotherapy dermatology biopsy radiotherapy immunotherapy metastatic carcinoma lesions "
same_a = mk("ICF", "Study ABC-123 tests " + MEL * 6)
same_b = mk("PROTOCOL", "Protocol ABC-123 evaluates " + MEL * 6)
other = mk("PROTOCOL", "Protocol XYZ-999: " + "arthroplasty rehabilitation physiotherapy osteoarthritis orthopaedic prosthesis mobility cartilage ligament " * 6)
check("matching pair: no pairing warning at all", not rc.assess_inputs(same_a, same_b).warnings,
      str(rc.assess_inputs(same_a, same_b).warnings))
mismatch = rc.assess_inputs(same_a, other)
check("different identifiers raise a warning", any("share no study identifier" in w for w in mismatch.warnings))
check("different vocabulary raises a warning", any("different studies" in w for w in mismatch.warnings), str(mismatch.warnings))
check("identifiers are listed for the person to compare", any("Identifiers found" in n for n in mismatch.notes))

# --- API key prompt: hidden entry, asks before saving ---
import builtins, stat, tempfile
tmp = rc.Path(tempfile.mkdtemp())
rc.CONFIG_DIR, rc.CONFIG_FILE = tmp / "cfg", tmp / "cfg" / "config.json"
os.environ.pop("LLM_API_KEY", None)

class Tty:
    def isatty(self): return True
    def read(self): return ""
real_stdin, real_input, real_getpass = sys.stdin, builtins.input, rc.getpass.getpass
sys.stdin = Tty(); rc.getpass.getpass = lambda prompt="": "sk-secret"
try:
    builtins.input = lambda prompt="": "n"
    cfg = {}
    key = rc.get_api_key(cfg)
    check("key accepted for this run", key == "sk-secret")
    check("declining to save writes nothing", not rc.CONFIG_FILE.exists() and "llm_api_key" not in cfg)
    builtins.input = lambda prompt="": "y"
    rc.get_api_key(cfg)
    check("agreeing to save writes the file", rc.CONFIG_FILE.exists())
    if os.name == "posix":
        mode = stat.S_IMODE(rc.CONFIG_FILE.stat().st_mode)
        check("saved config is owner-only (0600)", mode == 0o600, oct(mode))
finally:
    sys.stdin, builtins.input, rc.getpass.getpass = real_stdin, real_input, real_getpass

# --- wrong file types ---
import zipfile
with zipfile.ZipFile("fake.txt", "w") as z:
    z.writestr("word/document.xml", "<w:document/>")
try:
    rc.extract_document("fake.txt", "ICF"); check("Word file renamed .txt rejected", False)
except rc.ReviewError as e:
    check("Word file renamed .txt rejected", "binary" in str(e), str(e)[:80])
open("notreally.pdf", "w").write("this is plain text, not a pdf")
try:
    rc.extract_document("notreally.pdf", "ICF"); check("text file renamed .pdf rejected", False)
except rc.ReviewError as e:
    check("text file renamed .pdf rejected", "Could not read" in str(e), str(e)[:80])

# --- a part never exceeds its budget, even after a blank-line cut ---
para = " ".join(["The protocol describes procedures in detail."] * 20)   # ~900 chars
awkward = rc.document_from_text("\n".join(["short line"] * 60 + [""] + [para] * 18 + ["word " * 3200]), "PROTOCOL", "p")
sizes = [len(c.text) for c in rc.chunk_document(awkward, 17983)]
check("blank-line cut followed by long lines stays within budget", max(sizes) <= 17983, str(sizes))
tidy = rc.document_from_text("\n".join(["short line"] * 60 + [""] + [para] * 3), "PROTOCOL", "p")
check("a section that fits is not split", len(rc.chunk_document(tidy, 17983)) == 1)

# --- quote checks on the facts that matter: numbers, negation, arms ---
facts = rc.document_from_text(
    "Group A will receive the study drug. Group B will receive placebo. The study lasts 12 weeks. "
    "You will be paid $10.00 per visit. The dose is 1.5 mg daily. Participants will receive "
    "compensation for travel. We draw up to 100 mL of blood.", "ICF", "facts")
fv = rc.QuoteVerifier(facts)
for quote, expected in [
    ("You will be paid $10.00 per visit", "VERIFIED"),
    ("You will be paid $1,000 per visit", "NOT FOUND"),
    ("The dose is 1.5 mg daily", "VERIFIED"),
    ("The dose is 15 mg daily", "NOT FOUND"),
    ("We draw up to 100 mL", "VERIFIED"),
    ("We draw up to 10", "NOT FOUND"),
    ("Participants will receive compensation", "VERIFIED"),
    ("Participants will [not] receive compensation", "NOT FOUND"),
    ("Group B will receive placebo. The study lasts 12 weeks", "VERIFIED"),
    ("Group B will receive [...] lasts 12 weeks", "VERIFIED"),
]:
    got = fv.verify(quote)
    check(f"quote {quote!r} -> {expected}", got == expected, got)
check("short fragment after a gap is not trusted",
      fv.verify("Group A will receive [...] placebo").startswith("PARTIAL"), fv.verify("Group A will receive [...] placebo"))
check("fragments out of order are not VERIFIED",
      fv.verify("The study lasts 12 weeks ... Group B will receive placebo").startswith("PARTIAL"))
far = rc.document_from_text("The first sentence of the form is here. " + "filler words " * 400 + "The last sentence of the form is here.", "ICF", "far")
check("fragments too far apart are not VERIFIED",
      rc.QuoteVerifier(far).verify("The first sentence of the form ... The last sentence of the form").startswith("PARTIAL"))

# --- merged duplicate keeps a failed quote check ---
dup = [dict(zip(rc.TABLE_COLUMNS, ["Item", "VAGUE", "LOW", "", "", "", "", "", "n"]), _part=1, _quotes_ok=True),
       dict(zip(rc.TABLE_COLUMNS, ["Item", "VAGUE", "LOW", "", "", "", "", "", "n"]), _part=2, _quotes_ok=False)]
for row in dup:
    row["Quote check"] = "Protocol: VERIFIED" if row["_quotes_ok"] else "Protocol: NOT FOUND"
merged = rc.merge_rows(dup)
check("duplicate with an unverified quote keeps the row flagged", len(merged) == 1 and merged[0]["_quotes_ok"] is False)
clean_dup = [dict(r, _quotes_ok=True) for r in dup]
check("duplicate with verified quotes stays verified", rc.merge_rows(clean_dup)[0]["_quotes_ok"] is True)

# --- text files in Windows-1252 are decoded, not garbled ---
open("fr_cp1252.txt", "wb").write("Votre participation est volontaire et sans préjudice à vos soins.".encode("cp1252"))
fr = rc.extract_document("fr_cp1252.txt", "ICF")
check("cp1252 text decoded correctly", "préjudice à" in fr.pages[0], fr.pages[0][:60])
open("fr_utf8.txt", "w", encoding="utf-8").write("Votre participation est volontaire et sans préjudice.")
check("UTF-8 text still decoded as UTF-8", "préjudice" in rc.extract_document("fr_utf8.txt", "ICF").pages[0])

print()
print("FAILED:" if fails else "ALL UNIT CHECKS PASSED", fails if fails else "")
sys.exit(1 if fails else 0)
