"""Webhook behaviour that the ledger's correctness rests on."""

import pytest


@pytest.mark.anyio
async def test_replayed_callback_does_not_double_credit() -> None:
    """The same external_ref twice credits once and still returns 200."""
    pytest.skip("Implement once the payments service lands.")


@pytest.mark.anyio
async def test_forged_signature_is_rejected() -> None:
    pytest.skip("Implement once the payments service lands.")


@pytest.mark.anyio
async def test_callback_with_no_matching_invoice_goes_to_suspense() -> None:
    pytest.skip("Implement once the payments service lands.")
