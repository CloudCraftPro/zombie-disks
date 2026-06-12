#!/usr/bin/env python3
"""
zombie_disks.py — find and safely remove orphaned VMDK files on vSphere datastores.

WORKFLOW (two-pass, recoverable)
================================

  1. mark   — list every .vmdk on every datastore, list every .vmdk attached
              to (or referenced by snapshot chains of) any VM or template,
              difference = orphans. For each orphan that's older than
              --min-age-days AND smaller than --max-size-mb, rename in place
              from  foo.vmdk  to  foo.zombie-YYYY-MM-DD.vmdk
              The rename uses VirtualDiskManager so all extents
              (-flat, -delta, -ctk, -sesparse) move together.
              This hides the file from any future backup or scan but
              leaves it trivially recoverable (just rename back).

  2. sweep  — find every *.zombie-YYYY-MM-DD.vmdk on every datastore.
              Parse the embedded date. If it's older than --sweep-min-age-days
              (default 7), delete via VirtualDiskManager.

Both passes write a CSV audit log (timestamped) of every action taken.
Both pass default to --dry-run; you must add --apply to actually change state.

USAGE
=====
  # see what would happen — read only
  python3 zombie_disks.py mark

  # actually rename today's orphans
  python3 zombie_disks.py mark --apply

  # a week later, sweep
  python3 zombie_disks.py sweep --apply

CONFIG
======
  Connection via env vars (preferred) or flags:
      VCENTER_HOST   --host
      VCENTER_USER   --user
      VCENTER_PASS   --pass    (prompted if not set)

  Set once per shell:
      export VCENTER_HOST=vcenter.example.com
      export VCENTER_USER=svc-cleanup@vsphere.local
      export VCENTER_PASS='...'

  Optional:
      --insecure              skip TLS verification (self-signed certs)
      --datastore PATTERN     only this datastore (regex), repeatable
      --min-age-days N        only mark orphans this old (default 30)
      --max-size-mb N         skip anything larger (default 100)
      --sweep-min-age-days N  delete renamed files this old (default 7)
      --log-dir PATH          where to write CSV audit logs (default ./logs)
"""

import argparse
import csv
import datetime as dt
import os
import re
import ssl
import sys
import time
from getpass import getpass
from pathlib import Path

try:
    from pyVim.connect import SmartConnect, Disconnect
    from pyVmomi import vim, vmodl
except ImportError:
    sys.exit("pyVmomi not installed. Run: pip3 install pyvmomi")


# ─────────────────────────── helpers ───────────────────────────

ZOMBIE_RE = re.compile(r"\.zombie-(\d{4}-\d{2}-\d{2})\.vmdk$", re.IGNORECASE)


def connect(host, user, password, insecure=False):
    ctx = None
    if insecure:
        ctx = ssl._create_unverified_context()
    try:
        si = SmartConnect(host=host, user=user, pwd=password, sslContext=ctx)
    except Exception as e:
        sys.exit(f"failed to connect to vCenter {host}: {e}")
    return si


def get_all_vms(content):
    """Return all VMs AND templates."""
    cv = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
    try:
        return list(cv.view)
    finally:
        cv.Destroy()


def get_all_datastores(content):
    cv = content.viewManager.CreateContainerView(content.rootFolder, [vim.Datastore], True)
    try:
        return list(cv.view)
    finally:
        cv.Destroy()


def datacenter_for(entity):
    """Walk up parent chain to find the containing Datacenter."""
    p = entity
    while p is not None and not isinstance(p, vim.Datacenter):
        p = p.parent
    return p


