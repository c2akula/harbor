# Extending harbor to your provider

harbor's lifecycle speaks a small protocol; Hyperstack is just the built-in
implementation. Bring your own by pointing config at a class:

```toml
[vm]
provider = "mycorp.harbor:GCPProvider"
```

The module only has to be importable where harbor runs — no packaging.

## The required protocol

```python
class GCPProvider:
    def state(self, cfg) -> str: ...   # ACTIVE | OFF | TRANSITIONING | UNKNOWN | ABSENT
    def start(self, cfg) -> None: ...  # revive an OFF box
    def stop(self, cfg) -> None: ...   # park as cheaply as the provider allows
```

Rules:

- Map your vendor's states onto the five above. Keep vendor quirks *inside*
  the implementation (Hyperstack has two off-states with different billing —
  harbor never needs to know).
- Raise `harbor.provider.ProviderError` with a message a human can act on;
  harbor prints it verbatim.
- `UNKNOWN` means harbor refuses to act. `ABSENT` means no machine exists
  under the configured name.

## The optional redeploy capability

Implement these three and `harbor up` can recreate a deleted box from the
weights volume, and `harbor down --release` can delete it:

```python
    def stock(self, cfg, flavor) -> int: ...          # units available
    def create(self, cfg, flavor, user_data) -> int:  # new VM id
    def destroy(self, cfg) -> None: ...
    def public_ip(self, cfg) -> str: ...              # '' until assigned
```

- `create` receives a cloud-init document (network hub, volume mount,
  driver check) and must attach the weights volume before returning.
- Without these methods, nothing changes: `up` on an ABSENT box refuses
  plainly, and `--release` falls back to parking.

## What harbor guarantees your implementation

- One provider call at a time per command — no concurrency to defend against.
- A start that gets dropped is re-issued during the wait; make `start`
  idempotent.
- Nothing durable may live on the VM's own disk — harbor treats the weights
  volume as the system. Design `create`/`destroy` accordingly.

## Serving prerequisite (managed modes)

harbor provisions vLLM, which requires CUDA 13 / driver >= 580. That is a
requirement on the image you configure, not something the protocol
negotiates — pick the image, and bootstrap upgrades the driver in place when
the image is older.

The oracle escalation protocol is documented in `config/ORACLE.md`.
