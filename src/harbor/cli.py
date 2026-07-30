"""harbor — a private, guarded coding box on rented compute."""
from __future__ import annotations

import time

import typer

from . import config as config_mod
from . import state as state_mod

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="harbor — private guarded coding box on rented compute",
)


def _cfg() -> config_mod.Config:
    try:
        return config_mod.load()
    except FileNotFoundError:
        typer.echo(f"harbor: no config at {config_mod.config_path()}", err=True)
        raise typer.Exit(2)


def _cfg_with_vm() -> config_mod.Config:
    """For commands that manage a machine. Without a [vm] section harbor is in
    bring-your-own-endpoint mode and has nothing to manage."""
    cfg = _cfg()
    if not cfg.manages_vm:
        typer.echo(
            "harbor: no [vm] section in "
            f"{config_mod.config_path()} — harbor is pointed at an endpoint you "
            "run yourself, so there is no machine for it to manage.\n"
            "Add a [vm] section to have harbor manage one, or use the commands "
            "that don't need it (consult, flow, crush).", err=True)
        raise typer.Exit(2)
    return cfg


@app.command()
def init() -> None:
    """First-time setup: prompt for this machine's values, write config, render units."""
    from . import init_cmd
    raise typer.Exit(init_cmd.run())


@app.command()
def up() -> None:
    """Resume the GPU box, link into its network, wait until serving."""
    from . import lifecycle
    raise typer.Exit(lifecycle.up(_cfg_with_vm()))


@app.command()
def share() -> None:
    """Print the team onboarding message (blob included). Composed offline
    from what the last `harbor up` cached; safe to run any time."""
    from . import team as team_mod
    from . import wgnet
    cfg = _cfg_with_vm()
    status = team_mod.push_team_key(cfg)
    if status == "unreachable":
        typer.echo("note: box unreachable — the key ships at the next "
                   "'harbor up'; the message below is still valid.", err=True)
    try:
        typer.echo(team_mod.share_message())
    except wgnet.CacheMissing as e:
        typer.echo(f"harbor share: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def join(
    blob: str = typer.Argument(..., help="the blob from your operator's message"),
    force: bool = typer.Option(False, "--force",
                               help="overwrite an existing harbor config"),
) -> None:
    """Join a team's box: raise the private link, write config, set up Crush.
    Rerunnable — an aborted join continues where it stopped."""
    from . import team as team_mod
    raise typer.Exit(team_mod.join(blob, force=force))


@app.command()
def peers(
    action: str = typer.Argument("list", help="list | remove <address|pubkey>"),
    ident: str = typer.Argument(""),
) -> None:
    """Devices on the box's network. Offboarding is `peers remove` (cuts the
    tunnel) plus `keys rotate` (kills the share message)."""
    from . import keys as keys_mod
    cfg = _cfg_with_vm()
    script = ("cat /weights/wg/peers.json 2>/dev/null || echo '{}'"
              if action == "list" else f"""sudo python3 - <<'PY'
import json, pathlib, subprocess
ident = {ident!r}
f = pathlib.Path("/weights/wg/peers.json")
peers = json.loads(f.read_text()) if f.exists() else {{}}
hit = [k for k, v in peers.items() if ident in (k, v)]
if not hit:
    raise SystemExit(f"no peer matches {{ident!r}}")
for k in hit:
    subprocess.run(["wg", "set", "wg0", "peer", k, "remove"], check=True)
    del peers[k]
f.write_text(json.dumps(peers, indent=1))
print(f"removed {{len(hit)}} peer(s)")
PY""")
    if action == "remove" and not ident:
        typer.echo("harbor peers: remove needs an address or pubkey", err=True)
        raise typer.Exit(2)
    if action not in ("list", "remove"):
        typer.echo(f"harbor peers: unknown action '{action}'", err=True)
        raise typer.Exit(2)
    try:
        out = keys_mod._run(cfg, f"cd / && {script}")
    except state_mod.StateUnavailable as e:
        typer.echo(f"harbor peers: box unreachable ({e})", err=True)
        raise typer.Exit(1)
    if action == "list":
        table = __import__("json").loads(out or "{}")
        for pub, ip in sorted(table.items(), key=lambda kv: kv[1]):
            typer.echo(f"{ip:<14} {pub}")
        if not table:
            typer.echo("(no peers registered)")
    else:
        typer.echo(out.strip())


@app.command()
def down(
    release: bool = typer.Option(False, "--release",
                                 help="DELETE the VM, keep the volume; "
                                      "'harbor up' recreates it"),
    yes: bool = typer.Option(False, "--yes", help="skip the confirmation"),
) -> None:
    """Park the box (hibernate; GPU billing stops). With --release, delete it
    outright — everything durable lives on the volume."""
    from . import lifecycle, status as status_mod
    cfg = _cfg_with_vm()
    if release and not yes:
        if load := status_mod.pressure(cfg):
            typer.echo(f"current load: {load}")
        typer.confirm("Delete the VM (root disk dies; volume survives)?",
                      abort=True)
    raise typer.Exit(lifecycle.down(cfg, release=release))


@app.command()
def status() -> None:
    """Every layer: box · model · watchdog · credit."""
    from . import status as status_mod
    raise typer.Exit(status_mod.run(_cfg()))


@app.command()
def model(name: str = typer.Argument(..., help="thinkingcap | qwen35 | absolute .gguf path on the box")) -> None:
    """Switch the served model, then wait for health."""
    from . import model as model_mod
    raise typer.Exit(model_mod.switch(_cfg_with_vm(), name))


