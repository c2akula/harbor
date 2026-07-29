"""Per-layer readout. A single /health conflates three independent failures
(VM parked / endpoint down / model not loaded); this reports them apart."""
from __future__ import annotations

import subprocess
import time

import requests

from . import hyperstack, state
from .config import Config


def _say(layer: str, value: str) -> None:
    print(f"{layer:<10} {value}")


def pressure(cfg: Config) -> str | None:
    """Load line for a shared box: running/queued sequences and preemptions.
    The context window is deliberately over-committed (every client is told
    the full window against one shared KV pool), so pressure must be visible
    rather than silent."""
    try:
        key = cfg.model_key_file.read_text().strip()
        m = requests.get(f"{cfg.endpoint_url}/metrics", timeout=5,
                         headers={"Authorization": f"Bearer {key}"}).text
    except requests.RequestException:
        return None

    def total(metric: str) -> int:
        return int(sum(float(line.rsplit(" ", 1)[1])
                       for line in m.splitlines()
                       if line.startswith(metric + "{")))

    try:
        running = total("vllm:num_requests_running")
        queued = total("vllm:num_requests_waiting")
        pre = total("vllm:num_preemptions_total")
        out = f"{running} running, {queued} queued"
        if pre:
            out += f", {pre} preemptions this boot (KV pool under pressure)"
        return out
    except Exception:
        return None


def model_serving(cfg: Config) -> str | None:
    """Name of the served checkpoint, 'unknown' if serving but unnamed, None if
    down. Engine-agnostic: llama.cpp answers /health with {"status":"ok"} and
    lists models[0].model; vLLM answers 200-with-empty-body and lists the
    OpenAI shape data[0]. Accept both rather than assume one."""
    base = cfg.endpoint_url
    try:
        if requests.get(f"{base}/health", timeout=5).status_code != 200:
            return None
    except requests.RequestException:
        return None
    try:
        key = cfg.model_key_file.read_text().strip()
        data = requests.get(f"{base}/v1/models", timeout=5,
                            headers={"Authorization": f"Bearer {key}"}).json()
        if entries := data.get("data"):           # OpenAI shape (vLLM)
            e = entries[0]
            return (e.get("root") or e.get("id", "")).split("/")[-1] or "unknown"
        return data["models"][0]["model"].split("/")[-1]   # llama.cpp shape
    except Exception:
        return "unknown"


def _vm_uptime_seconds(cfg: Config) -> int | None:
    r = subprocess.run(
        ["ssh", "-i", str(cfg.ssh_key), "-o", "ConnectTimeout=8",
         f"{cfg.ssh_user}@{cfg.vm_ip}", "cut -d. -f1 /proc/uptime"],
        capture_output=True, text=True,
    )
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def _provider_detail(cfg: Config, p, state: str) -> str:
    """A parked box may not be startable — some providers gate on stock. Report
    that when the provider can tell us, plainly when it can't."""
    from . import hyperstack
    if not isinstance(p, hyperstack.HyperstackProvider):
        return state
    _, gpu = hyperstack.vm_info(cfg)
    stock = hyperstack.gpu_stock(cfg, gpu) if gpu else None
    raw = hyperstack.vm_state(cfg)
    if raw == "SHUTOFF":
        return ("SHUTOFF — BILLING AT FULL RATE. 'harbor up' to use it, "
                "'harbor down' to park it properly.")
    if stock is None:
        return state
    if stock > 0:
        return f"{state} · {gpu} stock {stock} (startable)"
    return f"{state} · {gpu} stock 0 — NOT currently startable"


def run(cfg: Config) -> int:
    """Exit: 0 all up · 1 something down · 2 provider API unreachable."""
    ok = 0

    if not cfg.manages_vm:
        # Bring-your-own-endpoint: no machine to report on, and no credit to
        # account for. The endpoint and model checks below still apply.
        _say("vm", "not managed (bring-your-own-endpoint)")
        model = model_serving(cfg)
        _say("model", f"serving {model}" if model else "not reachable")
        _say("endpoint", cfg.endpoint_url)
        return 0 if model else 1

    from . import provider as prov
    p = prov.load(cfg)
    box_state = p.state(cfg)
    if box_state == prov.UNKNOWN:
        _say("box", "UNKNOWN — provider unreachable")
        return 2
    if box_state == prov.ACTIVE:
        _say("box", "ACTIVE")
    else:
        # Extras (stock, credit) are Hyperstack-only. Ask, don't assume: a
        # third-party provider implements state/start/stop and nothing else.
        detail = _provider_detail(cfg, p, box_state)
        _say("box", detail)
        ok = 1

    model = model_serving(cfg)
    _say("model", f"serving {model}" if model else "not reachable")
    if not model:
        ok = 1
    elif load := pressure(cfg):
        _say("load", load)

    # A silent hold is how an idle box bills all afternoon — always report it.
    try:
        expiry = state.hold_expiry(cfg)
        if expiry and expiry > time.time():
            hhmm = time.strftime("%H:%M", time.localtime(expiry))
            _say("watchdog", f"HELD until {hhmm} (will not auto-park)")
        elif expiry is not None:
            _say("watchdog", "armed (hold expired)")
        else:
            _say("watchdog", "armed")
    except state.StateUnavailable:
        _say("watchdog", "shared state unreachable (watchdog will abstain)")

    # Credit is a Hyperstack extra, not part of the provider protocol.
    if box_state == prov.ACTIVE and isinstance(p, hyperstack.HyperstackProvider):
        cr = hyperstack.credit(cfg)
        up_s = _vm_uptime_seconds(cfg)
        if up_s is not None:
            accrued = up_s / 3600 * cfg.rate_per_hr
            _say("credit", f"${cr} (batched, lags) · ~${accrued:.2f} this boot")
        else:
            _say("credit", f"${cr} (batched, lags)")

    if ok:
        print("\nnot fully up — 'harbor up' starts the box.")
    return ok
