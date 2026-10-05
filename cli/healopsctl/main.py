"""`healopsctl` entrypoint.

Phase 3 ships `plan`. It answers the question a reviewer asks before trusting any of this: *show me
what your system would do, without letting it do anything.*

The command runs entirely offline from a committed incident bundle. No cluster, no database, no
network. That is not a convenience -- it demonstrates that planning is a pure function of the
bundle, the same property the CI decision gate and the determinism digest rely on.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_SRC = REPO_ROOT / "engine" / "src"
if str(ENGINE_SRC) not in sys.path:
    sys.path.insert(0, str(ENGINE_SRC))

app = typer.Typer(
    name="healopsctl",
    help="HealOps operator CLI. Plan, replay, audit and freeze.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

# Exit codes are part of the contract: CI and shell scripts branch on them.
EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_DENIED = 3
EXIT_ERROR = 4


def _load_bundle(path: Path):  # noqa: ANN202 - concrete type needs the engine on sys.path
    from bundle import IncidentBundle

    if not path.is_file():
        console.print(f"[red]no such bundle:[/red] {path}")
        raise typer.Exit(EXIT_ERROR)
    try:
        return IncidentBundle.from_json(path.read_text(encoding="utf-8"))
    except (ValueError, KeyError) as exc:
        console.print(f"[red]bundle is not readable:[/red] {exc}")
        raise typer.Exit(EXIT_ERROR) from exc


def _render_plan(plan, decision) -> None:  # noqa: ANN001
    """Print a plan the way the Slack card shows it, because both audiences want the same facts."""
    from plan import Durability

    if plan.is_refusal:
        console.print(
            Panel(
                f"[yellow]{plan.refusal_reason}[/yellow]",
                title=f"[bold yellow]REFUSED[/bold yellow] -- {plan.runbook}",
                subtitle="a refusal is a first-class outcome, not an error",
            )
        )
        return

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("runbook", plan.runbook)
    table.add_row("target", plan.target_key)
    table.add_row("summary", plan.summary)
    table.add_row("", "")

    for i, action in enumerate(plan.actions, 1):
        table.add_row(f"action {i}" if i == 1 else "", action.describe())
        if action.body:
            table.add_row("", f"[dim]{json.dumps(action.body, separators=(',', ' '))}[/dim]")

    for i, undo in enumerate(plan.compensation, 1):
        table.add_row("undo" if i == 1 else "", undo.describe())

    table.add_row("", "")
    radius = plan.blast_radius
    table.add_row(
        "blast radius",
        f"{radius.pods} pods / {radius.nodes} nodes / "
        f"{'user-facing' if radius.user_facing else 'internal'} "
        f"(score {radius.score()})",
    )
    table.add_row("reversibility", str(plan.reversibility))
    durability = str(plan.durability)
    if plan.durability is Durability.EPHEMERAL:
        durability += f", expires in {plan.ttl_seconds}s"
    else:
        durability += " -- needs a pull request"
    table.add_row("durability", durability)
    table.add_row("confidence", f"{plan.confidence:.2f}")
    if plan.cost_delta_inr_month:
        table.add_row("cost delta", f"Rs {plan.cost_delta_inr_month:.0f}/month")
    if plan.expected_signal:
        table.add_row("alert should clear", plan.expected_signal.alert_should_clear)
        table.add_row("recovery window", f"{plan.expected_signal.window_seconds}s")
    table.add_row("determinism digest", f"[dim]{plan.digest()[:16]}...[/dim]")

    colour = {"allow": "green", "require_approval": "yellow", "deny": "red"}[decision.effect]
    console.print(
        Panel(
            table,
            title=f"[bold]PLAN[/bold] -- [{colour}]{decision.effect.upper()}[/{colour}]",
            subtitle="nothing was executed",
        )
    )

    if decision.reasons:
        console.print()
        for reason, message in zip(decision.reasons, decision.messages, strict=True):
            console.print(f"  [{colour}]{reason}[/{colour}]: {message}")


@app.command()
def plan(
    bundle_path: Annotated[
        Path,
        typer.Argument(
            metavar="BUNDLE",
            help="Path to a committed incident bundle JSON (see corpus/).",
        ),
    ],
    promotion: Annotated[
        str,
        typer.Option(
            "--promotion",
            help="Promotion rung to evaluate against: shadow, approval or auto.",
        ),
    ] = "auto",
    policy_file: Annotated[
        Path | None,
        typer.Option("--policy", help="Policy file to gate against."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable output instead of a table.")
    ] = False,
) -> None:
    """Print the plan a runbook would produce for an incident, without executing anything.

    Offline: reads the bundle, selects the runbook, plans, and evaluates the policy gate. Exits 2 on
    a refusal and 3 on a policy denial, so a script can assert that HealOps declines what it should.
    """
    import policy as policy_module
    from planner import plan_for
    from policy import PolicyDecision, PromotionSnapshot

    incident_bundle = _load_bundle(bundle_path)
    alertname = incident_bundle.cause_alert.alertname

    # The same pure function the decision eval grades and the orchestrator runs, so what this
    # prints is what production would decide -- GitOps preparation included.
    result = plan_for(
        incident_bundle,
        promotion=PromotionSnapshot(mode=promotion),
        active_policy=policy_module.load_policy(
            policy_file or REPO_ROOT / "policy" / "remediation.yaml"
        ),
    )
    if result.runbook is None:
        # An unrecognised alert escalates rather than being force-fitted to a runbook.
        console.print(
            f"[yellow]no runbook answers[/yellow] {alertname}; this incident would escalate "
            "to a human"
        )
        raise typer.Exit(EXIT_REFUSED)

    produced = result.plan
    decision = result.policy or PolicyDecision(effect="deny")

    if as_json:
        console.print_json(
            json.dumps({"plan": produced.to_dict(), "policy": decision.to_dict()}, default=str)
        )
    else:
        console.print()
        console.print(
            f"[dim]incident[/dim] {incident_bundle.incident_id}  "
            f"[dim]alert[/dim] {alertname}  "
            f"[dim]correlation[/dim] {incident_bundle.correlation_reason}"
        )
        if incident_bundle.symptom_alerts:
            # Forty alerts being one incident is the whole point of correlation, so say so.
            console.print(
                f"[dim]plus {len(incident_bundle.symptom_alerts)} symptom alert(s) attached as "
                "evidence -- they do not select their own runbook[/dim]"
            )
        console.print()
        _render_plan(produced, decision)
        console.print()

    if produced.is_refusal:
        raise typer.Exit(EXIT_REFUSED)
    if decision.denied:
        raise typer.Exit(EXIT_DENIED)


@app.command()
def runbooks() -> None:
    """List registered runbooks: triggers, and the chaos fixture that proves each one."""
    from runbooks import load_all

    registry = load_all()
    table = Table(title="Registered runbooks")
    table.add_column("runbook")
    table.add_column("answers")
    table.add_column("proven by")

    for entry in registry.describe():
        table.add_row(
            entry["name"],
            ", ".join(entry["triggers"]),
            entry["chaos_fixture"],
        )
    console.print(table)


def _owner() -> str:
    import getpass

    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name in some containers
        return "unknown"


def _with_session(operation):  # noqa: ANN001, ANN202 - engine types need sys.path set first
    """Run one breaker operation in its own transaction against the engine's database."""
    import asyncio

    async def run():  # noqa: ANN202
        from db import connection

        try:
            async with connection.transaction() as session:
                return await operation(session)
        finally:
            await connection.dispose()

    return asyncio.run(run())


