"""harbor init — a guided first-time setup.

Five phases: mode, validated details, write, Crush integration, live verify.
Values are validated as entered (paths checked, endpoints probed, regexes
compiled); reachability problems WARN rather than block, because a parked box
is a legitimate state to set up from. Every exit names the next command.
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys

import requests
import typer
from rich.console import Console
from rich.rule import Rule

from . import config as config_mod

console = Console(highlight=False)


def _interactive() -> bool:
    """questionary needs a real terminal; piped stdin (tests, containers,
    scripts) falls back to plain prompts with identical semantics."""
    return sys.stdin.isatty()


def _ask_select(label: str, choices: list[str], default: str) -> str:
    if _interactive():
        import questionary
        return questionary.select(label, choices=choices,
                                  default=default).ask() or default
    while (a := typer.prompt(label, default=default).strip().lower()) \
            not in choices:
        _warn(f"choose one of: {', '.join(choices)}")
    return a


def _ask_text(label: str, default: str | None = None,
              validate=None) -> str:
    """validate: callable returning True or an error string."""
    if _interactive():
        import questionary
        q = questionary.text(label, default=default or "",
                             validate=validate)
        return q.ask() or (default or "")
    while True:
        raw = (typer.prompt(label, default=default) if default is not None
               else typer.prompt(label))
        verdict = validate(raw) if validate else True
        if verdict is True:
            return raw
        _warn(str(verdict))


def _ask_confirm(label: str, default: bool) -> bool:
    if _interactive():
        import questionary
        a = questionary.confirm(label, default=default).ask()
        return default if a is None else a
    return typer.confirm(label, default=default)


def _phase(n: int, title: str) -> None:
    console.print(Rule(f"[bold][{n}/5] {title}[/bold]"))


def _ok(msg: str) -> None:
    console.print(f"  [green]✓[/green] {msg}")


def _warn(msg: str) -> None:
    console.print(f"  [yellow]![/yellow] {msg}")


def _prompt_path(label: str, default: str) -> str:
    """A path answer; a missing file warns but is accepted — it may be
    created later (keys are often provisioned after setup)."""
    raw = _ask_text(label, default=default)
    if pathlib.Path(raw).expanduser().exists():
        _ok("file exists")
    else:
        _warn(f"{raw} does not exist yet — harbor will need it at run time")
    return raw


def _marker_verdict(raw: str):
    """True, or the reason the input must be retyped. An empty or
    uncompilable marker regex would make consult fail closed forever."""
    if not raw.strip():
        return "cannot be empty — consult refuses to run with no guard"
    try:
        re.compile(raw)
        return True
    except re.error as e:
        return f"not a valid regex ({e})"


def _prompt_markers() -> str:
    return _ask_text("Confidentiality markers (regex; consult refuses these)",
                     validate=_marker_verdict)


def _probe_endpoint(url: str) -> None:
    try:
        code = requests.get(f"{url}/health", timeout=3).status_code
        if code == 200:
            _ok(f"{url}/health answers")
        else:
            _warn(f"{url}/health answered {code}")
    except requests.RequestException:
        _warn(f"{url} not reachable right now (server down? that's fine — "
              "verify later with 'harbor status')")


def _probe_ssh(user: str, key: str, host: str) -> None:
    r = subprocess.run(
        ["ssh", "-i", str(pathlib.Path(key).expanduser()),
         "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
         "-o", "ConnectTimeout=3", f"{user}@{host}", "true"],
        capture_output=True)
    if r.returncode == 0:
        _ok(f"ssh {user}@{host} works")
    else:
        _warn(f"ssh {user}@{host} not reachable (box parked? that's fine)")


def _crush_phase() -> None:
    _phase(4, "Crush integration")
    if shutil.which("crush"):
        _ok("crush is installed")
        if _ask_confirm("Run 'harbor crush sync' now?", default=True):
            from . import crush as crush_mod
            try:
                applied = crush_mod.sync(config_mod.load(), apply=True)
                _ok("crush.json synced" + (f" ({len(applied)} keys)"
                                           if applied else " (no changes)"))
            except Exception as e:
                _warn(f"sync failed: {e} — run 'harbor crush sync' later")
    else:
        _warn("crush is not installed — harbor drives Crush, so you'll want it")
        cmd = ("brew install charmbracelet/tap/crush" if shutil.which("brew")
               else "see https://github.com/charmbracelet/crush#installation")
        console.print(f"    install with: {cmd}")
        if shutil.which("brew") and _ask_confirm("Run that now?",
                                                 default=False):
            subprocess.run(["brew", "install", "charmbracelet/tap/crush"])


def _verify_phase(managed: bool) -> None:
    _phase(5, "Verify")
    cfg = config_mod.load()
    _probe_endpoint(cfg.endpoint_url)
    if managed:
        console.print("  next: [bold]harbor up[/bold] — then "
                      "[bold]crush run 'hello'[/bold]")
    else:
        console.print("  next: [bold]crush run 'hello'[/bold] — or "
                      "[bold]harbor status[/bold] any time")

# Named rather than numbered: the mode shows up in bug reports, READMEs and
# support questions, where "3" carries no meaning. Each name says what YOU
# bring — Hyperstack credentials, a provider class, or a running server.
MODES = {
    "hyperstack": "harbor manages a Hyperstack GPU box for you",
    "provider":   "harbor manages a box elsewhere (you supply a provider class)",
    "endpoint":   "you already run a model server — harbor just drives it",
}

TEMPLATE = """\
# harbor configuration — written by `harbor init`; edit freely, re-render
# systemd units afterwards with `harbor install-units`.

