# Where we stopped, and how to pick it up

GCP credits ran out on 2026-09-14 and billing was disabled on the project, so
the instance became unreachable mid-matrix. Nothing is broken; the work is
paused. Deadline is **Sep 24 AoE**.

## Restore access

1. Re-enable billing on project `project-9c5b6cd4-961c-498d-936`
   (billing account `01A755-C6941C-7DB229`).
2. `gcloud compute instances start chia-orfs-dev --zone us-west1-b`
   The persistent disk should be intact -- suspending billing stops compute but
   does not immediately delete disks. Everything below lives on it.
3. Bring the cluster up and re-install the ported designs, which live in the
   container's ORFS tree and do **not** survive a container recreate:

       cd ~/repo
       yes | chia up examples/openroad_orfs/cluster.yaml
       docker cp ~/cb_designs/src/.      chia-orfs-barsat-0:/OpenROAD-flow-scripts/flow/designs/src/
       docker cp ~/cb_designs/sky130hd/. chia-orfs-barsat-0:/OpenROAD-flow-scripts/flow/designs/sky130hd/

4. Deploy the current code (the VM copy may lag this repo):

       tar czf /tmp/r.tgz chia_openroad examples experiments
       gcloud compute scp /tmp/r.tgz chia-orfs-dev:~/ --zone us-west1-b
       ssh: rm -rf ~/repo/{chia_openroad,examples,experiments} && tar xzf ~/r.tgz -C ~/repo

## First thing to do after restoring

**Copy the orphaned ledgers off the VM before anything else.** These are the
only copies; their numbers are transcribed in `results/transcribed_2026-09-14/`
but the raw data is not:

    ~/results_mf/        multi-fidelity full arm, all 4 designs
    ~/results_final/     the final matrix, 5 of 12 runs
    ~/results_v5/ ~/results_v6/   evidence-table and 10.4 ns sha256 runs
    ~/logs_mf/ ~/logs_final/ ~/logs_v5/ ~/logs_v6/

    tar czf ~/rescue.tgz -C ~ results_mf results_final results_v5 results_v6 logs_mf logs_final
    gcloud compute scp chia-orfs-dev:~/rescue.tgz /tmp/ --zone us-west1-b

## Matrix state: 5 of 12 runs

Four designs x three loop arms, all with the final agent, multi-fidelity
throughout (`--turns 4 --picks 8 --promote 2 --multi-fidelity`).

| arm | flags | cb_aes | cb_picorv32 | cb_sha256 | cb_ethmac |
|---|---|---|---|---|---|
| full | (none) | done | done | done | done |
| agent | `--no-tools --no-consult` | turn 1/4 | done | turn 4/4 | turn 1/4 |
| screen | `--no-agent` | not started | not started | not started | not started |

The orchestrator is `~/final_matrix.sh` (logs to `~/final_matrix.log`). It runs
`full`, then `agent`, then `screen`. Restarting it will redo the `agent` arm
from scratch; edit it to skip straight to what is missing, or run the arms by
hand with the flags above.

Render the table any time with `python3 ~/final_table.py`
(source: `experiments/final_table.py`).

## Clock periods (retuned so each design starts near closure)

    cb_aes 6.5 ns   cb_picorv32 4.5 ns   cb_sha256 10.4 ns   cb_ethmac 6.5 ns

cb_sha256 was moved from 10.0 to 10.4 partway through. At 10.0 the design sits
0.58 ns from closure and the knobs have no leverage -- 3 of 89 builds ever beat
the default. Do not compare 10.0 ns numbers with 10.4 ns ones.

## Machine shape

`n2-standard-32`. The project is capped at **32 vCPUs globally**
(`CPUS_ALL_REGIONS`), so a bigger machine is not available in any zone;
throughput comes from concurrency. `cluster.yaml` declares 16 `orfs` slots,
giving 2 threads per build, and the stage timeout scales inversely with threads
(3 h at 2 threads) -- at 90 min, healthy routes were being killed.

## What is left for the paper

* Finish the matrix: 7 runs (`agent` on aes/sha256/ethmac, `screen` on all four).
* Seeds. Everything is n=1 and the measured noise floor is ~0.06 ns, so
  differences smaller than that are not claimable.
* `paper/main.tex` has Context, the loop (4 subsections), trust boundary and
  surrogate socket written. Results, Limitations, Contributions and the
  Abstract are not.

## Budget

About $30 per four-design run at current rates. Finishing the matrix needs
roughly 2 runs' worth; seeds on the two arms that matter would be 4 more.