@app.command()
def freeze(
    reason: Annotated[str, typer.Option("--reason", help="Why automation is being stopped.")],
    owner: Annotated[str | None, typer.Option("--owner", help="Who is freezing it.")] = None,
) -> None:
    """Turn all automation off, now, with no expiry -- e.g. --reason "release in progress".

    The freeze is a ledger entry with a reason and an owner, and it stays until `unfreeze`: a
    release ends when the release does, not when a timer says so.
    """
    import breaker

    who = owner or _owner()
    _with_session(lambda session: breaker.freeze(session, reason=reason, owner=who))
    console.print(f"[bold red]FROZEN[/bold red] by {who}: {reason}")
    console.print("[dim]every plan is now denied by policy until `healopsctl unfreeze`[/dim]")


@app.command()
def unfreeze(
    owner: Annotated[str | None, typer.Option("--owner", help="Who is lifting it.")] = None,
) -> None:
    """Lift a freeze (or clear a tripped breaker) and let automation resume."""
    import breaker

    who = owner or _owner()
    _with_session(lambda session: breaker.unfreeze(session, owner=who))
    console.print(f"[bold green]unfrozen[/bold green] by {who}")


@app.command()
def promote(
    runbook: Annotated[str, typer.Argument(help="Runbook name, e.g. crashloop_rollback.")],
    namespace: Annotated[str, typer.Argument(help="Namespace, or '-' for node-scoped runbooks.")],
    to: Annotated[str, typer.Option("--to", help="shadow, approval or auto.")],
    reason: Annotated[str, typer.Option("--reason", help="Why a human is overriding the ladder.")],
    owner: Annotated[str | None, typer.Option("--owner", help="Who is doing it.")] = None,
) -> None:
    """Move a runbook's rung by hand. A ledger entry like any other transition, with an owner.

    The ladder normally moves on evidence; this is the override for seeding a demo or for pulling
    authority immediately. Granting `auto` by hand is recorded as exactly that.
    """
    import promotion

    if to not in ("shadow", "approval", "auto"):
        console.print("[red]--to must be shadow, approval or auto[/red]")
        raise typer.Exit(EXIT_ERROR)
    who = owner or _owner()
    scope = None if namespace == "-" else namespace

    async def apply(session):  # noqa: ANN001, ANN202
        return await promotion.set_mode(session, runbook, scope, to, by=who, reason=reason)  # type: ignore[arg-type]

    moved = _with_session(apply)
    if moved is None:
        console.print(f"{runbook} in {namespace} is already on {to}")
    else:
        console.print(
            f"{runbook} in {namespace}: {moved.from_mode} -> [bold]{to}[/bold] ({reason})"
        )


