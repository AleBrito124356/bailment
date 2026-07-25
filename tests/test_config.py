"""Settings, and the two refusals that matter.

Both tests here cover a *refusal to start*. That is deliberate: the failure modes they
describe are silent ones. A broker running with no key and a broker running with a key
published on GitHub both look completely healthy, and the difference only becomes visible
at the point where it is expensive.
"""

from __future__ import annotations

import pytest

from bailment.config import DEMO_ENCRYPTION_KEY, ENV_PREFIX, ConfigError, Settings
from bailment.secrets import generate_key


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


def test_a_missing_key_is_fatal_at_first_use_not_at_import() -> None:
    """Absent must be legal at import so `bailment keygen` runs on a machine with no key."""
    settings = _settings(encryption_key=None)
    with pytest.raises(ConfigError) as excinfo:
        settings.require_encryption_key()
    message = str(excinfo.value)
    assert "bailment keygen" in message
    assert f"{ENV_PREFIX}ENCRYPTION_KEY is not set" in message
    # It must say why it will not just make one up, or the next person adds that "fix".
    assert "permanently unreadable" in message


def test_bailment_refuses_to_start_with_the_published_demo_key() -> None:
    """The whole point of publishing the demo key is that using it cannot be an accident.

    A default credential that ships with a comment asking politely that it be changed is
    found in production three years later. This one stops the process.
    """
    settings = _settings(encryption_key=DEMO_ENCRYPTION_KEY, allow_demo_key=False)
    with pytest.raises(ConfigError) as excinfo:
        settings.require_encryption_key()
    message = str(excinfo.value)
    assert "public git repository" in message
    assert f"{ENV_PREFIX}ALLOW_DEMO_KEY" in message
    assert "bailment keygen" in message


def test_the_demo_key_works_when_the_demo_says_so() -> None:
    settings = _settings(encryption_key=DEMO_ENCRYPTION_KEY, allow_demo_key=True)
    assert settings.require_encryption_key() == DEMO_ENCRYPTION_KEY


def test_a_real_key_never_needs_the_opt_in() -> None:
    key = generate_key()
    assert _settings(encryption_key=key).require_encryption_key() == key


def test_the_demo_key_is_a_structurally_valid_fernet_key() -> None:
    """If it were not, the guard would be dead code and compose would fail on the default."""
    from bailment.secrets import SecretBox

    box = SecretBox([DEMO_ENCRYPTION_KEY])
    assert box.open(box.seal({"a": "b"})) == {"a": "b"}


def test_retired_keys_follow_the_active_one_for_decryption() -> None:
    active, retired = generate_key(), generate_key()
    settings = _settings(encryption_key=active, previous_encryption_keys=f" {retired} , ")
    assert settings.decryption_keys() == (active, retired)


def test_the_demo_guard_also_covers_the_key_reached_through_decryption_keys() -> None:
    """decryption_keys() goes through require_encryption_key(), so it must refuse too --
    otherwise there is a second door into the same room."""
    settings = _settings(encryption_key=DEMO_ENCRYPTION_KEY, allow_demo_key=False)
    with pytest.raises(ConfigError):
        settings.decryption_keys()
