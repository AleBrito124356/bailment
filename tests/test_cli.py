"""The operator's terminal, driven the way an operator drives it.

Every test here invokes the real Typer application against a real SQLite file, with its
configuration coming from ``BAILMENT_*`` environment variables exactly as a shell would
supply it. Nothing is injected: the CLI builds its own settings, its own engine and the
process-wide provider registry, which is the only way to test what the audit found broken
-- a reconciler in the CLI process asking a provider that had never seen the worker's
resources.

These are synchronous tests on purpose. Each CLI command runs its own event loop with
``asyncio.run``, so the test must not already be inside one; setup that needs the engine
goes through :meth:`Cli.run`, which does the same.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from collections.abc import Coroutine, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import pytest
from click.testing import Result
from typer.testing import CliRunner

from bailment.catalog.loader import load_catalog
from bailment.cli import app
from bailment.config import get_settings, reset_settings_cache
from bailment.db import dispose_engine, get_sessionmaker, session_scope
from bailment.engine.service import Caller, LeaseService, ProvisionRequest
from bailment.engine.worker import ProvisioningWorker
from bailment.providers import memory as memory_module
from bailment.providers import registry as registry_module
from bailment.providers.registry import default_registry
from bailment.states import LeaseState

T = TypeVar("T")


@dataclass
class Cli:
    database: Path
    runner: CliRunner

    def invoke(self, *args: str, code: int = 0) -> Result:
        result = self.runner.invoke(app, list(args))
        assert result.exit_code == code, (
            f"bailment {' '.join(args)} exited {result.exit_code}, expected {code}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}\n{result.exception!r}"
        )
        return result

    def json(self, *args: str, code: int = 0) -> Any:
        return json.loads(self.invoke(*args, "--json", code=code).stdout)

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        async def _wrapped() -> T:
            try:
                return await coro
            finally:
                await dispose_engine()

        return asyncio.run(_wrapped())

    def sql(self, statement: str) -> list[tuple[Any, ...]]:
        """Straight at the file, the way somebody with sqlite3 and a grudge would."""
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute(statement).fetchall()
            connection.commit()
        return rows

    def lease_state(self, lease_id: str) -> str:
        return str(self.sql(f"select state from leases where id = '{lease_id}'")[0][0])  # noqa: S608

    def age_everything(self, minutes: int = 20) -> None:
        shift = f"'-{minutes} minutes'"
        self.sql(
            f"update leases set created_at = datetime(created_at, {shift}), "  # noqa: S608
            f"activated_at = datetime(activated_at, {shift})"
        )
        self.sql(f"update memory_resources set created_at = datetime(created_at, {shift})")  # noqa: S608

    def request(self, inputs: dict[str, str], *, activate: bool = True) -> str:
        """Ask for a sandbox lease as an agent would, then let a worker pass run."""

        async def _request() -> str:
            settings = get_settings()
            catalog = load_catalog(settings.catalog_dir)
            registry = default_registry()
            async with session_scope() as session:
                outcome = await LeaseService(
                    session, catalog=catalog, registry=registry, settings=settings
                ).request_provision(
                    Caller.agent("agent-one", session="cli-test"),
                    ProvisionRequest(golden_path_id="sandbox", inputs=inputs),
                )
            if activate:
                worker = ProvisioningWorker(
                    get_sessionmaker(), catalog=catalog, registry=registry, settings=settings
                )
                await worker.tick()
            return outcome.lease.id

        return self.run(_request())

    def work(self) -> None:
        """One worker pass, with every backoff cleared first so nothing waits on a clock."""
        self.sql("update leases set next_attempt_at = null")

        async def _tick() -> None:
            settings = get_settings()
            await ProvisioningWorker(
                get_sessionmaker(),
                catalog=load_catalog(settings.catalog_dir),
                registry=default_registry(),
                settings=settings,
            ).tick()

        self.run(_tick())


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, encryption_key: str) -> Iterator[Cli]:
    database = tmp_path / "cli.db"
    for name, value in {
        "BAILMENT_DATABASE_URL": f"sqlite+aiosqlite:///{database.as_posix()}",
        "BAILMENT_ENCRYPTION_KEY": encryption_key,
        "BAILMENT_API_TOKENS": "agent-one:agent-token",
        "BAILMENT_ADMIN_TOKENS": "operator:operator-token",
        "BAILMENT_MAX_PROVISION_ATTEMPTS": "2",
        "BAILMENT_LOG_LEVEL": "warning",
        "BAILMENT_LOG_FORMAT": "console",
        # Wide enough that no table wraps a lease id or a command across two lines.
        "COLUMNS": "200",
    }.items():
        monkeypatch.setenv(name, value)
    reset_settings_cache()
    # The registry and the memory provider are process-wide singletons. A fresh pair per
    # test, so one test's fault hooks cannot leak into the next.
    monkeypatch.setattr(registry_module, "_DEFAULT", None)
    monkeypatch.setattr(memory_module, "_DEFAULT", None)
    handle = Cli(database=database, runner=CliRunner())
    handle.invoke("db", "upgrade")
    yield handle
    asyncio.run(dispose_engine())


# --------------------------------------------------------------------------------------
# bailment sandbox, and the cross-process reconcile it exists to demonstrate
# --------------------------------------------------------------------------------------


def test_a_reconcile_from_the_terminal_sees_the_worker_s_resources(cli: Cli) -> None:
    """The audit's reproduction, verbatim in shape: a healthy lease past the grace period
    and ``bailment reconcile`` in a process that did not create it. Nothing may change."""
    lease_id = cli.request({"name": "hello"})
    assert cli.lease_state(lease_id) == LeaseState.ACTIVE.value
    cli.age_everything()

    summary = cli.json("reconcile", "--provider", "memory")

    memory = summary["providers"][0]
    assert memory["resources_seen"] == 1
    assert memory["leases_checked"] == 1
    assert summary["drift_found"] == 0
    assert summary["orphans_found"] == 0
    assert cli.lease_state(lease_id) == LeaseState.ACTIVE.value


def test_sandbox_ls_shows_the_provider_side_of_the_ledger(cli: Cli) -> None:
    lease_id = cli.request({"name": "hello"})

    listed = cli.json("sandbox", "ls")
    assert [(r["lease_id"], r["lease_state"], r["simulate"]) for r in listed] == [
        (lease_id, "active", "ok")
    ]
    table = cli.invoke("sandbox", "ls").stdout
    assert f"lease {lease_id[:8]} (active) accounts for it" in table
    assert "shared store" in table


def test_vanish_then_reconcile_is_drift_and_releases_the_lease(cli: Cli) -> None:
    lease_id = cli.request({"name": "hello"})

    vanished = cli.invoke("sandbox", "vanish", lease_id[:8]).stdout
    assert f"deleted bailment-sandbox-{lease_id[:8]} out of band" in vanished
    assert "--grace 0s" in vanished
    assert cli.json("sandbox", "ls") == []
    assert cli.lease_state(lease_id) == LeaseState.ACTIVE.value

    # Inside the grace period the reconciler leaves a fresh lease alone...
    assert cli.json("reconcile", "--provider", "memory")["drift_found"] == 0
    # ...and --grace 0s is how a demo sees it without waiting ten minutes.
    summary = cli.json("reconcile", "--provider", "memory", "--grace", "0s", code=2)
    assert summary["drift_found"] == 1
    assert summary["providers"][0]["drift"][0]["to_state"] == "released"
    assert cli.lease_state(lease_id) == LeaseState.RELEASED.value


def test_a_planted_orphan_is_reported_and_destroy_removes_it(cli: Cli) -> None:
    planted = cli.invoke(
        "sandbox", "plant-orphan", "--name", "bailment-orphan-cli1", "--age", "15m"
    )
    assert "planted bailment-orphan-cli1" in planted.stdout

    report = cli.json("reconcile", "--provider", "memory", code=2)
    assert [o["external_name"] for o in report["providers"][0]["orphans"]] == [
        "bailment-orphan-cli1"
    ]

    destroyed = cli.invoke("reconcile", "--provider", "memory", "--destroy", "--yes")
    assert "destroyed" in destroyed.stdout
    assert cli.json("sandbox", "ls") == []


def test_plant_orphan_refuses_names_the_reconciler_could_never_own(cli: Cli) -> None:
    refused = cli.invoke("sandbox", "plant-orphan", "--name", "handmade-db", code=1)
    assert "does not start with the managed prefix" in refused.stderr

    lease_id = cli.request({"name": "hello"})
    taken = cli.invoke(
        "sandbox", "plant-orphan", "--name", f"bailment-sandbox-{lease_id[:8]}", code=1
    )
    assert "is live and already names" in taken.stderr


def test_a_demo_orphan_can_be_ended_from_the_terminal(cli: Cli) -> None:
    """simulate=destroy_failure, to ORPHANED, then relent and retry-teardown to RELEASED.

    Before this, ``relent`` was reachable from no surface and ``retry-teardown`` left the
    lease in a state no worker picks up, so the demo orphan was permanent.
    """
    lease_id = cli.request({"name": "stuck", "simulate": "destroy_failure"})
    cli.invoke("lease", "revoke", lease_id[:8], "--yes", "--reason", "finished")
    for _ in range(4):
        if cli.lease_state(lease_id) == LeaseState.ORPHANED.value:
            break
        cli.work()
    assert cli.lease_state(lease_id) == LeaseState.ORPHANED.value

    panel = cli.invoke("lease", "get", lease_id[:8]).stdout
    # An orphaned lease must not advertise a command that would only be refused.
    assert "bailment exec" not in panel
    assert "not usable while the lease is orphaned" in panel

    relented = cli.invoke("sandbox", "relent", lease_id[:8]).stdout
    assert "1 resource(s) will now accept a teardown" in relented
    assert f"bailment lease retry-teardown {lease_id[:8]}" in relented

    retried = cli.invoke("lease", "retry-teardown", lease_id[:8]).stdout
    assert "(deprovisioning)" in retried
    cli.work()
    assert cli.lease_state(lease_id) == LeaseState.RELEASED.value
    assert cli.json("sandbox", "ls") == []


def test_an_active_lease_still_advertises_exec(cli: Cli) -> None:
    lease_id = cli.request({"name": "hello"})
    panel = cli.invoke("lease", "get", lease_id[:8]).stdout
    assert f"bailment exec {lease_id[:8]} -- <command>" in panel


def test_sandbox_commands_refuse_a_lease_on_another_provider(cli: Cli) -> None:
    lease_id = cli.request({"name": "hello"})
    cli.sql(f"update leases set provider = 'neon' where id = '{lease_id}'")  # noqa: S608
    refused = cli.invoke("sandbox", "vanish", lease_id[:8], code=1)
    assert "only drives the built-in fake cloud" in refused.stderr
