# Setup: managed modes (`hyperstack` / `provider`)

harbor manages the GPU box: resume, park, model switching, idle watchdog,
per-user keys, and delete-and-recreate from a persistent volume.

## Prerequisites

| Thing | Why |
|---|---|
| Provider account + API key file | lifecycle calls |
| SSH keypair (registered with the provider) | harbor drives the box over SSH |
| Tailscale on your machine + an auth key file | the box serves on the tailnet only |
| A persistent volume (~64 GB) | weights, venv, keys — the system lives here |

Auth key rules: **reusable**, **not ephemeral** (a hibernated box goes
offline nightly; ephemeral nodes would be deleted), ideally tagged (e.g.
`tag:llm`) so the node never needs key re-auth.

Image rule: vLLM needs driver ≥ 580 / CUDA 13. Pin the newest image your
provider has; bootstrap upgrades the driver in place when the image is older.

## First-time provisioning (once)

1. Create the volume and a VM with it attached; join the VM to your tailnet.
2. On the VM: `cloud/bootstrap.sh <model-api-key>` — installs vLLM onto the
   volume, downloads the checkpoint, seeds the serving unit.
3. On your machine: `./install.sh`, then `harbor init` (pick `hyperstack`
   or `provider`) — the wizard validates as you answer.
4. Fill the redeploy intent in your config (`[vm] flavors / image / keypair /
   volume`) — see `deployments/config.toml.example`.

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
harbor up               # recreate: first flavor with stock, join tailnet,
                        # mount volume, fix driver, serve — unattended
```

Identity is the **name** (`[vm] name` = VM name = tailnet hostname); provider
ids change underneath and config never needs editing. A stock outage on your
flavor becomes a fallback to the next one in `[vm] flavors`.

If a recreate fails partway, `up` says which VM exists and bills, and the
next `up` continues against it rather than creating another.

## Teammates

```
harbor keys add alice     # prints her token ONCE; serving re-renders
harbor keys revoke alice  # access ends at the next render
```

Each teammate: joins the tailnet, installs harbor in `endpoint` mode
pointing at `http://<box-name>:8080`, puts their token in their
`model_key_file`. Done — see [operating.md](operating.md) for fairness and
capacity expectations.
