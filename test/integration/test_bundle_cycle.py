"""Test bundle chain cycle detection."""

from __future__ import annotations

import json
import pytest
from hcom.api import bundle


@pytest.fixture
def isolated_hcom_env(monkeypatch, tmp_path):
    """Isolated HCOM environment."""
    hcom_dir = tmp_path / ".hcom"
    hcom_dir.mkdir()

    monkeypatch.setenv("HCOM_DIR", str(hcom_dir))
    monkeypatch.setenv("HOME", str(tmp_path))

    from hcom.core.config import reload_config
    reload_config()

    from hcom.core.db import init_db
    init_db()

    yield hcom_dir


def test_bundle_chain_with_cycle(isolated_hcom_env):
    """Bundle chain should handle circular references without infinite loop."""
    from hcom.core.db import get_db
    from datetime import datetime, timezone

    # Create two bundles with circular extends reference directly in database
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()

    # Bundle A
    bundle_a_data = {
        "bundle_id": "bundle:aaaa0001",
        "title": "Bundle A",
        "description": "First bundle",
        "refs": {
            "events": ["1"],
            "files": ["a.py"],
            "transcript": ["1-2"]
        },
        "extends": "bundle:bbbb0001"  # Points to B
    }

    db.execute(
        "INSERT INTO events (timestamp, type, instance, data) VALUES (?, ?, ?, ?)",
        [now, "bundle", "tester", json.dumps(bundle_a_data)]
    )

    # Bundle B
    bundle_b_data = {
        "bundle_id": "bundle:bbbb0001",
        "title": "Bundle B",
        "description": "Second bundle",
        "refs": {
            "events": ["2"],
            "files": ["b.py"],
            "transcript": ["3-4"]
        },
        "extends": "bundle:aaaa0001"  # Points to A (creates cycle)
    }

    db.execute(
        "INSERT INTO events (timestamp, type, instance, data) VALUES (?, ?, ?, ?)",
        [now, "bundle", "tester", json.dumps(bundle_b_data)]
    )

    db.commit()

    # Attempt to chain from A - should not infinite loop
    # With the bug, this would hang forever
    # With the fix, it should detect the cycle and stop
    chain = bundle(action="chain", bundle_id="bundle:aaaa0001")

    # Should get at least A and B, but not infinite
    assert len(chain) >= 2
    assert len(chain) <= 2  # Should stop at cycle

    # Verify we got both bundles
    bundle_ids = [b["bundle_id"] for b in chain]
    assert "bundle:aaaa0001" in bundle_ids
    assert "bundle:bbbb0001" in bundle_ids


def test_bundle_chain_self_reference(isolated_hcom_env):
    """Bundle chain should handle self-reference without infinite loop."""
    from hcom.core.db import get_db
    from datetime import datetime, timezone

    db = get_db()
    now = datetime.now(timezone.utc).isoformat()

    # Bundle that extends itself
    bundle_data = {
        "bundle_id": "bundle:cccc0001",
        "title": "Self Bundle",
        "description": "Bundle that extends itself",
        "refs": {
            "events": ["1"],
            "files": ["c.py"],
            "transcript": ["1-2"]
        },
        "extends": "bundle:cccc0001"  # Points to itself
    }

    db.execute(
        "INSERT INTO events (timestamp, type, instance, data) VALUES (?, ?, ?, ?)",
        [now, "bundle", "tester", json.dumps(bundle_data)]
    )

    db.commit()

    # Should not infinite loop
    chain = bundle(action="chain", bundle_id="bundle:cccc0001")

    # Should get the bundle exactly once
    assert len(chain) == 1
    assert chain[0]["bundle_id"] == "bundle:cccc0001"