# zombie_disks.py

A two-pass, recoverable cleaner for orphaned VMDK files on VMware vSphere datastores. Built for environments where Druva / Broadcom integration leaves behind hundreds of stranded `.vmdk` files every month while a permanent fix is pending.

## What "zombie" means here

A `.vmdk` descriptor file sitting in a datastore folder that **no VM (or template, or snapshot chain, or linked-clone parent)** has any reference to. vCenter doesn't know about it. It just consumes storage and confuses backup tools.

## The two passes

1. **`mark`** — list every `.vmdk` on every datastore, list every `.vmdk` referenced anywhere by any VM, take the difference. For each orphan that's old enough and small enough (configurable), rename it in place:
   ```
   foo.vmdk  →  foo.zombie-2026-06-11.vmdk
   ```
   The rename uses `VirtualDiskManager.MoveVirtualDisk` so the data extents (`-flat`, `-delta`, `-ctk`, `-sesparse`) move together with the descriptor. Fully reversible — rename back manually if you flagged something by mistake.

2. **`sweep`** — find every `*.zombie-YYYY-MM-DD.vmdk`, parse the embedded date, delete via `VirtualDiskManager.DeleteVirtualDisk` if the marker is older than `--sweep-min-age-days` (default 7).

Both passes default to `--dry-run`. You must pass `--apply` to actually change state. Both write a timestamped CSV to `./logs/` recording every file inspected, action taken, and outcome.

## Install

Requires Python 3.8+.

```bash
pip3 install pyvmomi
```

## Configure

Set vCenter credentials once per shell:

```bash
export VCENTER_HOST=vcenter.example.com
export VCENTER_USER='svc-cleanup@vsphere.local'
export VCENTER_PASS='your-password'      # or leave unset to be prompted
```

The user needs at minimum:
- `System.View` on the root folder
- `Datastore.Browse`, `Datastore.FileManagement` on the target datastores
- `VirtualMachine.Provisioning.MarkAsVirtualMachine` (incidentally required by VirtualDiskManager)

## Use

```bash
# 1. See what would be marked today (read-only)
python3 zombie_disks.py mark

# 2. When the dry-run looks right, do it
python3 zombie_disks.py mark --apply

# 3. Seven days later, dry-run the sweep
python3 zombie_disks.py sweep

# 4. Then sweep for real
python3 zombie_disks.py sweep --apply
```

For self-signed vCenter certificates (common in lab/internal):
```bash
python3 zombie_disks.py mark --insecure
```

Limit to specific datastores:
```bash
python3 zombie_disks.py mark --datastore '^prod-' --datastore '^dr-'
```

Adjust thresholds:
```bash
# only mark orphans 60+ days old, skip anything over 500 MB
python3 zombie_disks.py mark --min-age-days 60 --max-size-mb 500 --apply

# sweep zombies marked 14+ days ago instead of the default 7
python3 zombie_disks.py sweep --sweep-min-age-days 14 --apply
```

## What it considers "attached"

Per-VM, the script walks:
- Every `VirtualDisk` device's `backing.fileName`
- The full snapshot chain via `backing.parent` (so all delta files in a chain are protected)
- `vm.layoutEx.file` — vCenter's authoritative list of files it associates with this VM, which catches edge cases like linked-clone parents and suspended-state files

This is a superset of what you'd get from RVTools' vDisk sheet. False-positive risk should be very low.

## Audit log

Every run writes `logs/{mark|sweep}-YYYYMMDD-HHMMSS.csv`:

| Column | Meaning |
|---|---|
| `timestamp` | when this row was written |
| `action` | mark, skip, delete |
| `datastore` | datastore name |
| `path` | full `[datastore] folder/file.vmdk` path |
| `size_kb` | file size in KB |
| `mtime` | last modified time from vCenter |
| `result` | dry-run, renamed, deleted, skipped, failed |
| `detail` | new path on rename, error on fail, reason on skip |

Keep these — they're the receipts for what got touched.

## Failure modes & recovery

- **Marked the wrong file.** Before sweep, manually rename `foo.zombie-2026-06-11.vmdk` back to `foo.vmdk` in the datastore browser or via `vmkfstools -E`. No data is touched until sweep runs.
- **`MoveVirtualDisk` fails on a corrupted descriptor.** The error is logged and the file is left alone. You can clean those manually or fall back to a raw `FileManager.MoveDatastoreFile` per file — not implemented here because corruption is rare and a manual review is probably warranted anyway.
- **`DeleteVirtualDisk` fails on sweep.** Logged. File stays as `*.zombie-*.vmdk`; you can retry next sweep or delete manually.

## Suggested monthly cadence

```
Day 1   : mark --apply              (rename orphans today)
Day 8+  : sweep --apply             (delete things marked ≥7d ago)
```

A 7-day window has proven a good balance: long enough that an accidental flagging would have surfaced via "where did my VM's disk go" tickets, short enough that storage gets reclaimed within the month.

## Why not just delete in one pass?

Because Druva-related orphans sometimes look exactly like legitimate disks that just *aren't actively attached at the moment* (e.g., a VM mid-storage-vMotion, a snapshot being consolidated, a template being cloned). The rename pass is a soft-quarantine: vCenter ignores the file, no backup picks it up, but everything stays recoverable for a week. If something was misidentified, someone hits "my VM is broken" inside the window, and you have time to undo.
