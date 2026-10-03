import asyncio
import collections
import json
import os
import shutil
import subprocess
import time as _time
import psutil
import redis.asyncio as redis

from pathlib import Path
from fastapi import APIRouter, Depends

from server.auth import current_superuser
from server.models import User

server_status_router = APIRouter()

# ── Redis (shared across workers) ──
REDIS_URL        = os.getenv("REDIS_URL", "redis://127.0.0.1:6379")
REDIS_HIST_KEY   = "stats:history"    # ZSET: score=timestamp_ms, member=json entry
REDIS_LEADER_KEY = "stats:leader"     # STRING with TTL — held by the collector worker
_LEADER_TTL_S    = 6                  # lock expires if not renewed within this window
_LEADER_RENEW_S  = 2                  # renewal interval (must be < _LEADER_TTL_S)

_redis_stats: redis.Redis | None = None

# ── Background stats collection ──
_SENSITIVE_DIR = Path(__file__).resolve().parent / "sensitive"
_HISTORY_FILE = _SENSITIVE_DIR / "stats_history.json"
_MAX_ENTRIES = 80_000
_MAX_FILE_BYTES = 5 * 1024 ** 3  # 5 GB rolling cap on the JSON file
_COLLECT_INTERVAL_S = 2.0
_PERSIST_INTERVAL_S = 60.0

_stats_history: collections.deque = collections.deque(maxlen=_MAX_ENTRIES)
_history_lock = asyncio.Lock()

# ── /api/system-stats  ──  admin-only live machine metrics ──
def _read_cpu_temp() -> float | None:
    """Read CPU temperature.

    Linux : /sys/class/thermal/thermal_zone*/temp  then psutil sensors.
    """
    # --- Linux: scan all thermal zones, pick the hottest ---
    tz_dir = Path("/sys/class/thermal")
    if tz_dir.is_dir():
        best = None
        for tz in sorted(tz_dir.glob("thermal_zone*/temp")):
            try:
                val = int(tz.read_text().strip()) / 1000.0
                if best is None or val > best:
                    best = val
            except (ValueError, OSError):
                continue
        if best is not None:
            return best

    return None


def _read_gpu_nvidia() -> dict:
    """Query nvidia-smi for GPU utilisation, temperature, and VRAM."""
    result: dict = {
        "gpu_percent": None, "gpu_temp_c": None, "gpu_name": None,
        "vram_used_mb": None, "vram_total_mb": None, "vram_percent": None,
    }
    if not shutil.which("nvidia-smi"):
        return result
    try:
        out = subprocess.check_output(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,temperature.gpu,name,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            timeout=4,
            text=True,
        ).strip()
        parts = [p.strip() for p in out.split(",")]
        if len(parts) >= 5:
            result["gpu_percent"] = float(parts[0])
            result["gpu_temp_c"] = float(parts[1])
            result["gpu_name"] = parts[2]
            used_mb = float(parts[3])
            total_mb = float(parts[4])
            result["vram_used_mb"] = used_mb
            result["vram_total_mb"] = total_mb
            result["vram_percent"] = round(used_mb / total_mb * 100, 1) if total_mb else None
    except Exception:
        pass
    return result


@server_status_router.get("/api/system-stats")
async def system_stats(_: User = Depends(current_superuser)):
    """Return current machine health metrics (admin only)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _collect_stats_sync)


def _collect_stats_sync() -> dict:
    """Collect system stats synchronously — intended to run in a thread executor."""
    gpu = _read_gpu_nvidia()
    cpu_temp = _read_cpu_temp()
    mem = psutil.virtual_memory()
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "total_gb": round(usage.total / (1024 ** 3), 2),
                "used_gb": round(usage.used / (1024 ** 3), 2),
                "free_gb": round(usage.free / (1024 ** 3), 2),
                "percent": usage.percent,
            })
        except (PermissionError, OSError):
            continue
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.3),
        "cpu_temp_c": cpu_temp,
        "ram_total_gb": round(mem.total / (1024 ** 3), 2),
        "ram_used_gb": round(mem.used / (1024 ** 3), 2),
        "ram_percent": mem.percent,
        "gpu_percent": gpu["gpu_percent"],
        "gpu_temp_c": gpu["gpu_temp_c"],
        "gpu_name": gpu["gpu_name"],
        "vram_used_mb": gpu["vram_used_mb"],
        "vram_total_mb": gpu["vram_total_mb"],
        "vram_percent": gpu["vram_percent"],
        "disks": disks,
    }


def _raw_to_entry(raw: dict, t_ms: float) -> dict:
    """Convert a raw stats dict to the compact entry format used by the browser's localStorage."""
    entry: dict = {
        "t": t_ms,
        "cpu": raw.get("cpu_percent"),
        "ram": raw.get("ram_percent"),
        "ramU": raw.get("ram_used_gb"),
        "ramT": raw.get("ram_total_gb"),
        "gpu": raw.get("gpu_percent"),
        "vram": raw.get("vram_percent"),
        "cpuT": raw.get("cpu_temp_c"),
        "gpuT": raw.get("gpu_temp_c"),
    }
    for dk in (raw.get("disks") or []):
        entry["d_" + dk["mountpoint"]] = dk["percent"]
    return entry


