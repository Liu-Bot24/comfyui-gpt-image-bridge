from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT / "reference" / "baseline-sha256.local.json"


@dataclass(frozen=True)
class CheckResult:
    label: str
    expected: str
    actual: str

    @property
    def unchanged(self) -> bool:
        return self.expected == self.actual


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def verify_manifest(manifest_path: Path) -> list[CheckResult]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    workflow = payload["original_workflow"]
    results = [
        CheckResult(
            label="normal_workflow",
            expected=str(workflow["sha256"]).upper(),
            actual=sha256(Path(workflow["path"])),
        )
    ]
    old_root = Path(payload["old_node_root"])
    for relative, expected in payload["old_node_files"].items():
        results.append(
            CheckResult(
                label=f"old_node:{relative}",
                expected=str(expected).upper(),
                actual=sha256(old_root / Path(relative)),
            )
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", nargs="?", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    if not args.manifest.is_file():
        print("READONLY_BASELINE_MISSING create reference/baseline-sha256.local.json")
        return 2
    try:
        results = verify_manifest(args.manifest)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        print("READONLY_BASELINE_INVALID manifest could not be checked")
        return 2
    for result in results:
        print(
            f"READONLY_TARGET label={result.label} "
            f"unchanged={str(result.unchanged).lower()}"
        )
    failures = [result.label for result in results if not result.unchanged]
    if failures:
        print("READONLY_BASELINE_FAILED labels=" + ",".join(failures))
        return 1
    print(f"READONLY_BASELINE_OK targets={len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
