from __future__ import annotations

import hashlib
import json

from tools.verify_readonly_baseline import verify_manifest


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def test_local_readonly_manifest_detects_unchanged_and_changed_targets(tmp_path):
    workflow = tmp_path / "Normal-Final.json"
    workflow.write_text("workflow", encoding="utf-8")
    old_root = tmp_path / "old-node"
    old_root.mkdir()
    old_file = old_root / "node.py"
    old_file.write_text("old node", encoding="utf-8")
    manifest = tmp_path / "baseline.local.json"
    manifest.write_text(
        json.dumps(
            {
                "original_workflow": {
                    "path": str(workflow),
                    "sha256": digest(workflow),
                },
                "old_node_root": str(old_root),
                "old_node_files": {"node.py": digest(old_file)},
            }
        ),
        encoding="utf-8",
    )

    assert all(result.unchanged for result in verify_manifest(manifest))
    old_file.write_text("changed", encoding="utf-8")
    results = verify_manifest(manifest)
    assert results[0].unchanged is True
    assert results[1].unchanged is False
