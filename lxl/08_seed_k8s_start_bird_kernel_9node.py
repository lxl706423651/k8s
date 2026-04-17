#!/usr/bin/env python3
from __future__ import annotations

from _seed_runtime_9node import bootstrap

bootstrap()

from seed_k8s_start_bird_kernel import main

if __name__ == "__main__":
    raise SystemExit(main())
