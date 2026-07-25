"""The envelope, and the redactor that catches whatever escapes it.

Two layers, tested together because they fail together. :class:`SecretBox` is the only
thing that ever holds plaintext; :func:`redact_secrets` is what stops the plaintext that
inevitably ends up somewhere it should not from reaching a log aggregator a dozen people
can read.

The redactor tests are deliberately paranoid about the *value*-shaped rule rather than the
key-shaped one. A field called ``password`` is caught by anybody's redactor. The way
secrets actually escape is ``log.info("provisioned", **provider_response)`` -- a dict whose
keys nobody chose and nobody reviewed -- and only the value rule catches that.
"""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from bailment.logging import (
    MAX_DEPTH,
    REDACTED,
    SAFE_KEY_NAMES,
    is_secret_key,
    redact_secrets,
    scrub_text,
)
from bailment.secrets import (
    REFERENCE_PREFIX,
    DecryptionError,
    InvalidKeyError,
    SecretBox,
    SecretsError,
    generate_key,
    make_reference,
    parse_reference,
)

PAYLOAD = {
    "DATABASE_URL": "postgresql://user:hunter2@db.example.com:5432/orders",
    "API_TOKEN": "tok_live_9f8e7d6c5b4a",
}


def redact(**values: object) -> dict[str, object]:
    """Run one event dict through the structlog processor, as structlog would."""
    return dict(redact_secrets(None, "info", dict(values)))


# --------------------------------------------------------------------------------------
# The envelope
# --------------------------------------------------------------------------------------


def test_a_payload_round_trips(encryption_key: str) -> None:
    box = SecretBox([encryption_key])
    assert box.open(box.seal(PAYLOAD)) == PAYLOAD


def test_the_ciphertext_contains_no_plaintext(encryption_key: str) -> None:
    sealed = SecretBox([encryption_key]).seal(PAYLOAD)
    assert b"hunter2" not in sealed
    assert b"DATABASE_URL" not in sealed


def test_sealing_twice_produces_different_ciphertext(encryption_key: str) -> None:
    """Fernet carries an IV and a timestamp, so identical payloads look unrelated.

    Worth pinning: a deterministic envelope would let anybody with read access to the
    table tell which two leases hold the same credential.
    """
    box = SecretBox([encryption_key])
    assert box.seal(PAYLOAD) != box.seal(PAYLOAD)


def test_a_wrong_key_raises_the_typed_error_and_explains_rotation(encryption_key: str) -> None:
    """``InvalidToken`` is an empty exception. The reader needs to be told about rotation,
    because that is almost always the actual cause."""
    sealed = SecretBox([encryption_key]).seal(PAYLOAD)
    other = SecretBox([generate_key()])
    with pytest.raises(DecryptionError) as caught:
        other.open(sealed)
    message = str(caught.value)
    assert "BAILMENT_PREVIOUS_ENCRYPTION_KEYS" in message
    assert "hunter2" not in message


def test_a_retired_key_still_opens_what_it_sealed(encryption_key: str) -> None:
    """Rotation is: prepend a new key, let the old leases drain, delete the old key.

    Without multi-key decryption, changing the key strands every live binding and you find
    out when an agent tries to use a lease that was fine an hour ago.
    """
    old = SecretBox([encryption_key])
    sealed = old.seal(PAYLOAD)

    fresh = generate_key()
    rotated = SecretBox([fresh, encryption_key])
    assert rotated.key_count == 2
    assert rotated.open(sealed) == PAYLOAD

    # New writes use the active key only, so the old key alone can no longer open them.
    with pytest.raises(DecryptionError):
        old.open(rotated.seal(PAYLOAD))


def test_rotate_reseals_without_the_plaintext_passing_through_python(
    encryption_key: str,
) -> None:
    sealed = SecretBox([encryption_key]).seal(PAYLOAD)
    fresh = generate_key()
    rotated = SecretBox([fresh, encryption_key]).rotate(sealed)

    assert SecretBox([fresh]).open(rotated) == PAYLOAD
    with pytest.raises(DecryptionError):
        SecretBox([encryption_key]).open(rotated)


