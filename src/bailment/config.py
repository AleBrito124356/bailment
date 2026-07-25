"""Runtime configuration for every bailment process.

One ``Settings`` object serves the API, the MCP server, the lease workers, the reconciler
and the CLI. They share it because a worker that disagrees with the API about which
database to write to is a worker that loses resources.

Four decisions here are load-bearing.

**The encryption key is required to *use*, not to *import*.** ``bailment keygen`` has to
run on a machine that does not have a key yet, so importing this module cannot demand
one. Every code path that actually touches a binding calls
:meth:`Settings.require_encryption_key`, which fails with an instruction instead of a
traceback. What we must never do is invent a key when one is missing: a process that
quietly generates its own key makes every binding written before it permanently
undecryptable, silently, and you find out during the incident rather than before it.

**Provider credentials are all optional.** A missing Neon token makes the Neon provider
*unavailable* -- a fact the dashboard shows and the MCP catalog reflects -- and nothing
else. It is never an import-time or startup-time crash, because the ``sandbox`` golden
path exists precisely so somebody with no cloud accounts at all can run the whole system
end to end.

**Unknown ``BAILMENT_*`` variables are ignored, then reported.** Rejecting them outright
turns a stray variable in a compose file into a crash loop. Ignoring them in silence
turns ``BAILMENT_NEON_KEY`` -- note the missing ``_API`` -- into an unexplained "provider
unavailable" that costs somebody an afternoon. :func:`unknown_env_vars` gives startup the
material to log the difference.

**Tokens are compared with :func:`hmac.compare_digest`.** Not because a timing attack on
a broker is likely, but because ``==`` on a credential is the kind of thing that gets
copied into the next project.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import os
import re
import socket
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, PrivateAttr, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX: Final = "BAILMENT_"

#: The key shipped as the default in ``docker-compose.yml``.
#:
#: A quickstart that runs with one command has to have a key from somewhere, and every
#: alternative is worse: generating one per container gives the API and the worker different
#: keys, so nothing the worker seals can ever be opened; generating one on the host turns
#: `docker compose up` into a two-step ritual that a reader will skip and then file a bug
#: about.
#:
#: So the key is published, and using it is a hard startup failure unless
#: ``BAILMENT_ALLOW_DEMO_KEY`` is set. That inverts the usual arrangement, where a default
#: credential ships with a comment asking politely that it be changed and is then found in
#: production three years later. Here the demo works, and the copy-pasted deployment
#: refuses to start and says exactly why -- which is the only version of this that survives
#: contact with someone in a hurry.
DEMO_ENCRYPTION_KEY: Final = "EQTZ5ZIU6i0mNGpw1Cs-j--D98tsBwsx8A-AI_LbY-Q="  # gitleaks:allow

#: A Fernet key is 32 raw bytes in url-safe base64: 43 characters plus one '=' of padding.
_FERNET_KEY_RE: Final = re.compile(r"^[A-Za-z0-9_-]{43}=$")

#: Which settings each first-party provider needs before it can be considered available.
#: A provider absent from this table declares no credential requirements, which is the
#: correct answer for the built-in ``memory`` provider and for anything a third party
#: registers without touching this file.
PROVIDER_CREDENTIALS: Final[dict[str, tuple[str, ...]]] = {
    "neon": ("neon_api_key", "neon_project_id"),
    "upstash": ("upstash_email", "upstash_api_key"),
    "cloudflare": ("cloudflare_api_token", "cloudflare_zone_id"),
    "memory": (),
}


class ConfigError(RuntimeError):
    """Configuration is missing or wrong in a way the operator has to fix.

    Carries a remedy, not just a complaint: every raise site in this module tells the
    reader which environment variable to set and, where relevant, which command produces
    a valid value.
    """


@dataclass(frozen=True, slots=True)
class TokenIdentity:
    """Who a bearer token belongs to, and whether it may approve things."""

    principal: str
    admin: bool


@dataclass(frozen=True, slots=True)
class _TokenEntry:
    principal: str
    token: str
    admin: bool


def _fingerprint(token: str) -> str:
    """A stable, non-reversible label for a token that was configured without a name.

    Two anonymous tokens have to be distinguishable in the audit log or the log is
    useless in exactly the incident where it matters. A truncated SHA-256 gives that
    without putting any part of the token itself on disk.
    """
    return "token-" + sha256(token.encode("utf-8")).hexdigest()[:8]


def _parse_tokens(spec: str, *, admin: bool) -> tuple[_TokenEntry, ...]:
    """Parse ``"alice:s3cret, ci:other"`` into entries.

    Split on the *first* colon, so a token may itself contain colons. A bare entry with
    no colon is treated as a token whose principal is derived from its fingerprint;
    naming them is strongly preferred and the dashboard says so.
    """
    entries: list[_TokenEntry] = []
    for raw in spec.split(","):
        item = raw.strip()
        if not item:
            continue
        principal, sep, token = item.partition(":")
        if not sep:
            entries.append(_TokenEntry(principal=_fingerprint(item), token=item, admin=admin))
            continue
        principal, token = principal.strip(), token.strip()
        if not principal or not token:
            raise ConfigError(
                f"malformed entry in {ENV_PREFIX}"
                f"{'ADMIN_TOKENS' if admin else 'API_TOKENS'}: expected "
                f"'principal:token' or a bare token, separated by commas"
            )
        entries.append(_TokenEntry(principal=principal, token=token, admin=admin))
    return tuple(entries)


class Settings(BaseSettings):
    """Everything bailment reads from the environment.

    Every field below is settable as ``BAILMENT_<FIELD_NAME_UPPERCASED>``, or in a
    ``.env`` file in the working directory.
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Storage -----------------------------------------------------------
    database_url: str = Field(
        default="sqlite+aiosqlite:///./bailment.db",
        description=(
            "Async SQLAlchemy URL. SQLite by default so that cloning the repo and "
            "running the sandbox path needs no database at all; point it at "
            "postgresql+asyncpg://... for anything with more than one worker."
        ),
    )
    db_echo: bool = Field(
        default=False,
        description="Echo SQL to the log. Noisy; useful when a claim query misbehaves.",
    )

    # --- Secrets -----------------------------------------------------------
    encryption_key: SecretStr | None = Field(
        default=None,
        description=(
            "Fernet key protecting every binding at rest. Generate with 'bailment "
            "keygen'. Absent is legal at import time and fatal at first use."
        ),
    )
    previous_encryption_keys: str = Field(
        default="",
        description=(
            "Comma-separated retired keys, newest first. Used for decryption only, so "
            "that rotating encryption_key does not strand bindings written before the "
            "rotation. Drop a key from here once nothing sealed with it is still live."
        ),
    )
    allow_demo_key: bool = Field(
        default=False,
        description=(
            "Permit the published demo key from docker-compose.yml. Only the local demo "
            "should ever set this; see DEMO_ENCRYPTION_KEY for why it is a hard refusal "
            "rather than a warning."
        ),
    )

    # --- Catalog -----------------------------------------------------------
    catalog_dir: Path = Field(
        default=Path(__file__).resolve().parent / "catalog" / "paths",
        description=(
            "Directory of golden path YAML files. Defaults to the four paths shipped "
            "inside the package; mount your own over it in production."
        ),
    )

    # --- Identity and authentication ---------------------------------------
    worker_id: str = Field(
        default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}",
        description=(
            "Written into Lease.claimed_by. Must be unique per process; the default "
            "host-pid pair is. Two processes sharing an id will steal each other's "
            "in-flight work."
        ),
    )
    api_tokens: str = Field(
        default="",
        description=(
            "Comma-separated 'principal:token' pairs that may request and read leases. "
            "The principal lands in Lease.requester and in every audit row, so name them."
        ),
    )
    admin_tokens: str = Field(
        default="",
        description=(
            "Comma-separated 'principal:token' pairs that may additionally approve, "
            "revoke and force teardown. Admin implies everything an api token can do."
        ),
    )
    allow_anonymous: bool = Field(
        default=False,
        description=(
            "Accept unauthenticated requests as the principal 'anonymous'. For the local "
            "demo only, and it is off by default so nobody gets it by accident. It is a "
            "separate switch rather than 'no tokens configured means open' precisely "
            "because that implicit rule is how open brokers happen."
        ),
    )

    # --- Serving -----------------------------------------------------------
    host: str = Field(default="127.0.0.1", description="Bind address. Containers set 0.0.0.0.")
    port: int = Field(default=8080, ge=1, le=65535)
    public_base_url: str = Field(
        default="http://localhost:8080",
        description=(
            "How a human reaches the dashboard from outside the container. Used to build "
            "approval links, which are worthless if they point at 127.0.0.1."
        ),
    )

    # --- Engine timing -----------------------------------------------------
    lease_tick_seconds: int = Field(
        default=15,
        ge=1,
        description=(
            "How often the lease engine looks for leases to warn about, expire or tear "
            "down. This is the resolution of the whole TTL promise: a 5 minute sandbox "
            "lease with a 60 second tick can outlive its deadline by a minute."
        ),
    )
    reconcile_interval_seconds: int = Field(
        default=300,
        ge=10,
        description="How often the reconciler compares provider reality against the lease table.",
    )
    reconcile_enabled: bool = Field(default=True)
    reconcile_auto_destroy_orphans: bool = Field(
        default=False,
        description=(
            "Whether the reconciler may destroy a resource it believes is orphaned. Off "
            "by default: finding orphans is safe, and deleting something a human created "
            "by hand outside bailment is not. Turn it on once you trust the tagging."
        ),
    )
    provider_timeout_seconds: float = Field(
        default=20.0,
        gt=0,
        description="Default httpx timeout for provider calls. There is no unbounded call.",
    )
    max_provision_attempts: int = Field(
        default=5,
        ge=1,
        description="Attempts before a provision or teardown stops retrying and surfaces.",
    )

    # --- Logging -----------------------------------------------------------
    log_level: str = Field(default="info", description="debug, info, warning, error or critical.")
    log_format: Literal["json", "console"] = Field(
        default="json",
        description="json for anything that ships logs, console for a human at a terminal.",
    )

    # --- Provider credentials, all optional --------------------------------
    neon_api_key: SecretStr | None = Field(default=None)
    neon_project_id: str | None = Field(
        default=None,
        description=(
            "Neon branches live inside a project; without one there is nowhere to put a branch."
        ),
    )
    upstash_email: str | None = Field(default=None)
    upstash_api_key: SecretStr | None = Field(default=None)
    cloudflare_api_token: SecretStr | None = Field(default=None)
    cloudflare_zone_id: str | None = Field(
        default=None, description="The zone every dns-record lease writes into."
    )

    _token_entries: tuple[_TokenEntry, ...] = PrivateAttr(default=())

    @field_validator("encryption_key")
    @classmethod
    def _check_encryption_key(cls, value: SecretStr | None) -> SecretStr | None:
        """Reject a malformed key at load time rather than at first decrypt.

        A typo'd key that is only discovered when the first lease goes active has already
        cost you a provisioned resource with an unreadable binding.
        """
        if value is None:
            return None
        raw = value.get_secret_value().strip()
        if not raw:
            return None
        if not _FERNET_KEY_RE.fullmatch(raw):
            raise ValueError(
                f"{ENV_PREFIX}ENCRYPTION_KEY is not a Fernet key (expected 44 url-safe "
                f"base64 characters). Generate one with 'bailment keygen'."
            )
        try:
            decoded = base64.urlsafe_b64decode(raw.encode("ascii"))
        except (binascii.Error, ValueError) as exc:  # pragma: no cover - regex covers it
            raise ValueError(f"{ENV_PREFIX}ENCRYPTION_KEY is not valid base64") from exc
        if len(decoded) != 32:
            raise ValueError(
                f"{ENV_PREFIX}ENCRYPTION_KEY decodes to {len(decoded)} bytes; Fernet needs 32"
            )
        return SecretStr(raw)

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        level = value.strip().lower()
        allowed = {"debug", "info", "warning", "error", "critical"}
        if level not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return level

    def model_post_init(self, context: object, /) -> None:
        # Admin entries first so that a token listed in both lists resolves to the more
        # privileged identity rather than to whichever list happened to be parsed first.
        self._token_entries = _parse_tokens(self.admin_tokens, admin=True) + _parse_tokens(
            self.api_tokens, admin=False
        )

    # --- Secrets -----------------------------------------------------------
    def require_encryption_key(self) -> str:
        """Return the Fernet key, or explain exactly how to get one.

        Called by anything that is about to seal or open a binding. Deliberately not a
        property: the call site should read like a precondition, because it is one.
        """
        if self.encryption_key is None:
            raise ConfigError(
                f"{ENV_PREFIX}ENCRYPTION_KEY is not set. Every binding bailment stores is "
                f"encrypted with it, so there is nothing sensible to do without one.\n"
                f"  Generate a key:  bailment keygen\n"
                f"  Then export it:  {ENV_PREFIX}ENCRYPTION_KEY=<the key>\n"
                f"bailment will not generate a key for you at runtime: a key that appears "
                f"by itself is a key that changes on restart, and every binding written "
                f"before that restart becomes permanently unreadable."
            )

        key = self.encryption_key.get_secret_value()
        if key == DEMO_ENCRYPTION_KEY and not self.allow_demo_key:
            raise ConfigError(
                f"{ENV_PREFIX}ENCRYPTION_KEY is the demo key published in bailment's "
                f"docker-compose.yml. It is in a public git repository, so anything sealed "
                f"with it can be opened by anyone who has read the repository.\n"
                f"  Generate your own:  bailment keygen\n"
                f"This almost always means a compose file or an .env was copied out of the "
                f"quickstart and pointed at something real. If you genuinely are running the "
                f"local demo, set {ENV_PREFIX}ALLOW_DEMO_KEY=true, which the shipped compose "
                f"file already does."
            )
        return key

    def decryption_keys(self) -> tuple[str, ...]:
        """The active key followed by any retired keys, in decrypt-preference order."""
        primary = self.require_encryption_key()
        retired = tuple(k.strip() for k in self.previous_encryption_keys.split(",") if k.strip())
        return (primary, *retired)

    # --- Authentication ----------------------------------------------------
    def lookup_token(self, presented: str | None) -> TokenIdentity | None:
        """Resolve a bearer token to an identity, or ``None`` if it is not ours.

        Scans every configured entry rather than returning on first match so the work
        does not depend on where in the list the token sits.
        """
        if not presented:
            return None
        found: TokenIdentity | None = None
        for entry in self.token_entries:
            if hmac.compare_digest(entry.token, presented) and (entry.admin or found is None):
                found = TokenIdentity(principal=entry.principal, admin=entry.admin)
        return found

    @property
    def token_entries(self) -> tuple[_TokenEntry, ...]:
        return self._token_entries

    @property
    def auth_configured(self) -> bool:
        """Whether anyone can authenticate at all. False plus ``allow_anonymous`` off is
        a broker nobody can call, which startup should say out loud."""
        return bool(self.token_entries)

    # --- Providers ---------------------------------------------------------
    def missing_provider_credentials(self, provider: str) -> tuple[str, ...]:
        """Environment variable names this provider needs and does not have.

        Returns the ``BAILMENT_*`` names rather than the field names because the answer
        is going into a message read by whoever has to set them.
        """
        missing: list[str] = []
        for field_name in PROVIDER_CREDENTIALS.get(provider, ()):
            value = getattr(self, field_name, None)
            if isinstance(value, SecretStr):
                value = value.get_secret_value()
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append(f"{ENV_PREFIX}{field_name.upper()}")
        return tuple(missing)

    def provider_available(self, provider: str) -> bool:
        """Whether this provider has everything it needs to talk to its API."""
        return not self.missing_provider_credentials(provider)


def unknown_env_vars(environ: dict[str, str] | None = None) -> tuple[str, ...]:
    """``BAILMENT_*`` variables that no setting claims, sorted.

    Almost always a typo. Startup logs these at warning level; see the module docstring
    for why they are not a hard error.
    """
    env = os.environ if environ is None else environ
    known = {f"{ENV_PREFIX}{name.upper()}" for name in Settings.model_fields}
    return tuple(sorted(name for name in env if name.startswith(ENV_PREFIX) and name not in known))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings, read once.

    Cached because reading a ``.env`` file on every request is silly and because two
    parts of the same process disagreeing about configuration is worse than silly.
    """
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings. For tests, and for a CLI that has just written a key."""
    get_settings.cache_clear()
