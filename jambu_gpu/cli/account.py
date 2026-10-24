"""`gpu ls` / `gpu sweep` - every jambu instance in the account, not just this project's."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.table import Table
from rich.text import Text

from ..core import sweep as sweep_mod
from ..core.config import RuntimeConfig, find_config
from ..core.credentials import CredentialResolver
from ..core.durations import format_duration, parse_duration
from ..core.errors import ConfigError, JambuError
from ..core.events import EventLog
from ..core.state import RuntimeState, StateStore
from ..providers.registry import load_builtin_providers
from . import timer
from ._common import Context, console, fmt_money, get_context, handle, print_json, state_text


class Account:
    """A provider handle that works with or without a project around it."""

    def __init__(self, context: Context, provider_name: Optional[str] = None) -> None:
        self.config: Optional[RuntimeConfig] = None
        if context.config_path is not None or find_config() is not None:
            self.config = context.config()
        if self.config is not None:
            project_dir = self.config.project_dir
            profile = self.config.provider.profile
            name = provider_name or self.config.provider.name
            config = self.config
        else:
            # No project: a placeholder config only carries the provider name;
            # credentials come from the environment, ./.env, ~/.jambu/credentials.
            project_dir = Path.cwd()
            profile = "default"
            name = provider_name or "vast"
            config = RuntimeConfig.model_validate(
                {"provider": {"name": name}, "model": {"id": "jambu/account"}}
            )
        self.credentials = CredentialResolver(project_dir, profile=profile)
        self.provider = make_provider(config, self.credentials, name)

    def tracked(self) -> Optional[RuntimeState]:
        if self.config is None:
            return None
        store = StateStore(self.config.state_dir)
        if not store.exists():
            return None
        state = store.load()
        if not state.instance_id or state.provider != self.provider.name:
            return None
        return state


def make_provider(config: RuntimeConfig, credentials: CredentialResolver, name: str):
    """Seam for tests: they swap in a FakeProvider here."""
    return load_builtin_providers().get(config, credentials, name=name)


def ls_command(
    ctx: typer.Context,
    provider: Optional[str] = typer.Option(
        None, "--provider", help="Provider to list (default: jambu.yaml's, else vast)."
    ),
) -> None:
    """List every jambu instance in the provider account."""
    context = get_context(ctx)
    try:
        account = Account(context, provider)
        instances = sweep_mod.list_jambu_instances(account.provider)
        tracked = account.tracked()
    except JambuError as exc:
        handle(exc)
        return
    rows = sweep_mod.account_rows(instances, tracked.instance_id if tracked else None)

    if context.json:
        print_json({"provider": account.provider.name, "instances": rows})
        return
    if not rows:
        console.print(Text(f"no jambu instances on {account.provider.display_name}", style="dim"))
        return
    table = Table(title=f"{account.provider.display_name} instances", title_justify="left", box=None)
    for column in ("ID", "LABEL", "STATE", "GPU", "$/H", "AGE", "TRACKED"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            row["instance_id"],
            row["label"],
            state_text(row["state"]),
            row["gpu"] or "-",
            fmt_money(row["hourly_cost_usd"]) or "-",
            format_duration(row["age_seconds"]),
            Text("yes", style="green") if row["tracked"] else Text("no", style="dim"),
        )
    console.print(table)


def _seconds(flag: Optional[str], name: str) -> Optional[float]:
    try:
        return parse_duration(flag)
    except ValueError as exc:
        raise ConfigError(f"{name}: {exc}") from exc


def sweep_command(
    ctx: typer.Context,
    apply: bool = typer.Option(False, "--apply", help="Act. Without it the sweep only reports."),
    max_lifetime: Optional[str] = typer.Option(
        None, "--max-lifetime", help="Override lifecycle.max_lifetime (default 6h without a project)."
    ),
    stopped_ttl: Optional[str] = typer.Option(
        None, "--stopped-ttl", help="Override lifecycle.stopped_ttl (default 24h without a project)."
    ),
    provider: Optional[str] = typer.Option(
        None, "--provider", help="Provider to sweep (default: jambu.yaml's, else vast)."
    ),
    install_timer: bool = typer.Option(
        False, "--install-timer", help="Write a systemd user timer that runs the sweep."
    ),
    every: str = typer.Option("5m", "--every", help="Timer period for --install-timer."),
    uninstall_timer: bool = typer.Option(
        False, "--uninstall-timer", help="Remove the systemd user timer."
    ),
) -> None:
    """Stop or destroy jambu instances whose guard evidently failed (dry run by default).

    A running instance past max_lifetime + 30m is stopped when this project
    tracks it and destroyed otherwise. An instance stopped longer than
    stopped_ttl is destroyed. Instances without a jambu- label are never touched.
    """
    context = get_context(ctx)
    try:
        if install_timer or uninstall_timer:
            _timer(context, install_timer, every)
            return
        _sweep(context, apply, max_lifetime, stopped_ttl, provider)
    except JambuError as exc:
        handle(exc)


def _timer(context: Context, install: bool, every: str) -> None:
    if not install:
        result = timer.uninstall_timer()
        if context.json:
            print_json(result)
            return
        for path in result["removed"]:
            console.print(Text(f"- removed {path}", style="red"))
        if not result["removed"]:
            console.print(Text("= no timer installed", style="dim"))
        console.print(f"  then run: {result['disable']}")
        return

    every_s = _seconds(every, "--every")
    if not every_s:
        raise ConfigError("--every must be a positive duration, e.g. 5m")
    project = context.config_path or find_config()
    if project is not None:
        project = context.config().source_path
    result = timer.install_timer(every_s, project_config=project)
    result["project_config"] = str(project) if project else None
    if context.json:
        print_json(result)
        return
    console.print(Text(f"+ wrote {result['service']}", style="green"))
    console.print(Text(f"+ wrote {result['timer']}", style="green"))
    console.print(
        f"  every {format_duration(result['every_s'])}: `gpu sweep --apply`"
        + (f", plus `gpu guard` for {result['guards']} project(s)" if result["guards"] else "")
    )
    console.print(Text(f"> enable it: {result['enable']}", style="cyan"))
    console.print(
        Text(
            "  user timers stop at logout unless lingering is on: loginctl enable-linger $USER",
            style="dim",
        )
    )


def _sweep(
    context: Context,
    apply: bool,
    max_lifetime: Optional[str],
    stopped_ttl: Optional[str],
    provider_name: Optional[str],
) -> None:
    account = Account(context, provider_name)
    cfg = account.config
    if max_lifetime is not None:
        max_lifetime_s = _seconds(max_lifetime, "--max-lifetime")
    elif cfg is not None:
        max_lifetime_s = cfg.max_lifetime_s
    else:
        max_lifetime_s = sweep_mod.DEFAULT_MAX_LIFETIME_S
    if stopped_ttl is not None:
        stopped_ttl_s = _seconds(stopped_ttl, "--stopped-ttl")
    elif cfg is not None:
        stopped_ttl_s = cfg.stopped_ttl_s
    else:
        stopped_ttl_s = sweep_mod.DEFAULT_STOPPED_TTL_S

    sinks = [EventLog(sweep_mod.jambu_home() / "logs").bind(provider=account.provider.name)]
    if cfg is not None:
        sinks.append(EventLog(cfg.state_dir / "logs").bind(provider=account.provider.name))

    if not context.json:
        console.print(
            f"> sweeping {account.provider.display_name}: running > "
            f"{format_duration(max_lifetime_s + sweep_mod.SWEEP_GRACE_S) if max_lifetime_s else 'never'}, "
            f"stopped > {format_duration(stopped_ttl_s) if stopped_ttl_s else 'never'}"
            + ("" if apply else " (dry run)")
        )
    result = sweep_mod.sweep(
        account.provider,
        max_lifetime_s=max_lifetime_s,
        stopped_ttl_s=stopped_ttl_s,
        apply=apply,
        tracked=account.tracked(),
        allow_indefinite_lock=bool(cfg and cfg.lifecycle.allow_indefinite_lock),
        events=sinks,
        echo=context.echo,
    )
    if context.json:
        print_json(result.to_dict())
        return
    acted = [a for a in result.actions if a.action != "none"]
    if not acted:
        console.print(Text(f"= {result.seen} jambu instance(s), nothing to do", style="green"))
    elif not apply:
        console.print(Text(f"! {len(acted)} action(s) pending; re-run with --apply", style="yellow"))
    if any(a.error for a in result.actions):
        raise typer.Exit(1)
