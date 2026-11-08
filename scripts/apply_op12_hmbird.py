#!/usr/bin/env python3
"""Apply the pinned OP12 HMBIRD backport without changing the source version."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path


def source_version(directory):
    text = (directory / "Makefile").read_text()
    values = []
    for key in ("VERSION", "PATCHLEVEL", "SUBLEVEL"):
        match = re.search(rf"^{key}\s*=\s*(\d+)\s*$", text, re.M)
        if match is None:
            raise ValueError(f"Cannot read {key} from the kernel Makefile")
        values.append(match.group(1))
    return ".".join(values)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    directory = args.source.resolve()
    root = Path(__file__).resolve().parents[1]
    lock_path = root / "patches/op12/hmbird-source-lock.json"
    lock = json.loads(lock_path.read_text())
    patch = root / "patches/op12/6.1.118-hmbird-compat.patch"
    digest = hashlib.sha256(patch.read_bytes()).hexdigest()
    if digest != lock["patch_sha256"]:
        raise ValueError("HMBIRD patch SHA256 does not match its reviewed source lock")
    reference = root / "scripts/compat/op12-oos16.0.0.201.json"
    if hashlib.sha256(reference.read_bytes()).hexdigest() != lock["reference_sha256"]:
        raise ValueError("The stock compatibility reference has changed")
    head = subprocess.check_output(
        ["git", "-C", str(directory), "rev-parse", "HEAD"], text=True
    ).strip()
    if head != lock["target_commit"]:
        raise ValueError(f"Wrong common source revision: {head}")
    before = source_version(directory)
    if before != "6.1.118":
        raise ValueError(f"The source must really be 6.1.118, found {before}")
    makefile_hash = hashlib.sha256((directory / "Makefile").read_bytes()).hexdigest()
    subprocess.run(
        ["git", "apply", "--check", "--whitespace=nowarn", str(patch)],
        cwd=directory,
        check=True,
    )
    if not args.check_only:
        subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(patch)],
            cwd=directory,
            check=True,
        )
        if (
            hashlib.sha256((directory / "Makefile").read_bytes()).hexdigest()
            != makefile_hash
        ):
            raise ValueError("The backport must not edit the kernel Makefile")
        for relative, expected in lock["result_files"].items():
            actual = hashlib.sha256((directory / relative).read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError(f"Unexpected backport result: {relative}")
        config = (
            (directory / "arch/arm64/configs/gki_defconfig").read_text().splitlines()
        )
        if "CONFIG_HMBIRD_SCHED=y" not in config:
            raise ValueError("The HMBIRD scheduler was not enabled")
        if any(
            f"CONFIG_{key}=y" in config for key in ("SLIM_SCHED", "SCHED_CLASS_EXT")
        ):
            raise ValueError("The incompatible SLIM scheduler remains enabled")
    report = {
        "target_commit": head,
        "source_version": before,
        "patch_sha256": digest,
        "stock_reference_sha256": lock["reference_sha256"],
        "upstream": lock["upstream"],
        "mode": "check-only" if args.check_only else "applied",
        "scope": "Source checks only; final Image ABI checks are mandatory before upload.",
    }
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    args.audit.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
