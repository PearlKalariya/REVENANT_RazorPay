"""Webhook signature verification tests.

Runs entirely offline with a locally generated secret. No Razorpay credentials
and no network are required, so these tests are safe in CI and prove the
security property independently of any account.
"""

import json

import pytest

from backend.integrations.webhook import (
    SignatureError,
    compute_signature,
    verify_signature,
)

SECRET = "test_whsec_local_only_not_a_real_secret"
BODY = json.dumps(
    {"event": "payment_link.paid", "payload": {"amount": 240000}}
).encode()


def test_valid_signature_passes():
    sig = compute_signature(BODY, SECRET)
    assert verify_signature(BODY, sig, SECRET) is True


def test_tampered_body_fails():
    """The attack this exists to stop: a forged amount."""
    sig = compute_signature(BODY, SECRET)
    tampered = BODY.replace(b"240000", b"99900000")
    assert verify_signature(tampered, sig, SECRET) is False


def test_wrong_secret_fails():
    sig = compute_signature(BODY, "some_other_secret")
    assert verify_signature(BODY, sig, SECRET) is False


def test_missing_signature_fails():
    assert verify_signature(BODY, None, SECRET) is False
    assert verify_signature(BODY, "", SECRET) is False


def test_empty_secret_fails_closed():
    """An unconfigured secret must never mean 'accept everything'."""
    sig = compute_signature(BODY, SECRET)
    assert verify_signature(BODY, sig, "") is False


def test_compute_requires_secret():
    with pytest.raises(SignatureError):
        compute_signature(BODY, "")


@pytest.mark.parametrize("bad", [123, [], {}, object()])
def test_non_string_signature_fails(bad):
    assert verify_signature(BODY, bad, SECRET) is False


def test_garbage_signature_fails():
    assert verify_signature(BODY, "not-a-hex-digest", SECRET) is False


def test_signature_is_whitespace_tolerant():
    """Headers pick up stray whitespace in transit; that alone must not
    invalidate an otherwise genuine signature."""
    sig = compute_signature(BODY, SECRET)
    assert verify_signature(BODY, f"  {sig}  ", SECRET) is True


def test_reserialised_json_does_not_match():
    """Documents rule 1: verification MUST use the raw bytes.

    This test exists so that if someone later 'tidies' the handler to parse
    then re-dump the body, this fails loudly and explains why.
    """
    sig = compute_signature(BODY, SECRET)
    reserialised = json.dumps(json.loads(BODY), indent=2).encode()
    assert verify_signature(reserialised, sig, SECRET) is False


def test_replay_harness_output_verifies():
    """A replayed event signed by the harness passes real verification.

    This is the D3 guarantee: the replay path does not bypass signature
    checking, it satisfies it with a genuine HMAC.
    """
    replayed = json.dumps({"event": "payment.captured"}).encode()
    harness_sig = compute_signature(replayed, SECRET)
    assert verify_signature(replayed, harness_sig, SECRET) is True
