import os, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import make_fixtures
os.chdir(tempfile.mkdtemp(prefix="icf-chain-tests-"))   # fixtures and outputs live here
make_fixtures.build_all()
import os, sys, json, shutil, io, contextlib, re
import review_chain_standalone as rc
import fake_llm_server as fake

server = fake.start()
URL = f"http://127.0.0.1:{server.server_port}/v1"
rc.CONFIG_FILE = rc.Path("/nonexistent/config.json")
fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond: fails.append(name)

def run(args, env, **state):
    for k in list(os.environ):
        if k.startswith("LLM_"): del os.environ[k]
    os.environ.update({"LLM_API_URL": URL, "LLM_API_KEY": "test"}); os.environ.update(env)
    fake.STATE.update({"calls": 0, "rate_limit_first": False, "truncate_agent2_part": None,
                       "auth_fail": False, "fail_agent2": False, "seen_system_prompts": [], "seen_parts": []}); fake.STATE.update(state)
    shutil.rmtree("out", ignore_errors=True)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = rc.main(args)
    return code, out.getvalue(), err.getvalue()

def latest_report():
    d = sorted(rc.Path("out").glob("review-*"))[-1]
    return d, (d / "unified_report.md").read_text(), json.loads((d / "run_metadata.json").read_text())

# ---------- 1. Normal run, protocol forced into several parts ----------
code, out, err = run(["icf.pdf", "protocol.pdf", "out"], {"LLM_MAX_INPUT_CHARS": "32000", "LLM_RETRY": "x"})
d, report, meta = latest_report()
parts = fake.STATE["seen_parts"]
check("1. runs to the end (no NameError)", code in (0, 2), f"exit {code}; {err.strip()[:200]}")
check("1. protocol split into several parts, all sent", len(parts) >= 3, f"{len(parts)} parts: {parts[0][:60]}")
check("1. every part names its page range", all("pp." in p or "p." in p for p in parts))
check("1. all 30 protocol pages covered across parts",
      sorted({int(n) for p in parts for n in re.findall(r"pp?\.(\d+)", p)})[0] == 1 and
      any("30" in p for p in parts), "; ".join(p.split(",")[1].split(" of")[0].strip() for p in parts))
check("1. reports 'reviewed in N of N parts'", f"{len(parts)} of {len(parts)} part(s)" in report)
check("1. fabricated protocol quote flagged NOT FOUND", "Protocol: NOT FOUND" in report)
check("1. wrong cited page flagged", "PAGE MISMATCH" in report)
check("1. genuine quote verified", "Protocol: VERIFIED; ICF: VERIFIED" in report)
check("1. malformed row surfaced, not dropped", "UNPARSED TABLE LINES" in report and "Broken row" in report)
check("1. duplicate 'Shared item' merged to one row", report.count("| Shared item |") == 1, f"{report.count('| Shared item |')}")
check("1. invented-payment HIGH row listed as CRITICAL ALERT", "Invented payment" in report.split("CRITICAL ALERTS:")[1].split("HUMAN REVIEW")[0])
table = report.split("### COMPARISON TABLE")[1].split("### UNPARSED")[0]
sampling = [l for l in table.splitlines() if l.startswith("| Sampling schedule part 1")][0]
check("1. per-row FK-GL filled in by code (model wrote n/a)", re.search(r"\| -?\d+\.\d \|", sampling) is not None, sampling[-90:])
check("1. FK-GL calculated in code appears in header", re.search(r"Flesch-Kincaid grade level \(calculated in code\): -?\d+\.\d", report) is not None)
check("1. Agent 1 invented quote flagged", "not verified in the source documents" in report and "destroy every record" in report)
check("1. Agent 1 genuine quote not flagged", "free to ask questions about part 1.2" not in report.split("not verified in the source documents")[1] if "not verified in the source documents" in report else False)
check("1. critical alert built on an unverifiable quote is labelled as such", "Invented payment 1 (INCONSISTENT; TCPS 2 Art. 4.1) [QUOTE NOT VERIFIED]" in report)
check("1. input hashes recorded", len(meta["inputs"]["ICF"]["sha256"]) == 64)
check("1. a malformed row is a WARNING, not a coverage gap", meta["review_status"] == "COMPLETE" and code == 0 and any("nine-column" in w for w in meta["warnings"]), f"{meta['review_status']} problems={meta['problems']} warnings={meta['warnings']}")

# ---------- 2. Everything fits in one request ----------
code, out, err = run(["icf.pdf", "protocol.pdf", "out"], {})
d, report, meta = latest_report()
check("2. large budget: protocol sent whole, single part", len(fake.STATE["seen_parts"]) == 1 and "complete Protocol" in fake.STATE["seen_parts"][0], fake.STATE["seen_parts"])
check("2. Agent 1 saw the protocol", "Agent 1 saw the Protocol: yes" in report)

# ---------- 3. Output cut off at token limit -> INCOMPLETE, exit 2 ----------
code, out, err = run(["icf.pdf", "protocol.pdf", "out"], {"LLM_MAX_INPUT_CHARS": "32000"}, truncate_agent2_part=2)
d, report, meta = latest_report()
check("3. truncated output marks review INCOMPLETE", meta["review_status"] == "INCOMPLETE" and "INCOMPLETE" in report.split("\n## Coverage")[0])
check("3. exit status 2", code == 2, f"exit {code}")
check("3. problem names the part", any("part 2" in p and "cut off" in p for p in meta["problems"]), str(meta["problems"])[:200])

