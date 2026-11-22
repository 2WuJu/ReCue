"""Host memory readings and conservative estimates for the CPU backend."""
from math import ceil

import psutil

MIB = 1024 ** 2
PROBE_RESERVE_MIB = 768
# Rounded up from ReCue's Windows peak-commit measurements. These are
# estimates for the current CPU profile, not guarantees for other systems.
MEASURED_MODELS = {
    "19fea4b380c3a618ec4723c3eef2eb785ffba0d0538cf43f8f235e7b3b34220f": 1536,
    "d75795ecff3f83b5faa89d1900604ad8c780abd5739fae406de19f23ecd98ad1": 2560,
}


class MemoryPressure(ValueError):
    pass


def available_memory():
    try:
        return psutil.virtual_memory().available // MIB
    except (OSError, psutil.Error):
        return None


def swap_written():
    try:
        return psutil.swap_memory().sout
    except (OSError, psutil.Error):
        return None


def probe_budget(entry):
    return entry.get("required_memory_mib") or (
        ceil(entry["signature"][2] / MIB) + PROBE_RESERVE_MIB
    )


def resource_status(entry, available):
    required = entry.get("required_memory_mib")
    if available is None:
        return "resource_unknown"
    if available < probe_budget(entry):
        return "memory_insufficient"
    if required is None:
        return "resource_unknown"
    if available < required + max(512, required // 4):
        return "below_recommended"
    return "available"
