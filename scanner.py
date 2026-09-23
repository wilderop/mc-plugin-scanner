#!/usr/bin/env python3
"""Low-priority catalog scan of survival playerdata + region/entity files."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from mc_catalog import (
    Store,
    chunk_data_version,
    harvest_chunk_nbt,
    harvest_player_nbt,
    iter_mca_nbt,
    load_nbt_file,
    write_json_atomic,
)

WORLD_DEFAULT = Path("/mnt/pool/survival/world")
DIMS = (
    "overworld",
    "the_nether",
    "the_end",
    "mirror_overworld",
    "mirror_nether",
)

STOP = False


def _on_stop(signum, _frame):
    global STOP
    STOP = True
    print(f"signal {signum}: finishing current file", flush=True)


class Throttle:
    def __init__(self, max_load: float, region_sleep: float, player_sleep: float):
        self.max_load = max_load
        self.region_sleep = region_sleep
        self.player_sleep = player_sleep

    def headroom(self):
        while not STOP:
            load = os.getloadavg()[0]
            if load <= self.max_load:
                return
            time.sleep(20)

    def after_players(self, n: int):
        if n % 20 == 0:
            self.headroom()
            time.sleep(self.player_sleep)

    def after_region(self):
        self.headroom()
        time.sleep(self.region_sleep)


def iso_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def list_player_files(world: Path) -> list[Path]:
    paths = []
    for rel in ("players/data", "playerdata"):
        d = world / rel
        if d.is_dir():
            paths.extend(sorted(d.glob("*.dat")))
    return paths


def list_mca(world: Path, dim: str, folder: str) -> list[Path]:
    d = world / "dimensions" / "minecraft" / dim / folder
    if not d.is_dir():
        # 1.16-era fallbacks
        if dim == "overworld" and folder == "region":
            d = world / "region"
        elif dim == "the_nether":
            d = world / "DIM-1" / folder
        elif dim == "the_end":
            d = world / "DIM1" / folder
    if not d.is_dir():
        return []
    return sorted(d.glob("r.*.mca"))


def export_public(store: Store, out_dir: Path, progress: dict):
    items = store.export_items()
    catalog = {
        "generated": iso_now(),
        "item_count": len(items),
        "instance_count": sum(i.get("count", 0) for i in items),
        "items": items,
    }
    write_json_atomic(out_dir / "catalog.json", catalog)
    write_json_atomic(out_dir / "progress.json", progress)
    return len(items)


def push_to_site(out_dir: Path, host: str):
    catalog = out_dir / "catalog.json"
    progress = out_dir / "progress.json"
    if not catalog.exists():
        return
    try:
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", host, "mkdir -p /tmp/azpbmd-catalog"],
            check=True,
            timeout=30,
        )
        subprocess.run(
            ["rsync", "-az", str(catalog), str(progress), f"{host}:/tmp/azpbmd-catalog/"],
            check=True,
            timeout=60,
        )
        subprocess.run(
            [
                "ssh", "-o", "BatchMode=yes", host,
                "sudo mkdir -p /var/www/azpbmd/data /tmp/azpbmd-catalog && "
                "sudo cp /tmp/azpbmd-catalog/catalog.json /tmp/azpbmd-catalog/progress.json /var/www/azpbmd/data/ && "
                "sudo chown www-data:www-data /var/www/azpbmd/data/catalog.json /var/www/azpbmd/data/progress.json && "
                "sudo chmod a+r /var/www/azpbmd/data/catalog.json /var/www/azpbmd/data/progress.json",
            ],
            check=True,
            timeout=60,
        )
    except Exception as e:
        print(f"push skipped: {e}", flush=True)


def scan_path(store: Store, path: Path, kind: str) -> int:
    try:
        st = path.stat()
    except OSError as e:
        store.record_file(path, kind, 0, 0, [], str(e))
        return 0
    if store.file_done(path, st.st_mtime, st.st_size):
        return -1
    hits = []
    err = None
    try:
        if kind == "player":
            nbt = load_nbt_file(path)
            if nbt:
                store.note_version(nbt.get("DataVersion"))
                hits = harvest_player_nbt(nbt)
        else:
            for chunk in iter_mca_nbt(path):
                store.note_version(chunk_data_version(chunk) if isinstance(chunk, dict) else None)
                hits.extend(harvest_chunk_nbt(chunk))
    except Exception as e:
        err = type(e).__name__ + ": " + str(e)[:200]
        hits = []
    # merge hits from same file
    merged = {}
    for fp, rec, count in hits:
        if fp in merged:
            merged[fp] = (fp, rec, merged[fp][2] + count)
        else:
            merged[fp] = (fp, rec, count)
    packed = list(merged.values())
    store.record_file(path, kind, st.st_mtime, st.st_size, packed, err)
    return len(packed)


def main():
    nproc = os.cpu_count() or 4
    ap = argparse.ArgumentParser(description="AZPBMD non-vanilla item catalog scanner")
    ap.add_argument("--world", type=Path, default=WORLD_DEFAULT)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "output")
    ap.add_argument("--max-load", type=float, default=max(4.0, nproc * 0.5))
    ap.add_argument("--region-sleep", type=float, default=0.12)
    ap.add_argument("--player-sleep", type=float, default=0.03)
    ap.add_argument("--players-only", action="store_true")
    ap.add_argument("--regions-only", action="store_true")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--push-host", default=os.environ.get("AZPBMD_SSH_HOST", "montreal-vps"))
    ap.add_argument("--export-every", type=int, default=250)
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _on_stop)
    signal.signal(signal.SIGINT, _on_stop)

    world = args.world
    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    lock = out_dir / "scanner.lock"
    if lock.exists():
        try:
            pid = int(lock.read_text().strip())
            os.kill(pid, 0)
            print(f"already running pid {pid}", file=sys.stderr)
            sys.exit(1)
        except (ValueError, OSError, ProcessLookupError):
            pass
    lock.write_text(str(os.getpid()))

    throttle = Throttle(args.max_load, args.region_sleep, args.player_sleep)
    store = Store(out_dir / "catalog.sqlite")

    def progress(phase, done, total, extra=""):
        files_done, unique, instances = store.stats()
        vers = store.version_histogram()
        payload = {
            "phase": phase,
            "updated": iso_now(),
            "files_done": files_done,
            "phase_done": done,
            "phase_total": total,
            "unique_items": unique,
            "instances": instances,
            "paused": False,
            "note": extra or "Reads on-disk NBT as-is. Unvisited chunks and inventories stay on whatever version they were last saved.",
            "nbt_versions": vers,
        }
        return payload

    def maybe_export(phase, done, total, force=False):
        if force or (done and done % args.export_every == 0):
            prog = progress(phase, done, total)
            n = export_public(store, out_dir, prog)
            print(f"export {phase} {done}/{total} unique={n}", flush=True)
            if args.push:
                push_to_site(out_dir, args.push_host)

    try:
        if not args.regions_only:
            players = list_player_files(world)
            print(f"playerdata files: {len(players)}", flush=True)
            done = 0
            last_i = 0
            for i, path in enumerate(players, 1):
                last_i = i
                if STOP:
                    break
                n = scan_path(store, path, "player")
                if n != -1:
                    done += 1
                    if done % 50 == 0:
                        store.commit()
                throttle.after_players(i)
                maybe_export("playerdata", i, len(players))
            store.commit()
            maybe_export("playerdata", last_i, len(players), force=True)

        if not args.players_only and not STOP:
            jobs = []
            for dim in DIMS:
                for folder in ("region", "entities"):
                    files = list_mca(world, dim, folder)
                    jobs.append((f"{dim}/{folder}", files))
            grand = sum(len(f) for _, f in jobs)
            print(f"region/entity files: {grand}", flush=True)
            seen = 0
            for label, files in jobs:
                print(f"start {label} ({len(files)})", flush=True)
                last_i = 0
                for i, path in enumerate(files, 1):
                    last_i = i
                    if STOP:
                        break
                    n = scan_path(store, path, "mca")
                    seen += 1
                    if n != -1 and seen % 10 == 0:
                        store.commit()
                    throttle.after_region()
                    maybe_export(label, i, len(files))
                store.commit()
                maybe_export(label, last_i, len(files), force=True)
                if STOP:
                    break

        store.commit()
        phase = "stopped" if STOP else "complete"
        maybe_export(phase, 1, 1, force=True)
        print(phase, store.stats(), flush=True)
    finally:
        store.close()
        try:
            lock.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