@app.command()
def consult(
    question: list[str] = typer.Argument(..., help="the abstracted question"),
    level: int = typer.Option(2, "-l", "--level", help="abstraction: 1 pseudonymized · 2 structural (default) · 3 canonical"),
    continue_session: bool = typer.Option(False, "-c", "--continue", help="continue the previous consultation"),
    model: str = typer.Option(None, "-m", "--model", help="frontier model alias (default from config)"),
) -> None:
    """Guarded escalation to the frontier model (blind: no code, no repo cwd)."""
    from . import consult as consult_mod
    raise typer.Exit(consult_mod.run(_cfg(), " ".join(question), level, continue_session, model))


@app.command()
def crush(
    action: str = typer.Argument("check", help="check (report drift) | sync (apply harbor-owned keys)"),
) -> None:
    """Assert harbor-owned keys in ~/.config/crush/crush.json (never touches yours)."""
    from . import crush as crush_mod
    cfg = _cfg()
    if action == "check":
        drift = crush_mod.check(cfg)
        if drift:
            typer.echo("crush.json drifts from harbor-owned config:")
            typer.echo("\n".join(drift))
            raise typer.Exit(1)
        typer.echo("crush.json: harbor-owned keys all in place")
    elif action == "sync":
        drift = crush_mod.sync(cfg, apply=True)
        if drift:
            typer.echo("applied:")
            typer.echo("\n".join(drift))
        else:
            typer.echo("crush.json: already in sync")
    else:
        typer.echo(f"harbor crush: unknown action '{action}'", err=True)
        raise typer.Exit(2)


@app.command()
def flow(
    script: str = typer.Argument(..., help="path to a flow script (Python)"),
    resume: str = typer.Option(None, "--resume", help="run-id of a prior run to replay completed calls from"),
) -> None:
    """Run a workflow script composing generate/agent/parallel primitives."""
    from . import flow as flow_mod
    raise typer.Exit(flow_mod.run_script(script, resume=resume))


@app.command(hidden=True)
def watchdog() -> None:
    """Idle check — invoked by the systemd timer, not a user command."""
    from . import watchdog as watchdog_mod
    raise typer.Exit(watchdog_mod.tick(_cfg_with_vm()))


@app.command(hidden=True)
def install_units() -> None:
    """Render + install systemd user units — invoked by install.sh."""
    from . import units
    units.install_units(_cfg_with_vm())


@app.command()
def keys(
    action: str = typer.Argument("list", help="list | add <name> | revoke <name> | rotate"),
    name: str = typer.Argument("", help="key owner (letters, digits, - and _)"),
) -> None:
    """Per-user model API keys. The token prints ONCE at issue time; the
    server accepts any listed key, so revoke + re-render cuts access.
    `rotate` re-mints the shared team key — the old share message dies."""
    from . import keys as keys_mod
    from . import model as model_mod
    from . import team as team_mod
    cfg = _cfg()
    try:
        if action == "list":
            for n in keys_mod.list_names(cfg):
                typer.echo(n)
            return
        if action == "add":
            token = keys_mod.add(cfg, name)
            typer.echo(f"key for {name!r} — shown ONCE, put it in their "
                       f"model_key_file:\n{token}")
        elif action == "revoke":
            keys_mod.revoke(cfg, name)
            typer.echo(f"revoked {name!r}")
        elif action == "rotate":
            token = team_mod.rotate_local()
            keys_mod._run(cfg, f"umask 077 && echo '{token}' > team.key")
            typer.echo("team key rotated — the old share message is dead; "
                       "print a fresh one with 'harbor share'")
        else:
            typer.echo(f"harbor keys: unknown action '{action}'", err=True)
            raise typer.Exit(2)
        # The serving unit embeds the key list; apply the change now.
        rc = model_mod.rerender(cfg)
        if rc != 0:
            typer.echo("key stored but NOT yet served — re-render failed; "
                       "run 'harbor model <name>'", err=True)
            raise typer.Exit(rc)
    except keys_mod.KeyExists as e:
        typer.echo(f"harbor keys: {e}", err=True)
        raise typer.Exit(1)
    except state_mod.StateUnavailable as e:
        typer.echo(f"harbor keys: box unreachable ({e})", err=True)
        raise typer.Exit(1)


@app.command()
def hold(duration: str = typer.Argument("2", help="hours ('4' or '4h'), or 'off' to release")) -> None:
    """Keep the box awake during long work (default 2h, self-expiring).
    The hold is shared: it blocks every user's watchdog, not just this one."""
    cfg = _cfg()
    try:
        if duration == "off":
            state_mod.release_hold(cfg)
            typer.echo("watchdog hold released")
            return
        try:
            hours = float(duration.rstrip("h"))
        except ValueError:
            typer.echo(f"harbor hold: '{duration}' is not a number of hours "
                       "or 'off'", err=True)
            raise typer.Exit(2)
        expiry = state_mod.set_hold(hours, cfg)
    except state_mod.StateUnavailable as e:
        typer.echo(f"harbor hold: box state unreachable — hold NOT placed "
                   f"({e})", err=True)
        raise typer.Exit(1)
    hhmm = time.strftime("%H:%M", time.localtime(expiry))
    typer.echo(f"watchdog hold set for {hours:g}h (until {hhmm}); 'harbor hold off' to release")
