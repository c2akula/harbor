"""Configuration: ~/.config/harbor/config.toml, overridable via HARBOR_CONF."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import effort as effort_mod
from . import wgnet

DEFAULT_PATH = Path.home() / ".config" / "harbor" / "config.toml"


def _valid_effort(level: str) -> str:
    effort_mod.budget(level)     # raises naming the valid set
    return level


@dataclass(frozen=True)
class Config:
    # [vm] — OPTIONAL. Absent means harbor manages no machine: you point it at
    # an endpoint you already run, and the lifecycle commands stand down.
    # Identity is a NAME, not an id: a released-and-redeployed box keeps its
    # name while the provider id changes underneath. The box's reachable
    # address is discovered at `up` and cached (wgnet.cache_*), not configured.
    vm_name: str
    provider: str
    ssh_user: str
    ssh_key: Path
    api: str
    key_file: Path
    rate_per_hr: float
    # [endpoint]
    model_key_file: Path
    slot_context: int
    # Effort label (see harbor.effort) — the thinking-budget dial.
    effort: str
    flow_concurrency: int
    endpoint_url: str
    # [oracle]
    oracle_markers: str
    oracle_model: str
    # Redeploy intent: SKU fallback order, pinned image, provider keypair,
    # the weights volume. Optional — without them, `up` on an absent box
    # refuses instead of creating.
    flavors: tuple[str, ...] = ()
    environment: str = "default-CANADA-1"
    image: str = ""
    keypair: str = ""
    volume_id: int = 0

    @property
    def manages_vm(self) -> bool:
        """False when no [vm] section was configured: harbor owns no machine
        lifecycle and just provides the harness (crush config, hooks, flows,
        consult) against an endpoint someone else runs."""
        return bool(self.vm_name)


def config_path() -> Path:
    return Path(os.environ.get("HARBOR_CONF", DEFAULT_PATH))


def load(path: Path | None = None) -> Config:
    p = path or config_path()
    with open(p, "rb") as f:
        raw = tomllib.load(f)
    vm, ep, orc = raw.get("vm", {}), raw.get("endpoint", {}), raw.get("oracle", {})
    vm_name = vm.get("name", "")
    return Config(
        # "" means "no machine to manage" — see manages_vm.
        vm_name=vm_name,
        # "hyperstack", or an import path to your own Provider subclass.
        provider=vm.get("provider", "hyperstack"),
        ssh_user=vm.get("ssh_user", "ubuntu"),
        ssh_key=Path(vm.get("ssh_key", "~/.ssh/hyperstack_llm")).expanduser(),
        api=vm.get("api", "https://infrahub-api.nexgencloud.com/v1"),
        key_file=Path(vm.get("key_file", "~/.config/hyperstack/api-key")).expanduser(),
        rate_per_hr=float(vm.get("rate_per_hr", 1.0)),
        flavors=tuple(vm.get("flavors", [])),
        environment=vm.get("environment", "default-CANADA-1"),
        image=vm.get("image", ""),
        keypair=vm.get("keypair", ""),
        volume_id=int(vm.get("volume", 0)),
        # Explicit url wins; a managed box serves on the hub address of the
        # harbor-owned WireGuard network — constant for every client.
        endpoint_url=ep.get(
            "url",
            wgnet.ENDPOINT_URL if vm_name else "http://127.0.0.1:8080",
        ).rstrip("/"),
        model_key_file=Path(ep.get("model_key_file", "~/.config/hyperstack/llm-cloud-key")).expanduser(),
        slot_context=ep.get("slot_context", 131072),
        effort=_valid_effort(ep.get("effort", effort_mod.DEFAULT)),
        # How many model calls a flow may run at once. 0 = auto-detect.
        flow_concurrency=raw.get("flow", {}).get("concurrency", 0),
        # No default markers: consult refuses to run when unset (fail closed).
        oracle_markers=orc.get("markers", ""),
        oracle_model=orc.get("model", ""),
    )
