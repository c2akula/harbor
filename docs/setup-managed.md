# Setup: managed modes (`hyperstack` / `provider`)

harbor manages the GPU box: resume, park, model switching, idle watchdog,
team access, and delete-and-recreate from a persistent volume. The box lives
on a private WireGuard network that harbor builds itself — there is no VPN
account, no sign-in, and nothing network-shaped to configure.

## Prerequisites

| Thing | Why |
|---|---|
| Provider account + API key file | lifecycle calls |
| SSH keypair (registered with the provider) | harbor drives the box over SSH |
| A persistent volume (~64 GB) | weights, venv, keys, network identity — the system lives here |
| `wireguard-tools` on your machine | your end of the private link |

Image rule: vLLM needs driver ≥ 580 / CUDA 13. Pin the newest image your
provider has; the box upgrades the driver in place when the image is older.

## First-time provisioning (once)

1. Create the volume; put its id and your flavor/image/keypair choices in
   the config's redeploy intent (`[vm] flavors / image / keypair / volume`)
   — see `deployments/config.toml.example`.
2. `./install.sh`, then `harbor init` (pick `hyperstack` or `provider`) —
   the wizard validates as you answer. There are no network questions.
3. `harbor up` — creates the box, raises its network, links your machine in
   (one sudo prompt), waits for serving. First-ever boot also needs
   `cloud/bootstrap.sh <model-api-key>` on the box to seed the volume.

## Daily life

```
harbor up          # resume (or recreate — see below), wait for serving
harbor down        # hibernate: GPU billing stops, resume is fast
harbor status      # box · model · load · watchdog · credit
harbor hold 3      # keep the watchdog off during long work (3h)
```

## Release and recreate

```
harbor down --release   # DELETE the VM, keep the volume
harbor up               # recreate: first flavor with stock, mount volume,
                        # raise the network, fix driver, serve — unattended
```

Identity is the **name** (`[vm] name`); provider ids change underneath and
config never needs editing. The box's network identity (keys, peers) rides
the volume, so a recreated box is the same box to everyone already joined —
but its **public address changes**: run `harbor share` for a fresh message
and have teammates rerun their join command.

If a recreate fails partway, `up` says which VM exists and bills, and the
next `up` continues against it rather than creating another.

## Teammates

One command for you, one paste for them — see [setup-team.md](setup-team.md).
