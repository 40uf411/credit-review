from pathlib import Path

import pytest

from credit_review.golden import GoldenProvider, load_golden
from credit_review.llm import LLMClient, LLMConfig
from credit_review.pdf import list_dossier
from credit_review.policy import load_policy

ROOT = Path(__file__).resolve().parents[1]
DOSSIER = ROOT / "case_pack" / "dossier"
GOLDEN = ROOT / "golden" / "sample_dossier.yaml"


@pytest.fixture(scope="session")
def policy():
    report = load_policy(ROOT / "policy" / "example_bank_v3.2.yaml")
    assert report.ok, report.errors
    return report.policy


@pytest.fixture(scope="session")
def docs():
    if not DOSSIER.exists():
        pytest.skip("case pack not unpacked in case_pack/dossier")
    return list_dossier(DOSSIER)


@pytest.fixture(scope="session")
def doc(docs):
    by_name = {d.name: d for d in docs}
    return lambda name: by_name[name]


@pytest.fixture(scope="session")
def golden():
    return load_golden(GOLDEN)


@pytest.fixture
def golden_llm(golden, tmp_path):
    config = LLMConfig(provider="anthropic", model="golden-replay", cache_dir=tmp_path / "cache")
    return LLMClient(config, provider=GoldenProvider(golden))