async def _load_persisted_history() -> None:
    """Load previously persisted history from disk into the in-memory deque on startup.
    In Redis mode also seeds the ZSET if it is empty.
    """
    if not _HISTORY_FILE.exists():
        return
    try:
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, lambda: _HISTORY_FILE.read_text("utf-8"))
        entries = []
        for l in text.splitlines():
            if not l.strip():
                continue
            parsed = json.loads(l)
            if isinstance(parsed, list):
                entries.extend(parsed)
            elif isinstance(parsed, dict):
                entries.append(parsed)
        entries.sort(key=lambda e: e.get("t", 0))
        async with _history_lock:
            for entry in entries[-_MAX_ENTRIES:]:
                _stats_history.append(entry)
        # Seed Redis ZSET if empty
        if _redis_stats is not None:
            try:
                if await _redis_stats.zcard(REDIS_HIST_KEY) == 0 and entries:
                    mapping = {
                        json.dumps(e, separators=(",", ":")): e["t"]
                        for e in entries[-_MAX_ENTRIES:]
                    }
                    await _redis_stats.zadd(REDIS_HIST_KEY, mapping)
            except Exception:
                pass
    except Exception:
        pass  # corrupt or missing — start fresh


def _trim_to_size(entries: list) -> list:
    """Drop oldest entries until the serialized payload fits within _MAX_FILE_BYTES."""
    lines = [json.dumps(e, separators=(",", ":")) for e in entries]
    size = sum(len(l.encode("utf-8")) + 1 for l in lines)  # +1 per newline
    i = 0
    while i < len(lines) and size > _MAX_FILE_BYTES:
        size -= len(lines[i].encode("utf-8")) + 1
        i += 1
    return entries[i:]


async def persist_stats_history() -> None:
    """Flush history to disk. In Redis mode reads the shared ZSET so all workers
    write consistent data (last-writer-wins is fine — it's the same content).
    """
    try:
        if _redis_stats is not None:
            raw = await _redis_stats.zrange(REDIS_HIST_KEY, 0, -1)
            snapshot = [json.loads(e) for e in raw]
        else:
            async with _history_lock:
                snapshot = list(_stats_history)
    except Exception:
        async with _history_lock:
            snapshot = list(_stats_history)

    if not snapshot:
        return
    try:
        loop = asyncio.get_running_loop()
        trimmed = await loop.run_in_executor(None, _trim_to_size, snapshot)
        if _redis_stats is None and len(trimmed) < len(snapshot):
            async with _history_lock:
                _stats_history.clear()
                for e in trimmed:
                    _stats_history.append(e)
        lines = [json.dumps(e, separators=(",", ":")) for e in trimmed]
        payload = "\n".join(lines) + "\n"
        _SENSITIVE_DIR.mkdir(parents=True, exist_ok=True)
        await loop.run_in_executor(
            None, lambda: _HISTORY_FILE.write_text(payload, "utf-8")
        )
    except Exception:
        pass