@app.command()
def breaker_status() -> None:
    """Show the remediation breaker: state, why, who, and when it may half-open."""
    import breaker

    async def read(session):  # noqa: ANN001, ANN202
        return await breaker.view(session)

    current = _with_session(read)
    colour = {"closed": "green", "half_open": "yellow", "open": "red"}[current.state]
    console.print(f"breaker [{colour}]{current.state}[/{colour}]")
    if current.reason:
        console.print(f"  reason: {current.reason}  (owner {current.owner})")
    if current.until:
        console.print(f"  half-open at: {current.until.isoformat()}")


# --- evidence: export, replay, audit -------------------------------------------------------------

EVIDENCE_DIR = REPO_ROOT / "evidence"


@app.command()
def export(
    incident_id: Annotated[int, typer.Argument(help="Incident to export.")],
    out: Annotated[Path, typer.Option("--out", help="Directory to write into.")] = EVIDENCE_DIR,
) -> None:
    """Write an incident's content-addressed evidence bundle, named by its own sha256."""
    import evidence

    async def build(session):  # noqa: ANN001, ANN202
        return await evidence.build(session, incident_id)

    bundle = _with_session(build)
    if bundle is None:
        console.print(f"[red]no incident {incident_id}[/red]")
        raise typer.Exit(EXIT_ERROR)
    path = evidence.write(bundle, out)
    console.print(f"{path.stem}  ->  {path}")


@app.command()
def replay(
    ref: Annotated[
        str, typer.Argument(help="A bundle's sha256 (or a unique prefix), or its path.")
    ],
    directory: Annotated[Path, typer.Option("--dir", help="Where bundles live.")] = EVIDENCE_DIR,
) -> None:
    """Reconstruct an incident from its evidence bundle alone: no cluster, no network, no database.

    Byte-identical on every run. Exits 4 if the bundle does not match its own address or a ledger
    entry inside it no longer matches its hash.
    """
    import evidence
    import replay as replay_module

    try:
        digest, bundle = evidence.load(ref, directory)
    except evidence.EvidenceError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(EXIT_ERROR) from exc
    # Plain stdout, not rich: the output is a golden file and must not depend on the terminal.
    sys.stdout.write(replay_module.render(bundle, digest))
    if not evidence.verify(bundle).valid:
        raise typer.Exit(EXIT_ERROR)


@app.command()
def audit(
    verify_chain: Annotated[
        bool, typer.Option("--verify-chain", help="Walk the whole ledger from GENESIS.")
    ] = False,
    bundle: Annotated[
        str | None, typer.Option("--bundle", help="Verify one evidence bundle offline instead.")
    ] = None,
    directory: Annotated[Path, typer.Option("--dir", help="Where bundles live.")] = EVIDENCE_DIR,
) -> None:
    """Prove nobody edited the record. Exits 4 and names the sequence number on a break."""
    import evidence
    import ledger

    if bundle is not None:
        try:
            digest, data = evidence.load(bundle, directory)
        except evidence.EvidenceError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(EXIT_ERROR) from exc
        status = evidence.verify(data)
        label = f"bundle {digest[:16]}"
    elif verify_chain:

        async def walk(session):  # noqa: ANN001, ANN202
            return await ledger.verify_chain(session)

        status = _with_session(walk)
        label = "ledger"
    else:
        console.print("pass --verify-chain, or --bundle <sha>")
        raise typer.Exit(EXIT_ERROR)

    if status.valid:
        console.print(f"[green]{label}: {status.entries} entries, chain VALID[/green]")
        return
    console.print(f"[bold red]{label}: chain BROKEN at seq={status.broken_at_seq}[/bold red]")
    console.print(f"  {status.detail}")
    raise typer.Exit(EXIT_ERROR)


if __name__ == "__main__":
    app()