def test_rotate_refuses_a_token_no_configured_key_opens(encryption_key: str) -> None:
    stranger = SecretBox([generate_key()]).seal(PAYLOAD)
    with pytest.raises(DecryptionError, match="no configured key opens it"):
        SecretBox([encryption_key]).rotate(stranger)


def test_sealing_refuses_an_empty_payload(encryption_key: str) -> None:
    with pytest.raises(SecretsError, match="empty binding payload"):
        SecretBox([encryption_key]).seal({})


def test_sealing_refuses_a_non_string_value_and_names_only_the_key(
    encryption_key: str,
) -> None:
    """A binding exists to be injected into a process environment; an int that survives to
    that point becomes a ``TypeError`` at the least convenient moment."""
    with pytest.raises(SecretsError) as caught:
        SecretBox([encryption_key]).seal({"PORT": 5432})  # type: ignore[dict-item]
    message = str(caught.value)
    assert "'PORT'" in message
    assert "5432" not in message


def test_sealing_refuses_a_non_string_key(encryption_key: str) -> None:
    with pytest.raises(SecretsError, match="keys must be strings"):
        SecretBox([encryption_key]).seal({1: "x"})  # type: ignore[dict-item]


@pytest.mark.parametrize(
    "token",
    [b"", b"not a fernet token", b"gAAAAA-nonsense", bytes(range(32))],
)
def test_opening_rubbish_raises_the_typed_error(encryption_key: str, token: bytes) -> None:
    with pytest.raises(DecryptionError):
        SecretBox([encryption_key]).open(token)


def test_opening_something_that_is_not_bytes_raises_the_typed_error(
    encryption_key: str,
) -> None:
    with pytest.raises(DecryptionError, match="not bytes"):
        SecretBox([encryption_key]).open(None)  # type: ignore[arg-type]


def test_a_payload_that_decrypts_to_the_wrong_shape_is_refused(encryption_key: str) -> None:
    """Defence against a hand-edited row, and against a future writer that seals a list."""
    raw = Fernet(encryption_key.encode("ascii"))
    box = SecretBox([encryption_key])

    with pytest.raises(DecryptionError, match="expected an object"):
        box.open(raw.encrypt(json.dumps(["a", "b"]).encode()))
    with pytest.raises(DecryptionError, match="non-string entry"):
        box.open(raw.encrypt(json.dumps({"PORT": 5432}).encode()))
    with pytest.raises(DecryptionError, match="not valid JSON"):
        box.open(raw.encrypt(b"\x00\x01 not json"))


def test_a_json_failure_does_not_carry_the_document_into_the_traceback(
    encryption_key: str,
) -> None:
    """``from None``, not ``from exc``: a ``JSONDecodeError`` holds the document it failed
    to parse, and any traceback renderer that prints locals would print the plaintext."""
    raw = Fernet(encryption_key.encode("ascii"))
    with pytest.raises(DecryptionError) as caught:
        SecretBox([encryption_key]).open(raw.encrypt(b"hunter2 is not json"))
    assert caught.value.__cause__ is None
    assert "hunter2" not in str(caught.value)


def test_a_malformed_key_names_its_position_and_never_its_material() -> None:
    """A malformed key is still a key somebody may be about to paste into a bug report."""
    with pytest.raises(InvalidKeyError) as caught:
        SecretBox(["not-a-fernet-key-but-still-a-secret"])
    message = str(caught.value)
    assert "encryption_key" in message
    assert "bailment keygen" in message
    assert "not-a-fernet-key" not in message

    with pytest.raises(InvalidKeyError) as caught:
        SecretBox([generate_key(), "also-not-a-key"])
    assert "previous_encryption_keys[0]" in str(caught.value)


def test_a_box_with_no_keys_is_refused() -> None:
    with pytest.raises(InvalidKeyError, match="at least one Fernet key"):
        SecretBox([])