def referenced_vmdks(vms):
    """
    Build a set of every .vmdk path referenced by any VM.
    Includes:
      - Current disk file (the .vmdk the VM is using)
      - Snapshot chain (each delta's .vmdk and its parents up the chain)
      - Anything in vm.layoutEx.file (catches harder-to-find linked clones)
    Paths are normalized to '[datastore] folder/file.vmdk' form.
    """
    refs = set()

    def add(path):
        if path and path.lower().endswith(".vmdk"):
            refs.add(path)

    for vm in vms:
        # 1. Current device-level disk paths (and walk snapshot chain via backing.parent)
        try:
            for dev in (vm.config.hardware.device if vm.config else []):
                if isinstance(dev, vim.vm.device.VirtualDisk):
                    backing = dev.backing
                    while backing is not None:
                        add(getattr(backing, "fileName", None))
                        backing = getattr(backing, "parent", None)
        except Exception:
            pass

        # 2. layoutEx covers everything vCenter knows is bound to this VM —
        #    snapshots, suspended-state disks, swap files, etc.
        try:
            for f in (vm.layoutEx.file if vm.layoutEx else []):
                add(f.name)
        except Exception:
            pass

    return refs


def list_vmdks_on_datastore(content, ds):
    """
    Walk a datastore via HostDatastoreBrowser and return a list of dicts:
      [{path, size_kb, mtime}, ...]
    Only returns .vmdk descriptor files (skips -flat, -delta, -ctk, -sesparse,
    -rdm, -rdmp — those are companion data files that ride along with their
    descriptor and shouldn't be considered independently).
    """
    browser = ds.browser
    spec = vim.HostDatastoreBrowserSearchSpec()
    spec.matchPattern = ["*.vmdk"]
    spec.details = vim.FileQueryFlags(fileType=True, fileSize=True, modification=True)

    skip_suffixes = ("-flat.vmdk", "-delta.vmdk", "-ctk.vmdk",
                     "-sesparse.vmdk", "-rdm.vmdk", "-rdmp.vmdk")

    results = []
    task = browser.SearchDatastoreSubFolders_Task(datastorePath=f"[{ds.name}]", searchSpec=spec)
    wait_for_task(task)
    if task.info.state != "success":
        return results

    for folder in (task.info.result or []):
        for f in (folder.file or []):
            name = f.path
            low = name.lower()
            if any(low.endswith(s) for s in skip_suffixes):
                continue
            # also skip files we ourselves marked — sweep handles those
            full = f"{folder.folderPath}{name}"
            results.append({
                "path": full,
                "size_kb": (f.fileSize or 0) // 1024,
                "mtime": f.modification,
                "is_zombie_marker": ZOMBIE_RE.search(name) is not None,
            })
    return results


def wait_for_task(task, poll=1.0):
    while task.info.state in ("queued", "running"):
        time.sleep(poll)
    return task.info


def rename_vmdk(content, src_path, dst_path, datacenter):
    vdm = content.virtualDiskManager
    task = vdm.MoveVirtualDisk_Task(
        sourceName=src_path, sourceDatacenter=datacenter,
        destName=dst_path, destDatacenter=datacenter,
        force=False,
    )
    info = wait_for_task(task)
    if info.state != "success":
        raise RuntimeError(f"rename failed: {info.error.msg if info.error else info.state}")


def delete_vmdk(content, path, datacenter):
    vdm = content.virtualDiskManager
    task = vdm.DeleteVirtualDisk_Task(name=path, datacenter=datacenter)
    info = wait_for_task(task)
    if info.state != "success":
        raise RuntimeError(f"delete failed: {info.error.msg if info.error else info.state}")


def open_log(log_dir, name):
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = log_dir / f"{name}-{ts}.csv"
    f = open(path, "w", newline="")
    w = csv.writer(f)
    w.writerow(["timestamp", "action", "datastore", "path",
                "size_kb", "mtime", "result", "detail"])
    return f, w, path


def datastore_match(name, patterns):
    if not patterns:
        return True
    return any(re.search(p, name) for p in patterns)


# ─────────────────────────── commands ───────────────────────────

