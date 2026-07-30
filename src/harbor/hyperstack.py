"""Hyperstack (NexGenCloud) API client — the minimal surface harbor uses."""
from __future__ import annotations

import requests

from .config import Config

TIMEOUT = 20


class HyperstackError(RuntimeError):
    """API-level failure carrying the provider's message (e.g. stock outages)."""

    def __init__(self, status: int, message: str):
        super().__init__(f"hyperstack {status}: {message}")
        self.status = status
        self.message = message


def _req(cfg: Config, method: str, path: str, body: dict | None = None) -> dict:
    r = requests.request(
        method, f"{cfg.api}{path}", json=body,
        headers={"api_key": cfg.key_file.read_text().strip()},
        timeout=TIMEOUT,
    )
    if not r.ok:
        try:
            message = r.json().get("message", r.text[:200])
        except Exception:
            message = r.text[:200]
        raise HyperstackError(r.status_code, message)
    return r.json()


def _get(cfg: Config, path: str) -> dict:
    return _req(cfg, "GET", path)


# name -> id, per API host. A redeploy changes the id under a stable name, so
# create/destroy invalidate this.
_ID_CACHE: dict[tuple[str, str], int] = {}


def resolve_id(cfg: Config) -> int | None:
    """The id of the VM named cfg.vm_name, or None if no such VM exists."""
    key = (cfg.api, cfg.vm_name)
    if key in _ID_CACHE:
        return _ID_CACHE[key]
    data = _get(cfg, "/core/virtual-machines")
    for inst in data.get("instances", []):
        if inst.get("name") == cfg.vm_name:
            _ID_CACHE[key] = int(inst["id"])
            return _ID_CACHE[key]
    return None


def create(cfg: Config, flavor: str, user_data: str) -> int:
    """Create the VM with a public address (which speaks only WireGuard,
    SSH and peer registration — the firewall in cloud-init sees to that)
    and attach the weights volume. Returns the new id."""
    body = {
        "name": cfg.vm_name,
        "environment_name": cfg.environment,
        "image_name": cfg.image,
        "flavor_name": flavor,
        "key_name": cfg.keypair,
        "assign_floating_ip": True,
        "user_data": user_data,
        "count": 1,
    }
    data = _req(cfg, "POST", "/core/virtual-machines", body)
    vm_id = int(data["instances"][0]["id"])
    _ID_CACHE[(cfg.api, cfg.vm_name)] = vm_id
    attach_volume(cfg, vm_id)
    return vm_id


def attach_volume(cfg: Config, vm_id: int) -> None:
    """Attach the weights volume, waiting out the build first — the API
    refuses attachment until the VM reaches ACTIVE or SHUTOFF."""
    import time as _time
    for _ in range(60):
        inst = _get(cfg, f"/core/virtual-machines/{vm_id}").get("instance", {})
        if inst.get("status") in ("ACTIVE", "SHUTOFF"):
            break
        _time.sleep(10)
    _req(cfg, "POST", f"/core/virtual-machines/{vm_id}/attach-volumes",
         {"volume_ids": [cfg.volume_id]})


def destroy(cfg: Config) -> None:
    vm_id = resolve_id(cfg)
    if vm_id is None:
        return
    _req(cfg, "DELETE", f"/core/virtual-machines/{vm_id}")
    _ID_CACHE.pop((cfg.api, cfg.vm_name), None)


def floating_ip(cfg: Config) -> str:
    """The VM's public address, '' while none is assigned yet."""
    try:
        vm_id = resolve_id(cfg)
        if vm_id is None:
            return ""
        data = _get(cfg, f"/core/virtual-machines/{vm_id}")
    except (requests.RequestException, HyperstackError):
        return ""
    return data.get("instance", {}).get("floating_ip") or ""


def gpu_from_flavor(flavor: str) -> str:
    """'n3-L40x1' -> 'L40', 'n3-RTX-A6000x2' -> 'RTX-A6000'."""
    import re
    m = re.match(r"^n\d+-(.+?)x\d+", flavor)
    return m.group(1) if m else flavor


def vm_info(cfg: Config) -> tuple[str, str]:
    """(status, gpu model) — '' fields when unknowable, ABSENT when no VM
    carries the configured name."""
    try:
        vm_id = resolve_id(cfg)
        if vm_id is None:
            return "ABSENT", ""
        data = _get(cfg, f"/core/virtual-machines/{vm_id}")
    except (requests.RequestException, HyperstackError):
        return "", ""
    inst = data.get("instance", {})
    return (inst.get("status", ""),
            gpu_from_flavor(inst.get("flavor", {}).get("name", "")))


