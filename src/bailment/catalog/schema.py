"""Golden path definitions -- the single source of truth for the whole broker.

One YAML file describes a thing a platform team is willing to hand out. From that one
file bailment derives, with no second definition anywhere:

* the MCP tool an AI agent sees (name, description, JSON Schema for arguments),
* the web form a human sees in the dashboard,
* the Open Service Broker catalog entry,
* the policy rules evaluated on every request,
* the lease defaults and ceilings,
* the shape of the binding handed back.

The reason this is one file and not five is not tidiness. If the agent-facing catalog
and the human-facing catalog can drift, they *will* drift, and the first time they do
an agent gets a capability a human was never shown. Deriving both from one definition
makes that class of bug unrepresentable.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,48}[a-z0-9]$")


def parse_duration(raw: str) -> timedelta:
    """Parse a compact duration such as ``4h``, ``30m``, ``1h30m``, ``90s``.

    Deliberately not ISO 8601: these values are written by humans in YAML and read by
    humans in review, and ``PT4H`` helps nobody.
    """
    match = _DURATION_RE.fullmatch(raw.strip())
    if not match or raw.strip() == "":
        raise ValueError(
            f"invalid duration {raw!r}; expected a combination of hours, minutes and "
            f"seconds such as '4h', '30m', '1h30m' or '90s'"
        )
    hours, minutes, seconds = (int(g or 0) for g in match.groups())
    delta = timedelta(hours=hours, minutes=minutes, seconds=seconds)
    if delta <= timedelta(0):
        raise ValueError(f"duration {raw!r} must be greater than zero")
    return delta


def format_duration(delta: timedelta) -> str:
    """Render a timedelta back to the compact form. Round-trips :func:`parse_duration`."""
    total = int(delta.total_seconds())
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = [
        f"{hours}h" if hours else "",
        f"{minutes}m" if minutes else "",
        f"{seconds}s" if seconds else "",
    ]
    return "".join(parts) or "0s"


class Duration(str):
    """A duration string that validates and exposes :attr:`delta`."""

    delta: timedelta

    def __new__(cls, value: str) -> Self:
        obj = super().__new__(cls, value)
        obj.delta = parse_duration(value)
        return obj

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> Any:  # noqa: ANN401
        from pydantic_core import core_schema

        return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())


PolicyEffect = Literal["allow", "deny", "require_approval"]


class PolicyRule(BaseModel):
    """One rule in a golden path's policy chain.

    Rules are evaluated top to bottom and the first match wins, which is the only
    ordering semantics people reliably predict correctly. A rule with no ``when`` is an
    unconditional match and therefore acts as the default -- every path must end in one.
    """

    model_config = ConfigDict(extra="forbid")

    when: str | None = Field(
        default=None,
        description=(
            "A restricted expression over the request. Omit for an unconditional "
            "default rule. Evaluated by bailment's own sandboxed evaluator -- there is "
            "no eval() and no third-party expression engine anywhere in this path."
        ),
    )
    effect: PolicyEffect = Field(description="What happens when this rule matches.")
    reason: str = Field(
        min_length=1,
        description=(
            "Shown verbatim to the requester and written to the audit log. Write it for "
            "the person who just got blocked, not for the person who wrote the rule."
        ),
    )
    approvers: list[str] = Field(
        default_factory=list,
        description=(
            "Principals allowed to approve, when effect is require_approval. Empty "
            "means any authenticated approver."
        ),
    )


class LeasePolicy(BaseModel):
    """Lease duration defaults and ceilings for a golden path."""

    model_config = ConfigDict(extra="forbid")

    default_ttl: Duration = Field(
        default=Duration("4h"),
        description="Applied when the requester does not ask for a specific TTL.",
    )
    max_ttl: Duration = Field(
        default=Duration("72h"),
        description="Hard ceiling. A request above this is clamped, not rejected.",
    )
    warn_before: Duration = Field(
        default=Duration("30m"),
        description="How long before expiry the lease enters EXPIRING and notifies.",
    )
    renewable: bool = Field(
        default=True,
        description="Whether an active lease may be extended before it expires.",
    )
    max_renewals: int = Field(
        default=3,
        ge=0,
        description="Cap on extensions, so a lease cannot become permanent by attrition.",
    )

    @model_validator(mode="after")
    def _check_ordering(self) -> Self:
        if self.default_ttl.delta > self.max_ttl.delta:
            raise ValueError(f"default_ttl ({self.default_ttl}) exceeds max_ttl ({self.max_ttl})")
        if self.warn_before.delta >= self.default_ttl.delta:
            raise ValueError(
                f"warn_before ({self.warn_before}) must be shorter than default_ttl "
                f"({self.default_ttl}), otherwise every lease is born already expiring"
            )
        return self


class BindingOutput(BaseModel):
    """One value the consumer receives when a lease becomes active."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description="Environment-variable style name, e.g. DATABASE_URL.",
        pattern=r"^[A-Z][A-Z0-9_]*$",
    )
    description: str = ""
    secret: bool = Field(
        default=True,
        description=(
            "Secret outputs are never returned by value to an agent and never stored "
            "in the database in plaintext. The consumer receives a reference."
        ),
    )


