#!/usr/bin/env python3
"""
ICF Review Chain — Agent 1 (Pre-Review) → Agent 2 (Consistency Auditor)
Orchestration script for the Hermes platform.

Usage:
  python3 review_chain.py <icf_file> <protocol_file> [output_dir]

Example:
  python3 review_chain.py /opt/data/reb-sim/protocols/2026-041_icf.md /opt/data/reb-sim/protocols/2026-041_protocol.md

The script runs Agent 1 → parses handoff → Agent 2 → unified report,
then waits for all steps to complete before returning.
"""

import sys
import os
import re
import time
from datetime import datetime
from hermes_tools import read_file, write_file, delegate_task

# Paths
AGENT1_PROMPT = "/opt/data/reb-sim/templates/agent1_prereview_prompt.md"
AGENT2_PROMPT = "/opt/data/reb-sim/templates/agent2_consistency_auditor_prompt.md"
DEFAULT_OUTPUT_DIR = "/opt/data/reb-sim/deliberations"


def read_prompt(path):
    """Read an agent prompt from file."""
    result = read_file(path)
    if result.get("content"):
        return result["content"]
    return None


def parse_handoff_block(text):
    """Parse the HANDOFF TO CONSISTENCY AUDITOR block from Agent 1's output."""
    match = re.search(
        r'### HANDOFF TO CONSISTENCY AUDITOR.*?(?=\n## |\n# |$)',
        text, re.DOTALL
    )
    if match:
        return match.group(0)
    return None


def merge_reports(agent1_output, agent2_output, handoff_block):
    """Merge Agent 1 and Agent 2 outputs into a unified report."""
    report = f"""# ICF Review Report — Unified

## Agent 1: Pre-Review
{agent1_output}

## Agent 2: Consistency Auditor
{agent2_output}

## Handoff Block (Agent 1 → Agent 2)
{handoff_block or 'No handoff block found.'}
"""
    return report


