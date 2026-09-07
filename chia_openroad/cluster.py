"""Connecting to the cluster the same way whether run directly or as a CHIA job.

``chia job submit`` (which CHIA forwards to Ray) uploads a working directory and
propagates it to every task. Passing ``py_modules`` on top of that re-pickles an
already-shipped module and fails before any work starts::

    TypeError: cannot pickle 'module' object

Run the same script directly on the head and the opposite is true: nothing has
shipped the code, so workers cannot import it. Both are correct in their own
context, so the script has to know which one it is in.
"""
from __future__ import annotations

import os


#: Ray sets this in a submitted job's entrypoint process to carry the job's
#: runtime_env. Verified by printing the environment from inside a job — the
#: obvious guesses (RAY_JOB_ID, RAY_JOB_SUBMISSION_ID) are NOT set there, and
#: guessing wrong fails the same way as not checking at all.
_JOB_MARKER = "RAY_JOB_CONFIG_JSON_ENV_VAR"


def in_job() -> bool:
    """True when running under `chia/ray job submit`, which ships the code itself."""
    return bool(os.environ.get(_JOB_MARKER))


def init(ray, module=None):
    """ray.init for either context. Returns the resolved runtime_env, for logging."""
    if in_job():
        ray.init(address="auto")
        return {}
    env = {"py_modules": [module]} if module is not None else {}
    ray.init(address="auto", runtime_env=env)
    return env