async def stats_collector_task() -> None:
    """Background task: sample system stats every 2 s and persist to disk every 60 s.

    Multi-worker safety: uses Redis leader-election so exactly one worker collects
    at a time.  If Redis is unavailable (dev / single-worker), all code paths fall
    back to the original in-process behaviour.
    """
    is_leader = False
    renew_task: asyncio.Task | None = None

    # ── Leader election (Redis mode) ──
    if _redis_stats is not None:
        while True:
            try:
                acquired = await _redis_stats.set(
                    REDIS_LEADER_KEY, "1", nx=True, ex=_LEADER_TTL_S
                )
            except Exception:
                acquired = None
            if acquired:
                is_leader = True
                break
            # Not the leader — wait one TTL window and retry so we take over
            # quickly if the current leader's worker dies.
            await asyncio.sleep(_LEADER_TTL_S)

        # Periodically renew the leadership lease
        async def _renew_leader() -> None:
            while True:
                await asyncio.sleep(_LEADER_RENEW_S)
                try:
                    await _redis_stats.expire(REDIS_LEADER_KEY, _LEADER_TTL_S)
                except Exception:
                    pass

        renew_task = asyncio.create_task(_renew_leader())
    else:
        is_leader = True  # single-worker / no Redis — always collect

    await _load_persisted_history()
    loop = asyncio.get_running_loop()
    last_persist = _time.monotonic() - _PERSIST_INTERVAL_S

    try:
        while True:
            try:
                raw = await loop.run_in_executor(None, _collect_stats_sync)
                t_ms = _time.time() * 1000
                entry = _raw_to_entry(raw, t_ms)
                async with _history_lock:
                    _stats_history.append(entry)
                if _redis_stats is not None:
                    try:
                        entry_json = json.dumps(entry, separators=(",", ":"))
                        await _redis_stats.zadd(REDIS_HIST_KEY, {entry_json: t_ms})
                        excess = await _redis_stats.zcard(REDIS_HIST_KEY) - _MAX_ENTRIES
                        if excess > 0:
                            await _redis_stats.zremrangebyrank(REDIS_HIST_KEY, 0, excess - 1)
                    except Exception:
                        pass
            except Exception:
                pass

            now = _time.monotonic()
            if now - last_persist >= _PERSIST_INTERVAL_S:
                await persist_stats_history()
                last_persist = now

            await asyncio.sleep(_COLLECT_INTERVAL_S)
    finally:
        if renew_task is not None:
            renew_task.cancel()
            try:
                await renew_task
            except asyncio.CancelledError:
                pass
        if _redis_stats is not None and is_leader:
            try:
                await _redis_stats.delete(REDIS_LEADER_KEY)
            except Exception:
                pass


@server_status_router.get("/api/system-history")
async def system_history(_: User = Depends(current_superuser)):
    """Return the full server-side stats history (admin only)."""
    if _redis_stats is not None:
        try:
            raw = await _redis_stats.zrange(REDIS_HIST_KEY, 0, -1)
            return [json.loads(e) for e in raw]
        except Exception:
            pass
    async with _history_lock:
        return list(_stats_history)


@server_status_router.delete("/api/system-history")
async def clear_system_history(_: User = Depends(current_superuser)):
    """Clear the in-memory stats history and delete the persisted JSON file (admin only)."""
    async with _history_lock:
        _stats_history.clear()
    if _redis_stats is not None:
        try:
            await _redis_stats.delete(REDIS_HIST_KEY)
        except Exception:
            pass
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: _HISTORY_FILE.unlink(missing_ok=True))
    except Exception:
        pass
    asyncio.create_task(_collect_and_persist_once())
    return {"ok": True, "message": "Stats history cleared."}


async def _collect_and_persist_once() -> None:
    """Collect one stats snapshot and persist it to disk immediately."""
    try:
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(None, _collect_stats_sync)
        t_ms = _time.time() * 1000
        entry = _raw_to_entry(raw, t_ms)
        async with _history_lock:
            _stats_history.append(entry)
        if _redis_stats is not None:
            try:
                entry_json = json.dumps(entry, separators=(",", ":"))
                await _redis_stats.zadd(REDIS_HIST_KEY, {entry_json: t_ms})
            except Exception:
                pass
        await persist_stats_history()
    except Exception:
        pass


async def stats_startup() -> None:
    """Connect to Redis. Falls back silently to single-worker in-process mode."""
    global _redis_stats
    try:
        client = redis.Redis.from_url(REDIS_URL, decode_responses=False)
        await client.ping()
        _redis_stats = client
    except Exception:
        _redis_stats = None


async def stats_shutdown() -> None:
    """Close the Redis connection cleanly."""
    global _redis_stats
    if _redis_stats is not None:
        try:
            await _redis_stats.aclose()
        except Exception:
            pass
        _redis_stats = None