def gpu_stock(cfg: Config, gpu: str, region: str = "CANADA-1") -> int | None:
    """Units of our GPU class available (1x config); None when unknowable.
    Hibernated VMs need free stock to resume — 0 means 'harbor up' will fail."""
    try:
        data = _get(cfg, "/core/stocks")
    except (requests.RequestException, HyperstackError):
        return None
    for entry in data.get("stocks", []):
        if entry.get("region") != region:
            continue
        for m in entry.get("models", []):
            name = str(m.get("model", ""))
            if name == gpu or name.startswith(gpu):
                return int(m.get("configurations", {}).get("1x", 0) or 0)
    return None


def vm_state(cfg: Config) -> str:
    """VM status string (ACTIVE, HIBERNATED, ...); 'ABSENT' when no VM has
    the configured name; '' if the API answer is malformed."""
    try:
        vm_id = resolve_id(cfg)
        if vm_id is None:
            return "ABSENT"
        data = _get(cfg, f"/core/virtual-machines/{vm_id}")
    except (requests.RequestException, HyperstackError):
        return ""
    return data.get("instance", {}).get("status", "")


def resume(cfg: Config) -> None:
    _get(cfg, f"/core/virtual-machines/{resolve_id(cfg)}/hibernate-restore")


def start(cfg: Config) -> None:
    """Bring a SHUTOFF box back. Distinct from resume(): the provider has two
    off-states with different endpoints, and SHUTOFF bills full compute."""
    _get(cfg, f"/core/virtual-machines/{resolve_id(cfg)}/start")


def hibernate(cfg: Config) -> tuple[bool, str]:
    """Must be hibernate, NOT stop — stopped VMs still bill in full on Hyperstack."""
    data = _get(cfg, f"/core/virtual-machines/{resolve_id(cfg)}/hibernate")
    return bool(data.get("status")), data.get("message", "")


def credit(cfg: Config) -> str:
    """Account credit as a display string; '?' when unavailable (balance is batched)."""
    try:
        data = _get(cfg, "/billing/user-credit/credit")
        return f"{data['data']['credit']:.2f}"
    except Exception:
        return "?"


class HyperstackProvider:
    """The built-in provider. Hyperstack has TWO off-states with different
    endpoints and different billing — HIBERNATED bills storage only, SHUTOFF
    bills full compute — so both normalise to OFF and start() picks the right
    call. That distinction is exactly the kind of vendor detail that belongs
    behind the seam rather than in harbor's lifecycle logic."""

    def state(self, cfg: Config) -> str:
        from . import provider as p
        raw = vm_state(cfg)
        if not raw:
            return p.UNKNOWN
        if raw == "ABSENT":
            return p.ABSENT
        if raw == "ACTIVE":
            return p.ACTIVE
        if raw in ("HIBERNATED", "SHUTOFF"):
            return p.OFF
        return p.TRANSITIONING          # HIBERNATING, BUILDING, DELETING, ...

    def start(self, cfg: Config) -> None:
        from . import provider as p
        raw = vm_state(cfg)
        try:
            if raw == "SHUTOFF":
                start(cfg)              # /start — a stopped VM bills full rate
            else:
                resume(cfg)             # /hibernate-restore
        except HyperstackError as e:
            if "stock" in e.message.lower():
                raise p.ProviderError(
                    f"provider has no free capacity — {e.message}. The VM stays "
                    "parked; retry when stock returns.") from e
            raise

    def stop(self, cfg: Config) -> None:
        # Hibernate, never stop: a SHUTOFF VM still bills full compute here.
        hibernate(cfg)

    # --- optional redeploy capability (see provider.py) -------------------
    def stock(self, cfg: Config, flavor: str) -> int:
        n = gpu_stock(cfg, gpu_from_flavor(flavor))
        return n if n is not None else 0

    def create(self, cfg: Config, flavor: str, user_data: str) -> int:
        import time as _time

        from . import provider as p
        # A just-deleted predecessor holds the volume until its deletion
        # finishes; attaching would fail, so wait for it to free up.
        for _ in range(60):
            try:
                data = _get(cfg, f"/core/volumes/{cfg.volume_id}")
                status = data.get("volume", {}).get("status", "")
            except (requests.RequestException, HyperstackError):
                status = ""
            if status == "available":
                break
            _time.sleep(10)
        else:
            raise p.ProviderError(
                f"volume {cfg.volume_id} never became available — is the "
                "previous VM still deleting?")
        try:
            return create(cfg, flavor, user_data)
        except HyperstackError as e:
            raise p.ProviderError(f"create failed — {e.message}") from e

    def destroy(self, cfg: Config) -> None:
        destroy(cfg)

    def attach(self, cfg: Config) -> None:
        """Ensure the weights volume is attached — the recovery half of a
        create that died between VM build and attachment. Already-attached
        is success, not an error."""
        vm_id = resolve_id(cfg)
        if vm_id is None:
            return
        try:
            attach_volume(cfg, vm_id)
        except HyperstackError as e:
            if "already" not in e.message.lower():
                raise

    def public_ip(self, cfg: Config) -> str:
        return floating_ip(cfg)
