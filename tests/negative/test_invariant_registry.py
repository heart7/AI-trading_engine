"""One entry per §18 invariant. Uncovered invariants xfail (strict) so they stay visible as red."""
import pytest

from tests.conftest import REGISTRY


def test_registry_is_complete():
    assert [r["id"] for r in REGISTRY] == [f"INV-{i:02d}" for i in range(1, 44)]


@pytest.mark.parametrize("inv", REGISTRY, ids=[r["id"] for r in REGISTRY])
def test_invariant_registered(inv):
    # Collection marks this xfail when no test proves the invariant. If a proving test exists,
    # this passes and the proving test itself decides GREEN or RED in the summary.
    from tests.conftest import _coverage
    assert _coverage.get(inv["id"]), f"{inv['id']} not yet implemented ({inv['phase']})"