def cmd_mark(args):
    si = connect(args.host, args.user, args.password, args.insecure)
    try:
        content = si.RetrieveContent()
        vms = get_all_vms(content)
        refs = referenced_vmdks(vms)
        print(f"  collected {len(refs)} VMDK references from {len(vms)} VMs/templates")

        datastores = [ds for ds in get_all_datastores(content)
                      if datastore_match(ds.name, args.datastore)]
        print(f"  scanning {len(datastores)} datastore(s)")

        now = dt.datetime.now(dt.timezone.utc)
        min_age = dt.timedelta(days=args.min_age_days)
        max_size_kb = args.max_size_mb * 1024
        date_suffix = dt.date.today().isoformat()

        log_f, log, log_path = open_log(args.log_dir, "mark")
        total_orphans = total_eligible = total_renamed = total_failed = 0

        for ds in datastores:
            print(f"\n  [{ds.name}]")
            try:
                files = list_vmdks_on_datastore(content, ds)
            except Exception as e:
                print(f"    skip — could not browse: {e}")
                continue
            dc = datacenter_for(ds)
            for fi in files:
                if fi["is_zombie_marker"]:
                    continue  # already marked — sweep handles these
                if fi["path"] in refs:
                    continue  # attached to a VM — not orphan
                total_orphans += 1
                age = now - fi["mtime"].astimezone(dt.timezone.utc) if fi["mtime"] else dt.timedelta(0)
                size_kb = fi["size_kb"]
                eligible = (age >= min_age) and (size_kb <= max_size_kb)
                if not eligible:
                    log.writerow([dt.datetime.now().isoformat(), "skip",
                                  ds.name, fi["path"], size_kb, fi["mtime"],
                                  "skipped",
                                  f"age={age.days}d size={size_kb}KB (need ≥{args.min_age_days}d ≤{args.max_size_mb}MB)"])
                    continue
                total_eligible += 1
                new_path = re.sub(r"\.vmdk$", f".zombie-{date_suffix}.vmdk",
                                  fi["path"], flags=re.IGNORECASE)
                if args.apply:
                    try:
                        rename_vmdk(content, fi["path"], new_path, dc)
                        total_renamed += 1
                        print(f"    marked: {fi['path']}  ({size_kb} KB, {age.days}d old)")
                        log.writerow([dt.datetime.now().isoformat(), "mark",
                                      ds.name, fi["path"], size_kb, fi["mtime"],
                                      "renamed", new_path])
                    except Exception as e:
                        total_failed += 1
                        print(f"    FAILED:  {fi['path']}  — {e}")
                        log.writerow([dt.datetime.now().isoformat(), "mark",
                                      ds.name, fi["path"], size_kb, fi["mtime"],
                                      "failed", str(e)])
                else:
                    print(f"    [dry] would mark: {fi['path']}  ({size_kb} KB, {age.days}d)")
                    log.writerow([dt.datetime.now().isoformat(), "mark",
                                  ds.name, fi["path"], size_kb, fi["mtime"],
                                  "dry-run", new_path])

        log_f.close()
        print(f"\n  summary:")
        print(f"    orphans found:    {total_orphans}")
        print(f"    eligible (age+size meet thresholds): {total_eligible}")
        if args.apply:
            print(f"    renamed:          {total_renamed}")
            print(f"    failed:           {total_failed}")
        else:
            print(f"    (dry-run — pass --apply to actually rename)")
        print(f"    log:              {log_path}")
    finally:
        Disconnect(si)


