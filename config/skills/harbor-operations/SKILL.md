---
name: harbor-operations
description: Use when asked about the state of the GPU box, its cost, parking or keeping it awake, or switching the served model — e.g. "what's this costing", "park it", "keep it up", "which model is loaded".
---

# Operating harbor

The box you are served by is a GPU VM rented by the hour that hibernates when
idle. One command manages it; run it with bash and report what it says.

    harbor status            every layer: box · model · load · watchdog · credit
    harbor down              park the box (hibernate; GPU billing stops)
    harbor hold [h|off]      keep the idle watchdog from parking it (default 2h)
    harbor model <name>      switch the served model, then wait for health

## When to use which

- **"How are we doing / is everything up? / what's this costing?"** → `harbor
  status`. It reports each layer apart (so "not working" becomes "the model
  is not reachable"), and the credit line — note the balance is batched, so
  the accrued-this-boot figure is the honest number.
- **"We're done" / end of a session** → offer `harbor down`. Parking is the
  whole reason the box is affordable; left ACTIVE it burns ~$1/hour idle.
- **Starting long multi-stage work** → `harbor hold`, and `harbor hold off`
  when done. A forgotten hold is how an idle box bills all afternoon.

## What you cannot do

You cannot bring the box up. If it is hibernated you are not running, so there
is nobody to ask — the user starts it from their shell with `harbor up`.

## Judgement

Do not park the box because a task finished; the user may have more to do.
Offer, and let them decide. If a hold is active and work has clearly ended, say
so — an unnoticed hold is a silent cost, and `harbor status` reports it.