def test_generated_keys_are_usable_and_distinct() -> None:
    first, second = generate_key(), generate_key()
    assert first != second
    assert SecretBox([first]).key_count == 1


def test_a_box_built_from_settings_uses_the_configured_keys(encryption_key: str) -> None:
    from bailment.config import Settings

    retired = generate_key()
    settings = Settings(encryption_key=encryption_key, previous_encryption_keys=retired)
    box = SecretBox.from_settings(settings)
    assert box.key_count == 2
    assert box.open(SecretBox([retired]).seal(PAYLOAD)) == PAYLOAD


# --------------------------------------------------------------------------------------
# References
# --------------------------------------------------------------------------------------


def test_a_reference_round_trips() -> None:
    reference = make_reference("3f2a9c1e-0000-0000-0000-000000000000")
    assert reference.startswith(REFERENCE_PREFIX)
    assert parse_reference(reference) == "3f2a9c1e-0000-0000-0000-000000000000"


@pytest.mark.parametrize(
    "value",
    ["", "3f2a9c1e", "bailment://binding", "https://example.com/binding/1", REFERENCE_PREFIX],
)
def test_a_bad_reference_is_refused_rather_than_guessed_at(value: str) -> None:
    """A caller passing a bare uuid has lost track of what kind of string they hold."""
    with pytest.raises(ValueError, match="reference|no id"):
        parse_reference(value)


# --------------------------------------------------------------------------------------
# The redactor: keys
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "password",
        "passwd",
        "DATABASE_PASSWORD",
        "api_key",
        "neon_api_key",
        "REDIS_TOKEN",
        "rest_token",
        "secret",
        "client_secret",
        "credential",
        "credentials",
        "dsn",
        "connection_string",
        "conn_str",
        "authorization",
        "auth_header",
        "cookie",
        "ciphertext",
        "private_key_pem",
        "signature",
    ],
)
def test_secret_looking_keys_are_recognised(name: str) -> None:
    assert is_secret_key(name)
    assert is_secret_key(name.upper())
    assert redact(**{name: "hunter2"})[name] == REDACTED


@pytest.mark.parametrize("name", sorted(SAFE_KEY_NAMES))
def test_the_allowlist_is_exact_matches_only(name: str) -> None:
    """An allowlist that matched substrings would not be an allowlist, it would be a hole.

    ``idempotency_key`` is the entry that earns the list: it appears on every request row,
    and an audit trail that cannot show it cannot answer "did this retry double-provision".
    """
    assert not is_secret_key(name)
    assert redact(**{name: "visible"})[name] == "visible"
    # The same name with anything appended is no longer the allowlisted one. Only the
    # entries that actually trip a hint substring can demonstrate this; ``output_names``
    # is on the list defensively and matches no hint at all.
    if any(hint in name for hint in ("key", "token")):
        assert is_secret_key(f"{name}_value")


@pytest.mark.parametrize("name", ["lease_id", "state", "external_name", "requester", "provider"])
def test_ordinary_keys_survive(name: str) -> None:
    assert not is_secret_key(name)
    assert redact(**{name: "visible"})[name] == "visible"


def test_over_redaction_is_the_accepted_failure() -> None:
    """A field called ``monkey`` is caught by the ``key`` substring. That is a mildly
    annoying log line; the other direction is a credential nobody can un-see."""
    assert is_secret_key("monkey")
    assert redact(monkey="banana")["monkey"] == REDACTED


# --------------------------------------------------------------------------------------
# The redactor: values
# --------------------------------------------------------------------------------------


def test_a_credential_bearing_uri_is_scrubbed_under_any_key_at_all() -> None:
    """The case that matters. The key here is innocent; the value is not."""
    result = redact(note="connecting to postgresql://neondb_owner:hunter2@ep-1.neon.tech/db")
    assert "hunter2" not in result["note"]  # type: ignore[operator]
    assert REDACTED in result["note"]  # type: ignore[operator]
    # The host survives, because knowing where a worker tried to connect is most of the
    # value of the line and none of the risk.
    assert "ep-1.neon.tech" in result["note"]  # type: ignore[operator]
    assert "neondb_owner" in result["note"]  # type: ignore[operator]


