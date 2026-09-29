"""jambu-gpu entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer

from ..core.errors import JambuError
from . import inspect as inspect_cli
from . import lifecycle, providers, run, setup, status, validate
from ._common import Context, err_console, get_context

app = typer.Typer(
    name="jambu-gpu",
    help=(
        "Provider-agnostic CLI for provisioning and managing temporary GPU runtimes.\n\n"
        "jambu.yaml is the desired-state source of truth; .jambu/state.json is what "
        "was last observed."
    ),
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)


@app.callback()
def main_callback(
    ctx: typer.Context,
    config: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Path to jambu.yaml (default: nearest one upwards)."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Machine-readable output on stdout."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose diagnostics."),
) -> None:
    context = Context()
    context.config_path = config
    context.json = json_output
    context.verbose = verbose
    ctx.obj = context


app.command("init")(setup.init_command)
app.command("inspect-model")(inspect_cli.inspect_model_command)
app.command("profiles")(inspect_cli.profiles_command)
app.command("providers")(providers.providers_command)
app.command("validate")(validate.validate_command)
app.command("offers")(setup.offers_command)
app.command("setup")(setup.setup_command)
app.command(
    "run",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)(run.run_command)
app.command("status")(status.status_command)
app.command("logs")(status.logs_command)
app.command("events")(status.events_command)
app.command("lock")(lifecycle.lock_command)
app.command("unlock")(lifecycle.unlock_command)
app.command("stop")(lifecycle.stop_command)
app.command("destroy")(lifecycle.destroy_command)
app.command("guard")(lifecycle.guard_command)


@app.command("endpoint")
def endpoint_command(ctx: typer.Context) -> None:
    """Print the model endpoint URL (handy for shell pipelines)."""
    context = get_context(ctx)
    try:
        state = context.engine().store.load()
    except JambuError as exc:
        err_console.print(f"error: {exc}")
        raise typer.Exit(getattr(exc, "exit_code", 1)) from exc
    if not state.endpoint:
        err_console.print("error: no endpoint; run `jambu-gpu setup` first")
        raise typer.Exit(3)
    print(state.endpoint)


@app.command("version")
def version_command() -> None:
    """Print the CLI version."""
    from .. import __version__

    print(__version__)


def main() -> None:
    try:
        app()
    except JambuError as exc:  # safety net: errors are normalized, never tracebacks
        err_console.print(f"error: {exc}")
        sys.exit(getattr(exc, "exit_code", 1))
    except KeyboardInterrupt:
        err_console.print("interrupted")
        sys.exit(130)


if __name__ == "__main__":
    main()
