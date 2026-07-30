"""The seam between harbor and whoever owns the machine.

harbor manages a box's lifecycle and cost; it does not care whose cloud it is.
This is the smallest surface that supports that: state, start, stop. Anything
vendor-specific — hibernate versus shutoff, stock gambles, credit reporting,
snapshots — stays *inside* an implementation rather than being flattened into
a lowest-common-denominator abstraction that lies about the differences.

To support another provider, subclass Provider and point config at it:

    [vm]
    provider = "mycorp.harbor:GCPProvider"

Prerequisite for modes where harbor provisions the box: harbor serves vLLM,
which needs CUDA 13 / driver >= 580. That is a requirement on the image you
give it, not something this interface negotiates.
"""
from __future__ import annotations

import importlib
from typing import Protocol, runtime_checkable

from .config import Config

# Normalised states. Providers map their own vocabulary onto these so harbor's
# logic never names one vendor's states (Hyperstack alone has two distinct
# "off" states with different endpoints AND different billing).
ACTIVE = "ACTIVE"                # running and usable
OFF = "OFF"                      # stopped/hibernated — start() will revive it
TRANSITIONING = "TRANSITIONING"  # mid-change; wait rather than act
UNKNOWN = "UNKNOWN"              # cannot tell; callers must not act on this
ABSENT = "ABSENT"                # no machine exists under the configured name


def can_redeploy(p: "Provider") -> bool:
    """Redeploy (create/destroy/stock/public_ip) is an OPTIONAL capability: a
    provider without it keeps park-and-resume semantics, and `harbor up` on an
    ABSENT box refuses instead of creating. Not part of the required protocol —
    optionality here beats forcing every provider to lie about creation."""
    return all(callable(getattr(p, m, None))
               for m in ("create", "destroy", "stock", "public_ip"))


@runtime_checkable
class Provider(Protocol):
    """Implement these three. Raise ProviderError with a message a human can
    act on — harbor surfaces it verbatim."""

    def state(self, cfg: Config) -> str:
        """One of ACTIVE / OFF / TRANSITIONING / UNKNOWN."""

    def start(self, cfg: Config) -> None:
        """Make the box usable. May be slow; harbor polls state() afterwards."""

    def stop(self, cfg: Config) -> None:
        """Park it as cheaply as the provider allows. Whether that is hibernate,
        stop, or delete-and-keep-a-volume is the implementation's business —
        but it should be the option that stops compute billing."""


class ProviderError(RuntimeError):
    """Carries a message harbor shows the user unchanged."""


def load(cfg: Config) -> Provider:
    """Resolve the configured provider. Default is the built-in Hyperstack one;
    anything else is an import path, so a team's provider only has to be
    importable, not packaged."""
    spec = (cfg.provider or "hyperstack").strip()
    if spec == "hyperstack":
        from .hyperstack import HyperstackProvider
        return HyperstackProvider()
    if ":" not in spec:
        raise ProviderError(
            f"provider '{spec}' is not recognised. Use 'hyperstack', or an "
            "import path like 'mycorp.harbor:GCPProvider'.")
    module_name, _, attr = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise ProviderError(
            f"cannot import provider module '{module_name}': {e}. It must be "
            "importable from where harbor runs.") from e
    try:
        return getattr(module, attr)()
    except AttributeError as e:
        raise ProviderError(
            f"module '{module_name}' has no '{attr}'") from e