class CostModel(BaseModel):
    """What this path is expected to cost, used for ceilings and for showing approvers."""

    model_config = ConfigDict(extra="forbid")

    estimated_monthly_usd: float = Field(default=0.0, ge=0)
    estimated_hourly_usd: float = Field(default=0.0, ge=0)
    note: str = ""

    @model_validator(mode="after")
    def _derive_hourly(self) -> Self:
        # 730 hours is the conventional cloud-billing month.
        if self.estimated_hourly_usd == 0.0 and self.estimated_monthly_usd > 0:
            object.__setattr__(
                self, "estimated_hourly_usd", round(self.estimated_monthly_usd / 730, 6)
            )
        return self


class GoldenPath(BaseModel):
    """A single provisionable capability.

    This is the object every surface in bailment renders from.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        description="Stable identifier. Appears in the MCP tool name and in URLs.",
    )
    name: str = Field(min_length=1, description="Human-readable title.")
    description: str = Field(
        min_length=1,
        description=(
            "Shown to humans in the catalog and to agents as the MCP tool description. "
            "Write it as an instruction to a capable stranger: what it gives you, what "
            "it costs, and when not to use it."
        ),
    )
    provider: str = Field(
        description="Registered provider id that knows how to create and destroy this."
    )
    enabled: bool = True
    tags: list[str] = Field(default_factory=list)

    inputs: dict[str, Any] = Field(
        description=(
            "A JSON Schema object describing the arguments. Used verbatim as the MCP "
            "tool input schema and to render the dashboard form."
        )
    )
    lease: LeasePolicy = Field(default_factory=LeasePolicy)
    policy: list[PolicyRule] = Field(min_length=1)
    cost: CostModel = Field(default_factory=CostModel)
    outputs: list[BindingOutput] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not _ID_RE.fullmatch(value):
            raise ValueError(
                f"invalid golden path id {value!r}; use lowercase letters, digits, "
                f"hyphens and underscores, starting with a letter (3-50 chars)"
            )
        return value

    @field_validator("inputs")
    @classmethod
    def _check_inputs_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("type") != "object":
            raise ValueError("inputs must be a JSON Schema of type 'object'")
        if "properties" not in value:
            raise ValueError("inputs must declare 'properties', even if empty")
        if value.get("additionalProperties") is not False:
            # Agents are enthusiastic. An open schema lets one smuggle unvalidated keys
            # straight through to a provider, so we close it rather than warn about it.
            value = {**value, "additionalProperties": False}
        return value

    @model_validator(mode="after")
    def _check_policy_has_default(self) -> Self:
        if self.policy[-1].when is not None:
            raise ValueError(
                f"golden path {self.id!r}: the last policy rule must be unconditional "
                f"(no 'when') so that every request gets a decision; add a final rule "
                f"such as {{effect: deny, reason: ...}}"
            )
        for rule in self.policy[:-1]:
            if rule.when is None:
                raise ValueError(
                    f"golden path {self.id!r}: only the last policy rule may omit "
                    f"'when'; an earlier unconditional rule makes the rest dead code"
                )
        return self

    @property
    def mcp_tool_name(self) -> str:
        """The tool name an agent calls. Namespaced so it cannot collide with other servers."""
        return f"bailment_provision_{self.id.replace('-', '_')}"

    def input_schema_with_lease(self) -> dict[str, Any]:
        """The inputs schema plus the optional, universally available ``ttl`` argument."""
        schema = {
            **self.inputs,
            "properties": {
                **self.inputs["properties"],
                "ttl": {
                    "type": "string",
                    "pattern": _DURATION_RE.pattern,
                    "description": (
                        f"How long to hold the lease, e.g. '2h' or '45m'. "
                        f"Defaults to {self.lease.default_ttl}; "
                        f"anything above {self.lease.max_ttl} is clamped to it."
                    ),
                },
            },
        }
        return schema