def cmd_sweep(args):
    si = connect(args.host, args.user, args.password, args.insecure)
    try:
        content = si.RetrieveContent()
        datastores = [ds for ds in get_all_datastores(content)
                      if datastore_match(ds.name, args.datastore)]
        print(f"  scanning {len(datastores)} datastore(s) for *.zombie-YYYY-MM-DD.vmdk")

        today = dt.date.today()
        min_age = dt.timedelta(days=args.sweep_min_age_days)

        log_f, log, log_path = open_log(args.log_dir, "sweep")
        total_found = total_eligible = total_deleted = total_failed = 0

        for ds in datastores:
            print(f"\n  [{ds.name}]")
            try:
                files = list_vmdks_on_datastore(content, ds)
            except Exception as e:
                print(f"    skip — could not browse: {e}")
                continue
            dc = datacenter_for(ds)
            for fi in files:
                if not fi["is_zombie_marker"]:
                    continue
                total_found += 1
                m = ZOMBIE_RE.search(fi["path"])
                try:
                    marked_on = dt.date.fromisoformat(m.group(1))
                except Exception:
                    print(f"    skip (unreadable date): {fi['path']}")
                    continue
                age = today - marked_on
                if age < min_age:
                    log.writerow([dt.datetime.now().isoformat(), "skip",
                                  ds.name, fi["path"], fi["size_kb"], fi["mtime"],
                                  "skipped",
                                  f"marked {age.days}d ago (need ≥{args.sweep_min_age_days}d)"])
                    continue
                total_eligible += 1
                if args.apply:
                    try:
                        delete_vmdk(content, fi["path"], dc)
                        total_deleted += 1
                        print(f"    deleted: {fi['path']}  (marked {age.days}d ago, {fi['size_kb']} KB)")
                        log.writerow([dt.datetime.now().isoformat(), "delete",
                                      ds.name, fi["path"], fi["size_kb"], fi["mtime"],
                                      "deleted", f"marked {age.days}d ago"])
                    except Exception as e:
                        total_failed += 1
                        print(f"    FAILED:  {fi['path']}  — {e}")
                        log.writerow([dt.datetime.now().isoformat(), "delete",
                                      ds.name, fi["path"], fi["size_kb"], fi["mtime"],
                                      "failed", str(e)])
                else:
                    print(f"    [dry] would delete: {fi['path']}  (marked {age.days}d ago)")
                    log.writerow([dt.datetime.now().isoformat(), "delete",
                                  ds.name, fi["path"], fi["size_kb"], fi["mtime"],
                                  "dry-run", f"marked {age.days}d ago"])

        log_f.close()
        print(f"\n  summary:")
        print(f"    zombie-marked files found: {total_found}")
        print(f"    eligible for deletion:     {total_eligible}")
        if args.apply:
            print(f"    deleted:                   {total_deleted}")
            print(f"    failed:                    {total_failed}")
        else:
            print(f"    (dry-run — pass --apply to actually delete)")
        print(f"    log:                       {log_path}")
    finally:
        Disconnect(si)


# ─────────────────────────── CLI ───────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Find and safely clean orphaned VMDKs on vSphere datastores. "
                    "Two-pass design: 'mark' renames orphans, 'sweep' deletes them ≥7 days later.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Connection
    ap.add_argument("--host", default=os.environ.get("VCENTER_HOST"),
                    help="vCenter hostname (env: VCENTER_HOST)")
    ap.add_argument("--user", default=os.environ.get("VCENTER_USER"),
                    help="vCenter username (env: VCENTER_USER)")
    ap.add_argument("--pass", dest="password", default=os.environ.get("VCENTER_PASS"),
                    help="vCenter password (env: VCENTER_PASS; prompts if unset)")
    ap.add_argument("--insecure", action="store_true",
                    help="Skip TLS verification (for self-signed vCenter certs)")

    # Common filters
    ap.add_argument("--datastore", action="append", default=[],
                    help="Only scan datastores whose name matches this regex (repeatable)")
    ap.add_argument("--log-dir", default="./logs", help="Where to write audit CSVs (default ./logs)")
    ap.add_argument("--apply", action="store_true",
                    help="Actually make changes. Without this, runs in dry-run mode.")

    sub = ap.add_subparsers(dest="cmd", required=True)

    pm = sub.add_parser("mark", help="Identify orphans and rename them to .zombie-YYYY-MM-DD.vmdk")
    pm.add_argument("--min-age-days", type=int, default=30,
                    help="Only mark orphans whose mtime is at least this many days old (default 30)")
    pm.add_argument("--max-size-mb", type=int, default=100,
                    help="Skip orphans larger than this — usually they're real disks (default 100 MB)")
    pm.set_defaults(fn=cmd_mark)

    ps = sub.add_parser("sweep", help="Delete files marked by 'mark' once they're old enough")
    ps.add_argument("--sweep-min-age-days", type=int, default=7,
                    help="Delete zombie-marked files whose marker date is at least this old (default 7)")
    ps.set_defaults(fn=cmd_sweep)

    args = ap.parse_args()

    if not args.host:
        sys.exit("--host or VCENTER_HOST required")
    if not args.user:
        sys.exit("--user or VCENTER_USER required")
    if not args.password:
        args.password = getpass(f"Password for {args.user}@{args.host}: ")

    args.fn(args)


if __name__ == "__main__":
    main()
