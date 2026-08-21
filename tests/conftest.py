import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PACK = ROOT.parent

NOW = datetime(2026, 4, 26, 10, 30, tzinfo=timezone.utc)


def _load(name):
    return json.loads((PACK / "dataset" / name).read_text())


@pytest.fixture(scope="session")
def categories():
    return {p.stem: json.loads(p.read_text())
            for p in (PACK / "dataset" / "categories").glob("*.json")}


@pytest.fixture(scope="session")
def merchants():
    return {m["merchant_id"]: m for m in _load("merchants_seed.json")["merchants"]}


@pytest.fixture(scope="session")
def customers():
    return {c["customer_id"]: c for c in _load("customers_seed.json")["customers"]}


@pytest.fixture(scope="session")
def triggers():
    return {t["id"]: t for t in _load("triggers_seed.json")["triggers"]}


@pytest.fixture(scope="session")
def expanded():
    """The generated half of the dataset — sparse merchants and placeholder triggers."""
    base = PACK / "expanded"
    if not base.exists():
        pytest.skip("run dataset/generate_dataset.py first")
    out = {}
    for kind, key in (("merchants", "merchant_id"), ("customers", "customer_id"),
                      ("triggers", "id")):
        out[kind] = {json.loads(p.read_text())[key]: json.loads(p.read_text())
                     for p in (base / kind).glob("*.json")}
    out["pairs"] = json.loads((base / "test_pairs.json").read_text())["pairs"]
    return out


@pytest.fixture(scope="session")
def now():
    return NOW
