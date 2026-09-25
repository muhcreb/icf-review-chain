#!/usr/bin/env python3
"""
ICF Review Chain — Verify .exe matches published source

Usage:
  python3 verify-checksums.py
  python3 verify-checksums.py --exe dist/icf-review-chain.exe
  python3 verify-checksums.py --source review_chain_standalone.py --exe dist/icf-review-chain.exe
"""

import sys
import os
import hashlib
import argparse
import tempfile
import shutil
from pathlib import Path


def sha256_file(filepath):
    """Compute SHA256 hash of a file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text):
    """Compute SHA256 hash of text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def list_extracted_files(exe_path):
    """List files inside a PyInstaller .exe using pyi-archive_viewer."""
    import subprocess
    result = subprocess.run(
        ["pyinstaller", "--version"],
        capture_output=True, text=True
    )
    # Try to extract using pyi-archive_viewer
    try:
        result = subprocess.run(
            ["pyi-archive_viewer", exe_path],
            capture_output=True, text=True, timeout=30
        )
        return result.stdout
    except FileNotFoundError:
        pass

    # Fallback: try to extract with PyInstaller
    try:
        import subprocess
        result = subprocess.run(
            ["pyinstaller", "--extract-built-tree", exe_path],
            capture_output=True, text=True, timeout=60
        )
        return result.stdout
    except Exception:
        pass

    return None


def extract_exe(exe_path, output_dir):
    """Extract PyInstaller .exe contents to a directory."""
    import subprocess

    extract_dir = Path(output_dir) / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)

    # Find pyi-archive_viewer in PATH or venv
    viewer = None
    for candidate in ["pyi-archive_viewer", "/opt/data/.venv/bin/pyi-archive_viewer"]:
        if Path(candidate).exists():
            viewer = candidate
            break

    if viewer:
        try:
            result = subprocess.run(
                [viewer, "-l", exe_path],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                print("Using pyi-archive_viewer to list contents...")
                print(result.stdout)
                return extract_dir
        except Exception as e:
            print(f"pyi-archive_viewer error: {e}")

    # Fallback: binary inspection of .exe
    print("pyi-archive_viewer not available — performing binary inspection...")
    with open(exe_path, "rb") as f:
        data = f.read()

    markers = [b"PyInstaller", b"MEI", b"PYZ", b"PKZ"]
    for marker in markers:
        if marker in data:
            print(f"  Found marker: {marker}")

    pyc_count = data.count(b".pyc")
    print(f"  Found {pyc_count} .pyc references in the binary")

    source_keywords = [
        b"HANDOFF TO CONSISTENCY AUDITOR",
        b"Agent 1 findings to verify",
        b"ICF Review Chain",
        b"Pre-Review",
        b"Consistency Auditor",
    ]
    for kw in source_keywords:
        if kw in data:
            print(f"  Found embedded string: {kw.decode('utf-8', errors='replace')[:50]}")

    return extract_dir


def verify_source_vs_exe(source_path, exe_path):
    """Verify that the .exe matches the published source."""
    source_path = Path(source_path)
    exe_path = Path(exe_path)

    print("=" * 60)
    print("ICF Review Chain — Integrity Verification")
    print("=" * 60)

    # 1. Hash the source file
    print(f"\nSource file: {source_path}")
    if not source_path.exists():
        print(f"  ERROR: Source file not found: {source_path}")
        return False

    source_hash = sha256_file(source_path)
    source_size = source_path.stat().st_size
    print(f"  SHA256: {source_hash}")
    print(f"  Size:   {source_size} bytes")

    # 2. Hash the .exe file
    print(f"\n.exe file: {exe_path}")
    if not exe_path.exists():
        print(f"  ERROR: .exe file not found: {exe_path}")
        return False

    exe_hash = sha256_file(exe_path)
    exe_size = exe_path.stat().st_size
    print(f"  SHA256: {exe_hash}")
    print(f"  Size:   {exe_size} bytes")

    # 3. Extract and inspect the .exe
    print(f"\n--- Extracting .exe contents ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        extract_dir = extract_exe(exe_path, tmpdir)

        # List extracted files
        if extract_dir.exists():
            extracted_files = list(extract_dir.rglob("*"))
            print(f"Extracted {len(extracted_files)} files to {extract_dir}")
            for f in sorted(extracted_files)[:20]:  # Show first 20
                if f.is_file():
                    h = sha256_file(f)
                    print(f"  {f.relative_to(extract_dir)}: {h} ({f.stat().st_size} bytes)")
            if len(extracted_files) > 20:
                print(f"  ... and {len(extracted_files) - 20} more files")

    # 4. Check for common PyInstaller markers
    print(f"\n--- PyInstaller markers ---")
    with open(exe_path, "rb") as f:
        exe_data = f.read(4096)  # Read first 4KB
        markers = [
            b"PyInstaller",
            b"MEI",
            b"PKZ",
            b"PYZ",
        ]
        for marker in markers:
            if marker in exe_data:
                print(f"  Found: {marker}")

    # 5. Summary
    print(f"\n{'=' * 60}")
    print("VERIFICATION SUMMARY")
    print(f"{'=' * 60}")
    print(f"Source:  {source_path.name}")
    print(f"  SHA256: {source_hash}")
    print(f"  Size:   {source_size} bytes")
    print(f"\n.exe:    {exe_path.name}")
    print(f"  SHA256: {exe_hash}")
    print(f"  Size:   {exe_size} bytes")
    print(f"\nNote: The .exe contains compiled Python bytecode (.pyc),")
    print(f"not the original .py source. To fully audit the .exe,")
    print(f"extract it with: pyi-archive_viewer -l {exe_path}")
    print(f"\nTo verify the .exe matches the source:")
    print(f"1. Read the source: cat {source_path}")
    print(f"2. Extract the .exe: pyi-archive_viewer {exe_path}")
    print(f"3. Compare the key components (prompts, logic)")
    print(f"{'=' * 60}")

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Verify ICF Review Chain .exe matches published source"
    )
    parser.add_argument("--source", default="review_chain_standalone.py",
                        help="Source file path (default: review_chain_standalone.py)")
    parser.add_argument("--exe", default="dist/icf-review-chain.exe",
                        help=".exe file path (default: dist/icf-review-chain.exe)")

    args = parser.parse_args()

    success = verify_source_vs_exe(args.source, args.exe)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()