def wait_for_file(path, min_size=1000, max_wait=120, poll_interval=2):
    """Wait for a file to appear with content."""
    for i in range(max_wait // poll_interval):
        if os.path.exists(path):
            size = os.path.getsize(path)
            if size >= min_size:
                return True
        time.sleep(poll_interval)
    return False


def run_review_chain(icf_file, protocol_file, output_dir=None):
    """Run the full Agent 1 → Agent 2 → unified report chain."""
    if output_dir is None:
        output_dir = DEFAULT_OUTPUT_DIR

    # Create output directory with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    chain_dir = os.path.join(output_dir, f"2026-041-chain-{timestamp}")
    os.makedirs(chain_dir, exist_ok=True)

    # Read prompts and documents
    agent1_prompt = read_prompt(AGENT1_PROMPT)
    agent2_prompt = read_prompt(AGENT2_PROMPT)
    icf_content = read_file(icf_file).get("content", "")
    protocol_content = read_file(protocol_file).get("content", "")

    if not agent1_prompt or not agent2_prompt:
        print("Error: Could not read agent prompts.")
        sys.exit(1)

    if not icf_content or not protocol_content:
        print("Error: Could not read ICF or Protocol files.")
        sys.exit(1)

    print(f"ICF: {icf_file} ({len(icf_content)} chars)")
    print(f"Protocol: {protocol_file} ({len(protocol_content)} chars)")
    print(f"Output dir: {chain_dir}")
    print(f"Agent 1 prompt: {len(agent1_prompt)} chars")
    print(f"Agent 2 prompt: {len(agent2_prompt)} chars)")
    print()

    # Step 1: Run Agent 1 (Pre-Review)
    print("=== Step 1: Agent 1 (Pre-Review) ===")
    agent1_goal = (
        f"Run Agent 1 (Pre-Review) on the ICF and Protocol documents. "
        f"Follow the Agent 1 prompt instructions exactly. "
        f"Produce the Pre-Review output including the HANDOFF TO CONSISTENCY AUDITOR block. "
        f"Save the output to {chain_dir}/agent1_output.md"
    )
    agent1_context = (
        f"Agent 1 prompt (read from {AGENT1_PROMPT}):\n\n"
        f"{agent1_prompt}\n\n"
        f"Documents to review:\n"
        f"- Protocol: {protocol_file}\n"
        f"- ICF: {icf_file}\n\n"
        f"IMPORTANT: Read the actual files at the paths above. Do NOT use cached or previous session data."
    )

    result1 = delegate_task(
        action="spawn",
        tasks=[{"context": agent1_context, "goal": agent1_goal}]
    )
    print(f"Agent 1 dispatched: {result1}")

    # Wait for Agent 1 to complete
    agent1_output_path = os.path.join(chain_dir, "agent1_output.md")
    if wait_for_file(agent1_output_path):
        size = os.path.getsize(agent1_output_path)
        print(f"Agent 1 completed ({size} bytes)")
    else:
        print("Warning: Agent 1 may not have completed yet")

    # Read Agent 1 output
    agent1_output = read_file(agent1_output_path).get("content", "")
    handoff_block = parse_handoff_block(agent1_output)
    print(f"Handoff block: {'found' if handoff_block else 'NOT FOUND'}")

    # Step 2: Run Agent 2 (Consistency Auditor)
    print("\n=== Step 2: Agent 2 (Consistency Auditor) ===")
    agent2_goal = (
        f"Run Agent 2 (Consistency Auditor) on the ICF and Protocol documents, "
        f"consuming the handoff block from Agent 1's output. "
        f"Follow the Agent 2 prompt instructions exactly. "
        f"Produce the Consistency Auditor output (EXECUTIVE SUMMARY, COMPARISON TABLE). "
        f"Save the output to {chain_dir}/agent2_output.md"
    )
    agent2_context = (
        f"Agent 2 prompt (read from {AGENT2_PROMPT}):\n\n"
        f"{agent2_prompt}\n\n"
        f"Agent 1 output (read from {agent1_output_path}):\n"
        f"{agent1_output[:3000]}\n\n"
        f"Documents to review:\n"
        f"- Protocol: {protocol_file}\n"
        f"- ICF: {icf_file}\n\n"
        f"IMPORTANT: Read the actual files at the paths above. "
        f"Consume the handoff block from Agent 1's output. "
        f"Do NOT use cached or previous session data."
    )

    result2 = delegate_task(
        action="spawn",
        tasks=[{"context": agent2_context, "goal": agent2_goal}]
    )
    print(f"Agent 2 dispatched: {result2}")

    # Wait for Agent 2 to complete
    agent2_output_path = os.path.join(chain_dir, "agent2_output.md")
    if wait_for_file(agent2_output_path):
        size = os.path.getsize(agent2_output_path)
        print(f"Agent 2 completed ({size} bytes)")
    else:
        print("Warning: Agent 2 may not have completed yet")

    # Read Agent 2 output
    agent2_output = read_file(agent2_output_path).get("content", "")

    # Step 3: Generate unified report
    print("\n=== Step 3: Unified Report ===")
    unified_report = merge_reports(agent1_output, agent2_output, handoff_block)
    unified_path = os.path.join(chain_dir, "unified_report.md")
    write_file(path=unified_path, content=unified_report)
    print(f"Unified report saved to: {unified_path}")

    # Summary
    print(f"\n=== Review Chain Complete ===")
    print(f"Output directory: {chain_dir}")
    for f in os.listdir(chain_dir):
        fpath = os.path.join(chain_dir, f)
        print(f"  {f} ({os.path.getsize(fpath)} bytes)")

    return chain_dir


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 review_chain.py <icf_file> <protocol_file> [output_dir]")
        print("\nExample:")
        print("  python3 review_chain.py /opt/data/reb-sim/protocols/2026-041_icf.md /opt/data/reb-sim/protocols/2026-041_protocol.md")
        sys.exit(1)

    icf_file = sys.argv[1]
    protocol_file = sys.argv[2]
    output_dir = sys.argv[3] if len(sys.argv) > 3 else None

    if not os.path.exists(icf_file):
        print(f"Error: ICF file not found: {icf_file}")
        sys.exit(1)
    if not os.path.exists(protocol_file):
        print(f"Error: Protocol file not found: {protocol_file}")
        sys.exit(1)

    chain_dir = run_review_chain(icf_file, protocol_file, output_dir)
    print(f"\nDone. Unified report: {chain_dir}/unified_report.md")


if __name__ == "__main__":
    main()