# Agent v3: explore/refine split adapted to headroom

v2 gave every later turn a fixed half of its budget to refining the leader.
That helped where the default was already near-optimal and hurt where the
space was rich:

| design | headroom | v1 -> v2 best | v1 -> v2 median |
|---|---|---|---|
| cb_picorv32 | none (default -0.2352, best reachable ~-0.2351) | -0.2878 -> -0.2351 | -0.3723 -> -0.2769 |
| cb_ethmac | ample (default +0.1106, v1 reached +0.3703) | +0.3703 -> +0.2882 | +0.2437 -> +0.1096 |

Refining one or two knobs off the leader cannot travel as far as exploration,
so on a design with room to move it confines the search.

v3 makes the split conditional on whether the best result so far beats the
default. Behind it, half the turn refines. Ahead of it, one candidate
consolidates and the rest explore.

## Result

```
cb_picorv32   v1 -0.2878  v2 -0.2351  v3 -0.2352     progression worsening -> improving -> improving
cb_ethmac     v1 +0.3703  v2 +0.2882  v3 +0.4112     best of all three, 0.30 ns above default
```

v3 recovers what v2 cost on cb_ethmac and then exceeds v1, while keeping the
convergence v2 bought on cb_picorv32.

## Caveat

One run per configuration. The v1 -> v2 change on cb_picorv32 (worsening to
improving, median +0.095) is large and has a mechanism. Differences of ~0.05
between v2 and v3 on the same design are not separable from run-to-run
stochasticity at this sample size -- v3 and v2 allocate identically on
cb_picorv32, yet their medians differ by 0.057.
