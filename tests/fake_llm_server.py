"""A stand-in for an OpenAI-compatible endpoint, for testing the pipeline."""
import json, re, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

STATE = {"fail_agent2": False, "calls": 0, "rate_limit_first": False, "truncate_agent2_part": None, "auth_fail": False,
         "seen_system_prompts": [], "seen_parts": []}

def first_real_sentence(text, skip=0):
    """A verbatim multi-line quote from the supplied text, plus its page."""
    page = 1; found = 0
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"\[\[PAGE (\d+)\]\]", line.strip())
        if m: page = int(m.group(1)); continue
        if len(line.split()) >= 8 and found == skip:
            return " ".join(l.strip() for l in lines[i:i+2]), page
        if len(line.split()) >= 8: found += 1
    return "no text", page

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        STATE["calls"] += 1
        if STATE["auth_fail"]:
            self._send(401, {"error": "bad key"}); return
        if STATE["rate_limit_first"] and STATE["calls"] == 1:
            self._send(429, {"error": "slow down"}); return
        system = body["messages"][0]["content"]; user = body["messages"][1]["content"]
        if STATE["fail_agent2"] and system.startswith("# REB Informed"):
            self._send(400, {"error": "context length exceeded"}); return
        STATE["seen_system_prompts"].append(system[:30])
        finish = "stop"
        if system.startswith("# Consent Form Pre-Review"):
            icf_quote, _ = first_real_sentence(user.split("ICF:\n\n")[1], skip=1)
            content = (
                "### OVERALL IMPRESSION\n\nReviewed the provided ICF.\n\n"
                "### KEY ISSUES & RECOMMENDATIONS\n\n| Area | ICF location | Issue | Basis | Recommendation | Priority |\n| :--- | :--- | :--- | :--- | :--- | :--- |\n"
                f'| Voluntariness | p.1 | Wording "{icf_quote}" is fine | TCPS 2 | none | LOW |\n'
                '| Withdrawal | p.2 | The form says "You may withdraw at any time and we will immediately destroy every record we hold about you" | TCPS 2 | add limits | HIGH |\n\n'
                "### HANDOFF TO CONSISTENCY AUDITOR (machine-readable, do not rewrite)\n\n"
                "**Jurisdiction:** Quebec\n**Population:** adults\n\n**Agent 1 findings to verify (copy exact):**\n\n1. p.2 - withdrawal wording - TCPS 2 - HIGH\n")
        else:
            m = re.search(r"PROTOCOL \((.*?)\):\n\n(.*?)\n\nICF \(complete", user, re.S)
            scope, ptext = m.group(1), m.group(2)
            part = int(re.search(r"part (\d+) of", scope).group(1)) if "part" in scope else 1
            STATE["seen_parts"].append(scope)
            pq, ppage = first_real_sentence(ptext, skip=0)
            iq = "You are free to ask questions about part 1.1 of this study at any time. Your participation is voluntary and you may stop whenever you wish without any effect on your care."
            rows = [
                f'| Sampling schedule part {part} | CONSISTENT | MEDIUM | "{pq}" (p.{ppage}) | "{iq}" (p.1) | HIGH | n/a | N/A | Matches |',
                f'| Invented payment {part} | INCONSISTENT | HIGH | "Participants will be paid 500 dollars per visit in cash" (p.{ppage}) | "{iq}" (p.1) | HIGH | n/a | TCPS 2 Art. 4.1 | Fabricated protocol quote |',
                f'| Wrong page {part} | CONSISTENT | LOW | "{pq}" (p.{ppage + 7}) | ABSENT | MEDIUM | n/a | N/A | cited page is wrong |',
            ]
            if part == 1:
                rows.append("| Broken row | only | three cells |")
                rows.append('| Shared item | VAGUE | MEDIUM | "' + pq + f'" (p.{ppage}) | "' + iq + '" (p.1) | MEDIUM | n/a | N/A | appears in several parts |')
            else:
                rows.append('| Shared item | VAGUE | MEDIUM | "' + pq + f'" (p.{ppage}) | "' + iq + '" (p.1) | MEDIUM | n/a | N/A | appears in several parts |')
            if "Randomised placebo" not in ptext and "placebo arm" in ptext:
                rows.append('| Placebo blinding | ABSENT | HIGH | "Participants randomised to the placebo arm will receive an inactive tablet and will not be told which arm they have been assigned to" (p.17) | ABSENT | HIGH | n/a | TCPS 2 Art. 3.2 | Protocol blinding not disclosed - HUMAN REVIEW REQUIRED |')
            content = "### COMPARISON TABLE\n\n| Comparison Item | Status | Criticality | Protocol Text (Source) | ICF Text (Match) | Confidence | Readability Score (FK-GL) | Applicable Provision | Notes |\n|---|---|---|---|---|---|---|---|---|\n" + "\n".join(rows)
            if STATE["truncate_agent2_part"] == part:
                content = content[: len(content)//2]; finish = "length"
        self._send(200, {"choices": [{"message": {"content": content}, "finish_reason": finish}]})
    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

def start():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