# ---------- 4. ICF too big for one request -> loud stop ----------
open("icf_huge.txt", "w").write("You are free to ask questions about this study at any time. " * 1500)   # ~87k chars
code, out, err = run(["icf_huge.txt", "protocol.pdf", "out"], {"LLM_MAX_INPUT_CHARS": "60000"})
check("4. ICF that cannot fit stops with an explanation", code == 1 and "does not fit in one request" in err, err.strip()[:140])
check("4. nothing was sent for a partial ICF", fake.STATE["calls"] == 0, f"{fake.STATE['calls']} calls")
open("icf_big.txt", "w").write("You are free to ask questions about this study at any time. " * 700)    # ~41k chars
code, out, err = run(["icf_big.txt", "protocol.pdf", "out"], {"LLM_MAX_INPUT_CHARS": "55000"})
check("4. ICF that fits but leaves no room for the protocol stops too", code == 1 and "leaves only" in err, err.strip()[:140])
check("4. ... and sends nothing", fake.STATE["calls"] == 0, f"{fake.STATE['calls']} calls")

# ---------- 5. Scanned / blank ICF page ----------
code, out, err = run(["icf_with_blank_page.pdf", "protocol.pdf", "out"], {})
d, report, meta = latest_report()
check("5. blank ICF page makes the review INCOMPLETE", meta["review_status"] == "INCOMPLETE" and any("no extractable text" in p for p in meta["problems"]), str(meta["problems"])[:150])

# ---------- 6. API behaviour ----------
code, out, err = run(["icf.pdf", "protocol.pdf", "out"], {}, rate_limit_first=True)
check("6. 429 is retried and the run succeeds", code in (0, 2) and fake.STATE["calls"] >= 3, f"exit {code}, {fake.STATE['calls']} calls")
code, out, err = run(["icf.pdf", "protocol.pdf", "out"], {}, auth_fail=True)
check("6. 401 stops immediately with a clear message", code == 1 and "HTTP 401" in err and fake.STATE["calls"] == 1, err.strip()[:120])

# ---------- 7. Missing key, non-TTY ----------
for k in list(os.environ):
    if k.startswith("LLM_"): del os.environ[k]
out, err = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
    code = rc.main(["icf.pdf", "protocol.pdf", "out"])
check("7. no API key gives a clear error, not a crash", code == 1 and "LLM_API_KEY" in err.getvalue())

# ---------- 8. stdin ICF ----------
os.environ.update({"LLM_API_URL": URL, "LLM_API_KEY": "test"})
fake.STATE.update({"auth_fail": False, "calls": 0})
for argv in (["--stdin-icf", "protocol.pdf", "out"], ["--stdin-icf", "-", "protocol.pdf", "out"], ["--stdin-icf", "protocol.pdf"]):
    sys.stdin = io.StringIO("You are free to ask questions about part 1.1 of this study at any time. " * 30)
    shutil.rmtree("out", ignore_errors=True); shutil.rmtree("deliberations", ignore_errors=True)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = rc.main(argv)
    where = "deliberations" if len(argv) == 2 else "out"
    check(f"8. {' '.join(argv)}", code in (0, 2) and rc.Path(where).exists(), buf.getvalue()[-120:].replace(chr(10), " "))
shutil.rmtree("deliberations", ignore_errors=True)


# ---------- 9. Failure after Agent 1: partial work is kept and located ----------
code, out, err = run(["icf.pdf", "protocol.pdf", "out"], {}, fail_agent2=True)
dirs = sorted(rc.Path("out").glob("review-*"))
check("9. a fatal Agent 2 error stops the run (exit 1)", code == 1 and "HTTP 400" in err, err.strip()[:100])
check("9. error says where the saved work is", "is saved in" in err and "agent1_output.md" in err, err.strip()[-160:])
check("9. Agent 1 output survives on disk", dirs and (dirs[-1] / "agent1_output.md").read_text().strip() != "")
check("9. status file records FAILED", dirs and json.loads((dirs[-1] / "run_metadata.json").read_text())["review_status"] == "FAILED")

# ---------- 10. Mismatched study pair: warned in the report, stopped if the person says no ----------
code, out, err = run(["icf.pdf", "protocol.pdf", "out"], {})
d, report, meta = latest_report()
check("10. input-check notes appear in the coverage section", "Identifiers found" in report)
check("10. input warnings recorded in metadata", isinstance(meta["warnings"], list))

import builtins
class Tty:
    def isatty(self): return True
    def read(self): return ""
real_stdin, real_input = sys.stdin, builtins.input
mismatch_text = "Participants in this arthroplasty rehabilitation physiotherapy programme receive osteoarthritis surgery follow-up. " * 40
open("other_protocol.txt", "w").write("Protocol XYZ-999 " + mismatch_text)
open("icf_abc.txt", "w").write("Study ABC-123 vemurafenib cardiotoxicity melanoma patients consent. " * 40)
sys.stdin = Tty()
builtins.input = lambda prompt="": "n"
code, out, err = run(["icf_abc.txt", "other_protocol.txt", "out"], {})
sys.stdin, builtins.input = real_stdin, real_input
check("10. mismatched pair: person declines, nothing is sent", code == 1 and fake.STATE["calls"] == 0 and "Stopped before sending" in err,
      f"exit {code}, calls {fake.STATE['calls']}")
check("10. the warning was shown before asking", "share no study identifier" in out, out[-200:].replace(chr(10), " "))
code, out, err = run(["icf_abc.txt", "other_protocol.txt", "out", "--yes"], {})
check("10. --yes proceeds anyway and the warning is in the report", code in (0, 2) and "share no study identifier" in latest_report()[1])

print()
print("FAILED: " + ", ".join(fails) if fails else "ALL END-TO-END CHECKS PASSED")
sys.exit(1 if fails else 0)