@pytest.mark.parametrize(
    "uri",
    [
        "postgresql://user:hunter2@host/db",
        "redis://default:hunter2@fly-redis.upstash.io:6379",
        "rediss://default:hunter2@host:6379",
        "https://admin:hunter2@dashboard.example.com/",
        "mongodb+srv://user:hunter2@cluster.example.net",
    ],
)
def test_every_uri_scheme_with_an_inline_password_is_scrubbed(uri: str) -> None:
    assert "hunter2" not in scrub_text(uri)
    assert "hunter2" not in scrub_text(f"failed to reach {uri} after 3 attempts")


@pytest.mark.parametrize(
    "token",
    [
        "sk-abcdefghijklmnopqrstuvwxyz",
        "ghp_abcdefghijklmnopqrstuvwxyz01",
        "github_pat_abcdefghijklmnopqrstuvwxyz",
        "xoxb-1234567890-abcdefghij",
        "AKIAIOSFODNN7EXAMPLE",
        "AIzaSyA1234567890abcdefghijklmnopqrstu",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g",
    ],
)
def test_well_known_token_shapes_are_scrubbed(token: str) -> None:
    assert scrub_text(token) == REDACTED
    assert redact(anything=f"got {token} back")["anything"] == f"got {REDACTED} back"


def test_our_own_fernet_tokens_are_scrubbed(encryption_key: str) -> None:
    """A binding's ciphertext is not plaintext, but it is still not log material."""
    sealed = SecretBox([encryption_key]).seal(PAYLOAD).decode("ascii")
    assert REDACTED in scrub_text(sealed)


def test_the_event_message_itself_is_scrubbed() -> None:
    """``log.error(f"connect failed: {dsn}")`` is a normal thing for a tired person to write."""
    result = redact(event="connect failed: postgresql://u:hunter2@host/db")
    assert "hunter2" not in result["event"]  # type: ignore[operator]


def test_bytes_are_never_rendered() -> None:
    """The only bytes in this system are Fernet ciphertext and raw key material."""
    result = redact(blob=b"gAAAAAB-whatever", innocent=bytearray(b"1234"))
    assert result["blob"] == "<bytes len=16>"
    assert result["innocent"] == "<bytes len=4>"
    # ...including under a key the key-rule already caught, so the length still shows.
    assert redact(ciphertext=b"12345")["ciphertext"] == "<bytes len=5>"


def test_nested_structures_are_walked() -> None:
    result = redact(
        provider={"response": {"password": "hunter2", "host": "db.example.com"}},
        outputs=["postgresql://u:hunter2@host/db", "fine"],
        pair=("postgresql://u:hunter2@host/db",),
        unique={"postgresql://u:hunter2@host/db"},
    )
    assert result["provider"] == {"response": {"password": REDACTED, "host": "db.example.com"}}
    assert "hunter2" not in str(result["outputs"])
    assert "hunter2" not in str(result["pair"])
    assert "hunter2" not in str(result["unique"])


def test_a_structure_deeper_than_the_ceiling_is_summarised_not_walked() -> None:
    """An unbounded walk is a denial of service waiting for a self-referential response."""
    payload: dict[str, object] = {"leaf": "postgresql://u:hunter2@host/db"}
    for _ in range(MAX_DEPTH + 3):
        payload = {"nested": payload}
    rendered = str(redact(**payload))
    assert "nested too deep" in rendered
    assert "hunter2" not in rendered


def test_the_redactor_never_mutates_the_caller_dict() -> None:
    original = {"password": "hunter2", "lease_id": "abc"}
    redacted = redact_secrets(None, "info", dict(original))
    assert redacted["password"] == REDACTED
    assert original["password"] == "hunter2"


def test_non_string_values_pass_through_unharmed() -> None:
    result = redact(count=3, ratio=1.5, flag=True, missing=None)
    assert result == {"count": 3, "ratio": 1.5, "flag": True, "missing": None}
