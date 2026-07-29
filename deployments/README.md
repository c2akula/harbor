# Deployments

One folder per person/machine. A deployment is a filled-in `config.toml` —
identity and knobs, never credentials. Key files stay in
`~/.config/hyperstack/` and `~/.ssh/`; the config only points at them.

Apply yours with:

    ./install.sh <deployment-name>

which syncs `deployments/<name>/config.toml` to `~/.config/harbor/config.toml`,
installs the `harbor` package, and renders the systemd units from the config.
Re-run after editing your deployment — the repo copy is the source of truth.

On a machine with no deployment folder, `harbor init` scaffolds the config
interactively instead.
