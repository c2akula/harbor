# Setup: your team on one box

One person (the **operator**) rents and manages the GPU box. Everyone else
(**teammates**) uses it. Teammates never touch the cloud account, SSH, or
the box's lifecycle — and never need an account with anyone.

## Joining (teammate)

You received a message from your operator. Run the command in it:

```
curl -fsSL https://raw.githubusercontent.com/c2akula/harbor/master/install.sh | sh -s -- --join '<blob>'
```

That's the whole setup. It installs harbor, raises a WireGuard link that
reaches **only** the model box (one sudo prompt), writes your config, and
sets up Crush. Then:

```
crush run "hello"
```

What the link is: a private tunnel between your machine and the box.
Nothing else on your machine is exposed — not to the operator, not to other
teammates, not to the internet. The tunnel literally has no route anywhere
but the box.

If something stalls, rerun the same command — every step checks before it
acts, so an aborted join continues where it stopped.

### When the model doesn't answer

`connection refused` almost always means the box is parked. Only the
operator can wake it — ask them. It is not your setup.

If the operator rebuilt the box, your old link points at a dead address:
ask for a fresh message and rerun the join command.

## Sharing (operator)

```
harbor share
```

Prints the message above, blob included — paste it to your team. It works
offline (everything in it is cached from your last `harbor up`) and it is
idempotent: the same message serves every teammate, present and future.

**The message is access.** The blob carries the team key; whoever holds it
can use the box. Send it the way you'd send a password.

### Who's connected

```
harbor peers            # devices on the box's network
```

### When someone leaves (or the message leaks)

```
harbor peers remove <address>   # cut their tunnel
harbor keys rotate              # kill the old share message everywhere
harbor share                    # fresh message for the people who remain
```

Remaining teammates rerun the join command from the new message — one
paste, a few seconds.

## The trade this design makes

Everyone shares one team key, so revoking one person means rotating everyone
— cheap, because rejoining is one command. If you want per-person revocation
instead, issue individual keys (`harbor keys add alice`) and send personal
messages; both kinds work side by side.

## Why there's no "just open a port" mode

Exposing the model server directly to the internet would make its safety
depend on TLS configs and key handling being right forever. The private
network makes it depend on cryptography instead: the box's public address
answers to nothing but correctly-keyed WireGuard packets — to everyone
else it looks like nothing is there.
