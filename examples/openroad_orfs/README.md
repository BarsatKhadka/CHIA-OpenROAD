# openroad_orfs — ORFS as a CHIA backend

Minimal cluster and job proving the machinery end to end: a CHIA cluster brings
up the `chia-orfs` container, starts a Ray worker inside it, and a driver on the
head dispatches ORFS stages to it.

## Bring it up

```bash
chia up -y cluster.yaml          # -y matters: it prompts "Proceed? [y/N]"
chia status --chia-cluster cluster.yaml
python cluster_job.py
chia down -y cluster.yaml
```

Expected from `chia status`:

```
Total Usage:
 0.0/8.0 CPU
 0.0/4.0 orfs      <- the custom resource run_stage asks for
```

## Three things that are easy to get wrong

**`pull_before_run: False`.** The image is built on the machine, not pushed to a
registry. Leaving the default (`True`) makes CHIA try to pull `chia-orfs:latest`
from Docker Hub, which does not exist.

**`--shm-size=4g` in `run_options`.** Ray's object store lives in `/dev/shm`, and
Docker's 64 MB default makes the raylet fail to register with a bare
`IOError: ... End of file`. CHIA's own `docker.py` passes `--shm-size=8g` for
this reason.

**`py_modules` in `runtime_env`.** The container has chia and ORFS but not our
code. Shipping `chia_openroad` with the job (0.11 MiB) beats baking it into an
8 GB image — code changes reach workers without a rebuild.

## Head placement

The head runs on the VM, not a laptop. CHIA runs the head wherever `chia up` is
invoked, and Ray does not support mixing OS/architecture across a cluster — an
arm64 macOS head with linux/amd64 workers is untested territory we have no
reason to enter. The head needs conda + chia on the *host*, outside the
container, which is why `head_start_ray_commands` uses absolute paths.

## Not done here

- **No volume mount**, so a candidate's WORK_HOME lives inside the container and
  dies with it. Fine for a smoke test; real runs need a bind mount in
  `run_options` (or artifacts collected off the worker via `collect_fs`).
- **One worker.** Scaling means raising `num_workers` and listing more IPs, or
  adding a `gcp_nodes:` section so CHIA provisions them. Deferred until the
  medium design gives us a memory figure to size against — chameleon's DRC alone
  peaked at 9.2 GB.
