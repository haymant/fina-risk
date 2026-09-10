"""Runtime host CPU collection for benchmark and health reporting.

Reads portable sources first (os / platform), then falls back to parsing
/proc/cpuinfo and lscpu on Linux. In a serverless context this reports the
worker's host CPU, not the user's client machine. Absence of any source is
honest: the field is omitted rather than fabricated.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from collections import OrderedDict
from typing import Any


def _parse_cpuinfo() -> dict[str, Any]:
    try:
        with open("/proc/cpuinfo") as stream:
            lines = [line.strip() for line in stream if line.strip()]
    except OSError:
        return {}
    cpu: OrderedDict[str, dict[str, str]] = OrderedDict()
    current: dict[str, str] = {}
    for line in lines:
        if line.startswith("processor"):
            if current:
                cpu[current.get("processor", str(len(cpu)))] = current
            current = {"processor": line.split(":", 1)[1].strip()}
        elif ":" in line:
            key, _, value = line.partition(":")
            current[key.strip()] = value.strip()
    if current:
        cpu[current.get("processor", str(len(cpu)))] = current
    cores_by_physical: dict[str, set[str]] = {}
    for entry in cpu.values():
        cores_by_physical.setdefault(str(entry.get("core id", entry.get("physical id", "0"))), set())
        cores_by_physical[str(entry.get("core id", entry.get("physical id", "0")))].add(
            entry.get("physical id", "0")
        )
    first = next(iter(cpu.values()), {})
    return {
        "model_name": first.get("model name") or first.get("Hardware", ""),
        "logical_processors": len(cpu),
        "physical_cores": len(cores_by_physical),
        "vendor_id": first.get("vendor_id", ""),
        "cpu_family": first.get("cpu family", ""),
        "model": first.get("model", ""),
        "stepping": first.get("stepping", ""),
        "microcode": first.get("microcode", ""),
        "cpu_mhz": first.get("cpu MHz", ""),
        "cache_size": first.get("cache size", ""),
        "bogomips": first.get("bogomips", ""),
        "flags": first.get("flags", "").split() if first.get("flags") else [],
        "hypervisor_guest": first.get("hypervisor", ""),
    }


def _run_lscpu() -> dict[str, Any]:
    binary = shutil.which("lscpu")
    if not binary:
        return {}
    try:
        result = subprocess.run(
            [binary, "-J"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    fields = {
        field: entry.get("data")
        for entry in payload.get("lscpu", [])
        for field in [entry.get("field")]
    }
    return {key: value for key, value in fields.items() if value is not None}


def collect_cpu_info() -> dict[str, Any]:
    """Return a best-effort host CPU fingerprint at runtime."""
    info: dict[str, Any] = {
        "processor_architecture": platform.machine(),
        "processor": platform.processor() or "",
        "system": platform.system(),
        "logical_cpus_available": os.cpu_count(),
        "python_build": platform.python_compiler(),
        "platform_detail": platform.platform(),
    }
    lscpu = _run_lscpu()
    if lscpu:
        info["lscpu_model"] = lscpu.get("Model name")
        info["lscpu_architecture"] = lscpu.get("Architecture")
        info["lscpu_scalable_model"] = lscpu.get("Scalable Model")
        info["lscpu_cpus"] = lscpu.get("CPU(s)")
        info["lscpu_online_cpus"] = lscpu.get("On-line CPU(s) list")
        info["lscpu_cores"] = lscpu.get("Core(s) per socket")
        info["lscpu_sockets"] = lscpu.get("Socket(s)")
        info["lscpu_threads"] = lscpu.get("Thread(s) per core")
        info["lscpu_numa_nodes"] = lscpu.get("NUMA node(s)")
        info["lscpu_model_family"] = lscpu.get("Model name") or lscpu.get("Model family")
        info["lscpu_vendor_id"] = lscpu.get("Vendor ID")
        info["lscpu_frequency"] = lscpu.get("CPU max MHz")
        info["lscpu_virtualization"] = lscpu.get("Virtualization")
        info["lscpu_hypervisor"] = lscpu.get("Hypervisor vendor")
    cpuinfo = _parse_cpuinfo()
    if cpuinfo.get("model_name"):
        info["model_name"] = cpuinfo["model_name"]
    if cpuinfo.get("logical_processors"):
        info["logical_processors"] = cpuinfo["logical_processors"]
    if cpuinfo.get("physical_cores"):
        info["physical_cores"] = cpuinfo["physical_cores"]
    for key in ("vendor_id", "cpu_mhz", "cache_size", "flags", "hypervisor_guest"):
        if cpuinfo.get(key):
            info[key] = cpuinfo[key]
    return info