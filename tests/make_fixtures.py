"""Build the invented test documents used by unit_tests.py and e2e_tests.py.

Everything here is made up for testing: an invented study (ABC-123) with a
30-page protocol, a 5-page consent form, and the same consent form with a blank
third page standing in for a scanned page with no text layer. No real protocol
or consent form is used. Requires reportlab (pip install reportlab).

build_all() writes the PDFs into the current directory.
"""
import textwrap

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

STUDY = "ABC-123"
DRUG_TERMS = ("vemurafenib", "melanoma", "cardiotoxicity", "echocardiogram", "pharmacokinetic")

# Page 17 of the protocol holds the passage the unit tests quote. It is long
# enough to wrap, so the quote crosses a PDF line break.
PLACEBO_PASSAGE = (
    "Participants randomised to the placebo arm will receive an inactive tablet and will not be "
    "told which arm they have been assigned to until the study is unblinded at the final visit."
)

PROTOCOL_SECTIONS = [
    "Background and Rationale", "Study Objectives", "Study Design", "Eligibility Criteria",
    "Recruitment", "Randomisation", "Study Treatment", "Visit Schedule", "Sampling Schedule",
    "Cardiac Monitoring", "Pharmacokinetic Assessments", "Adverse Events", "Withdrawal",
    "Data Management", "Confidentiality", "Statistical Analysis", "Blinding and Unblinding",
    "Safety Review", "Biological Samples", "Compensation", "Insurance", "Monitoring",
    "Protocol Deviations", "Record Retention", "Publication", "Ethics Review",
    "Informed Consent Process", "Study Closure", "Responsibilities", "References",
]


def _protocol_page(number):
    """Text lines for one protocol page; every sentence names its page, so
    any two consecutive lines are unique to that page."""
    section = PROTOCOL_SECTIONS[number - 1]
    lines = [f"{number} {section}", ""]
    sentences = []
    for item in range(1, 15):
        term = DRUG_TERMS[(number + item) % len(DRUG_TERMS)]
        sentences.append(
            f"On protocol page {number}, step {item} of the {section.lower()} procedure for study "
            f"{STUDY} records the {term} findings for every enrolled participant."
        )
    if number == 17:
        sentences.insert(3, PLACEBO_PASSAGE)
    for start in range(0, len(sentences), 4):
        lines.extend(textwrap.wrap(" ".join(sentences[start:start + 4]), 95))
        lines.append("")
    return lines


def _icf_page(number):
    lines = [f"Information and consent form, study {STUDY}, page {number}", ""]
    for item in range(1, 6):
        paragraph = (
            f"You are free to ask questions about part {number}.{item} of this study at any time. "
            "Your participation is voluntary and you may stop whenever you wish without any effect "
            f"on your care. This study tests vemurafenib for melanoma and checks for cardiotoxicity "
            f"with an echocardiogram and pharmacokinetic blood tests."
        )
        lines.extend(textwrap.wrap(paragraph, 95))
        lines.append("")
    return lines


def _write_pdf(path, pages):
    pdf = canvas.Canvas(path, pagesize=letter)
    for lines in pages:
        pdf.setFont("Helvetica", 9)
        y = 750
        for line in lines:
            pdf.drawString(40, y, line)
            y -= 11
        pdf.showPage()  # an empty list gives a blank page (no text layer)
    pdf.save()


def build_all():
    protocol = [_protocol_page(n) for n in range(1, 31)]
    icf = [_icf_page(n) for n in range(1, 6)]
    _write_pdf("protocol.pdf", protocol)
    _write_pdf("icf.pdf", icf)
    _write_pdf("icf_with_blank_page.pdf", icf[:2] + [[]] + icf[3:])


if __name__ == "__main__":
    build_all()
