"""The envelope every credential bailment holds is stored inside.

A binding is a small JSON object -- ``{"DATABASE_URL": "postgresql://..."}`` -- sealed
with Fernet (AES-128-CBC plus HMAC-SHA256, authenticated, with a timestamp) and written
to a ``LargeBinary`` column. Nothing else in the system is allowed to see the plaintext:
the API returns a reference, the dashboard shows only which keys exist, and the CLI is
the single component that opens the envelope, and it does so to write the values straight
into a subprocess environment where the model that asked for them never sees them.

**Why an envelope of the whole dict, not per-value encryption.** Upstash hands back a URL
and a token that are useless apart. Sealing them together means they are revoked
together, rotated together and expire together, and there is no code path that can hand
out half a credential.

**Why decryption accepts several keys.** Rotating ``BAILMENT_ENCRYPTION_KEY`` with a
single-key box silently strands every live binding, and you discover it when an agent
tries to use a lease that was fine an hour ago. :class:`SecretBox` takes the active key
plus any retired ones, encrypts only with the first, and decrypts with any. Rotation
becomes: prepend a new key, let the old leases drain, delete the old key.

**Why a typed error.** ``cryptography.fernet.InvalidToken`` is an empty exception with no
message. Letting it escape produces a log line that says ``InvalidToken`` and nothing
else, and the reader has no way to know that the actual cause is almost always a changed
key rather than corrupt data. :class:`DecryptionError` says so.

Nothing in this module ever puts a plaintext value, or a ciphertext, into an exception
message or a log record.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

if TYPE_CHECKING:
    from bailment.config import Settings

#: The scheme an agent sees instead of a credential. Defined here because the reference
#: and the envelope are two halves of one idea, and because two components inventing two
#: reference formats is exactly the kind of drift this project exists to prevent.
REFERENCE_PREFIX = "bailment://binding/"


class SecretsError(RuntimeError):
    """Base class for anything that goes wrong handling sealed values."""


class InvalidKeyError(SecretsError):
    """A configured key is not a usable Fernet key."""


class DecryptionError(SecretsError):
    """A sealed payload could not be opened with any configured key."""


def generate_key() -> str:
    """A fresh Fernet key, as the url-safe base64 string that goes in the environment.

    Used by ``bailment keygen``. Deliberately the only place in the codebase that creates
    a key, so that no runtime path can decide to make one up.
    """
    return Fernet.generate_key().decode("ascii")


class SecretBox:
    """Seals and opens binding payloads.

    Construct once per process and pass it around; building a :class:`Fernet` per call
    re-derives nothing expensive but does make every call site a place where a key could
    be sourced differently, which is worse.
    """

    __slots__ = ("_fernet", "_key_count")

    def __init__(self, keys: Sequence[str]) -> None:
        """Take the active key first, then any retired decrypt-only keys."""
        if not keys:
            raise InvalidKeyError(
                "SecretBox needs at least one Fernet key; see Settings.require_encryption_key()"
            )
        fernets: list[Fernet] = []
        for position, key in enumerate(keys):
            # Position, never the key material. A malformed key is still a key that
            # somebody may be about to paste into a bug report.
            where = (
                "encryption_key" if position == 0 else f"previous_encryption_keys[{position - 1}]"
            )
            try:
                fernets.append(Fernet(key.encode("ascii")))
            except (ValueError, TypeError) as exc:
                raise InvalidKeyError(
                    f"{where} is not a valid Fernet key (32 url-safe base64 bytes); "
                    f"generate one with 'bailment keygen'"
                ) from exc
        self._fernet = MultiFernet(fernets)
        self._key_count = len(fernets)

    @classmethod
    def from_settings(cls, settings: Settings) -> SecretBox:
        """Build from configuration, failing with the 'run bailment keygen' message if unset."""
        return cls(settings.decryption_keys())

    @property
    def key_count(self) -> int:
        """How many keys can decrypt. Useful for a startup line; reveals nothing."""
        return self._key_count

    def seal(self, payload: Mapping[str, str]) -> bytes:
        """Encrypt a binding payload.

        Values must already be strings: a binding exists to be injected into a process
        environment, and an int that survives to that point becomes a ``TypeError`` at
        the least convenient moment. The check names the offending key -- key names are
        public by design, they are stored in ``Binding.output_names`` -- and never the
        value.
        """
        if not payload:
            raise SecretsError("refusing to seal an empty binding payload")
        for name, value in payload.items():
            if not isinstance(name, str):
                raise SecretsError(
                    f"binding payload keys must be strings, got {type(name).__name__}"
                )
            if not isinstance(value, str):
                raise SecretsError(
                    f"binding value for {name!r} must be a string, got {type(value).__name__}"
                )
        # sort_keys so that sealing the same payload twice differs only by Fernet's IV
        # and timestamp, which keeps ciphertext diffs meaningless rather than misleading.
        raw = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return self._fernet.encrypt(raw)

    def open(self, token: bytes) -> dict[str, str]:
        """Decrypt a binding payload.

        Raises :class:`DecryptionError` rather than letting ``InvalidToken`` escape,
        because the empty ``InvalidToken`` gives the reader nothing to act on and the
        cause is nearly always a rotated key.
        """
        try:
            raw = self._fernet.decrypt(token)
        except InvalidToken as exc:
            raise DecryptionError(
                "could not decrypt a stored binding with any configured key. The usual "
                "cause is that BAILMENT_ENCRYPTION_KEY changed; add the previous key to "
                "BAILMENT_PREVIOUS_ENCRYPTION_KEYS so existing leases keep working."
            ) from exc
        except TypeError as exc:
            raise DecryptionError("stored binding was not bytes") from exc
        try:
            decoded = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            # 'from None' rather than 'from exc': a JSONDecodeError carries the document
            # it failed to parse as an attribute, and any traceback renderer that prints
            # locals would print the plaintext credential along with it.
            raise DecryptionError("decrypted binding was not valid JSON") from None
        if not isinstance(decoded, dict):
            raise DecryptionError(
                f"decrypted binding was a {type(decoded).__name__}, expected an object"
            )
        result: dict[str, str] = {}
        for name, value in decoded.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise DecryptionError("decrypted binding contained a non-string entry")
            result[name] = value
        return result

    def rotate(self, token: bytes) -> bytes:
        """Re-seal an existing payload under the active key, without opening it here.

        ``MultiFernet.rotate`` keeps the plaintext inside the library. Used by the key
        rotation command so that retiring an old key does not require reading every
        credential into Python memory.
        """
        try:
            return self._fernet.rotate(token)
        except InvalidToken as exc:
            raise DecryptionError(
                "could not re-seal a stored binding: no configured key opens it"
            ) from exc


def make_reference(binding_id: str) -> str:
    """The handle an agent receives in place of a credential."""
    return f"{REFERENCE_PREFIX}{binding_id}"


def parse_reference(reference: str) -> str:
    """Extract the binding id from a reference, or raise :class:`ValueError`.

    Strict about the prefix on purpose: a caller passing a bare uuid is a caller who has
    lost track of what kind of string they are holding, and we would rather say so than
    guess.
    """
    if not reference.startswith(REFERENCE_PREFIX):
        raise ValueError(
            f"not a bailment binding reference (expected it to start with {REFERENCE_PREFIX!r})"
        )
    binding_id = reference[len(REFERENCE_PREFIX) :].strip()
    if not binding_id:
        raise ValueError("binding reference has no id")
    return binding_id