[vm]
provider = "{provider}"
# Name is the identity (VM name AND tailnet hostname); ids change on redeploy.
name = "{vm_name}"
ip = "{vm_ip}"
ssh_user = "{ssh_user}"
ssh_key = "{ssh_key}"
api = "https://infrahub-api.nexgencloud.com/v1"
key_file = "{key_file}"
rate_per_hr = {rate_per_hr}
# Optional redeploy intent — see deployments/config.toml.example:
# flavors = ["n3-L40x1"]
# image = "Ubuntu Server 24.04 LTS R570 CUDA 12.8"
# keypair = "your-keypair"
# volume = 12345

[endpoint]  # the box serves directly on its tailnet address (no tunnel)
model_key_file = "{model_key_file}"
# Context the server actually serves. A client asking for more than this is
# told a window the server will not honour, so keep them in step.
slot_context = 262144
# Thinking effort per request: none | low | medium | high | max.
effort = "high"

[oracle]
# Marker guard — project identifiers that must never reach the frontier model.
# consult REFUSES to run if this is empty (fail closed). Regex, |-separated.
markers = "{markers}"
# Which frontier model answers a consult. Heavier is usually the point.
model = "{oracle_model}"
"""


BYO_TEMPLATE = """\
# harbor configuration — written by `harbor init`.
# No [vm] section: harbor manages no machine. It owns your agent config,
# enforces the policy hooks, runs flows, and guards escalation — against a
# model server you run yourself.

[endpoint]
url = "{url}"
model_key_file = "{model_key_file}"
slot_context = {slot_context}

[oracle]
# Identifiers that must NEVER reach the frontier model. `harbor consult`
# refuses outright when this is empty (fail closed).
markers = "{markers}"
model = "{oracle_model}"

[flow]
# Concurrent model calls a flow may run. 0 = auto-detect, which works for
# llama.cpp but NOT vLLM — set it explicitly there (vLLM's --max-num-seqs).
concurrency = {concurrency}
"""


def _byo(path) -> int:
    """The `endpoint` mode: point harbor at a server someone else runs."""
    _phase(2, "Your model server")
    url = _ask_text("Endpoint base URL (no /v1)",
                    default="http://localhost:8080")
    _probe_endpoint(url)
    values = {
        "url": url,
        "model_key_file": _prompt_path("Path to the file holding its API key",
                                       "~/.config/harbor/model-key"),
        "slot_context": typer.prompt("Context window the server serves",
                                     type=int, default=262144),
        "markers": _prompt_markers(),
        "oracle_model": _ask_text("Frontier model for consult", default="opus"),
        "concurrency": typer.prompt(
            "Concurrent calls a flow may make (0 = auto-detect)",
            type=int, default=0),
    }
    _phase(3, "Write config")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BYO_TEMPLATE.format(**values))
    _ok(f"wrote {path}")
    console.print("  harbor manages no machine in this mode: up/down/model "
                  "stand down, everything else works.")
    _crush_phase()
    _verify_phase(managed=False)
    return 0


def run() -> int:
    path = config_mod.config_path()
    if path.exists():
        typer.confirm(f"{path} exists — overwrite?", abort=True)

    _phase(1, "Mode")
    for name, blurb in MODES.items():
        console.print(f"  [bold]{name:<11}[/bold] {blurb}")
    mode = _ask_select("Which", list(MODES), default="endpoint")
    if mode == "endpoint":
        return _byo(path)

    _phase(2, "Your box")
    console.print("  Enter accepts the default; warnings don't block — a "
                  "parked box is fine.")
    values = {
        "provider": "hyperstack",
        "vm_name": _ask_text("VM name (also the tailnet hostname)"),
        "vm_ip": _ask_text("VM address (tailnet MagicDNS name or IP)"),
        "ssh_user": _ask_text("SSH user", default="ubuntu"),
        "ssh_key": _prompt_path("SSH key path", "~/.ssh/hyperstack_llm"),
        "key_file": _prompt_path("Hyperstack API key file",
                                 "~/.config/hyperstack/api-key"),
        "rate_per_hr": typer.prompt("On-demand rate $/hr", type=float, default=1.0),
        "model_key_file": _prompt_path("Model API key file",
                                       "~/.config/hyperstack/llm-cloud-key"),
        "markers": _prompt_markers(),
        "oracle_model": _ask_text("Frontier model for consult", default="opus"),
    }
    if mode == "provider":
        console.print("  This needs a class implementing "
                      "harbor.provider.Provider (state/start/stop).")
        values["provider"] = typer.prompt(
            "Import path", default="mycorp.harbor:MyProvider")
    _probe_ssh(values["ssh_user"], values["ssh_key"], values["vm_ip"])

    _phase(3, "Write config")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE.format(**values))
    _ok(f"wrote {path}")

    from . import units
    units.install_units(config_mod.load(path))
    _ok("systemd units rendered")

    _crush_phase()
    _verify_phase(managed=True)
    return 0
