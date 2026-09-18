"""Permanent schema-1 geometry; sector counts are always 512-byte sectors."""
from __future__ import annotations

MIB = 1024 * 1024
SECTOR = 512
IMAGE_BYTES = 20480 * MIB
MIN_CARD_BYTES = 30_000_000_000  # Marketed 32 GB cards have varying usable capacities.
ALIGN_BYTES = 4 * MIB
PARTITIONS = (
    (1, "boot-control", "CP-CONTROL", "vfat", 4, 64),
    (2, "boot-A", "CP-BOOT-A", "vfat", 68, 1024),
    (3, "boot-B", "CP-BOOT-B", "vfat", 1092, 1024),
    (4, "root-A", "root-A", "ext4", 2116, 8192),
    (5, "root-B", "root-B", "ext4", 10308, 8192),
    (6, "data", "data", "ext4", 18500, 1976),
)


def validate_geometry(table, *, growing=False):
    if table.get("label") != "gpt" or table.get("sectorsize", SECTOR) != SECTOR:
        raise ValueError("LAYOUT: require GPT with 512-byte logical sectors")
    parts = table.get("partitions", [])
    if len(parts) != len(PARTITIONS):
        raise ValueError("LAYOUT: require exactly six partitions")
    for actual, (number, label, _, _, start, size) in zip(parts, PARTITIONS):
        if actual.get("name") != label or actual.get("start") != start * MIB // SECTOR:
            raise ValueError(f"LAYOUT: partition {number} identity/start mismatch")
        sectors = actual.get("size", 0)
        expected = size * MIB // SECTOR
        if sectors < expected or (number != 6 or not growing) and sectors != expected:
            raise ValueError(f"LAYOUT: partition {number} permanent size mismatch")
        expected_type = ("EBD0A0A2-B9E5-4433-87C0-68B6B72699C7" if number <= 3
                         else "0FC63DAF-8483-4772-8E79-3D69D8477DE4")
        if actual.get("type", "").upper() != expected_type:
            raise ValueError(f"LAYOUT: partition {number} type mismatch")
        if not actual.get("uuid"):
            raise ValueError("LAYOUT: missing partition UUID")
    if len({p["uuid"].lower() for p in parts}) != 6:
        raise ValueError("LAYOUT: duplicate partition UUID")
    return parts


def _uuid(value):
    import uuid
    if not isinstance(value, str) or str(uuid.UUID(value)) != value.lower():
        raise ValueError("LAYOUT: invalid partition UUID")
    return value.lower()


def fstab(slot, root_uuid, boot_uuid):
    if slot not in ("A", "B"):
        raise ValueError("SLOT")
    return (
        "proc /proc proc defaults 0 0\n"
        f"PARTUUID={_uuid(root_uuid)} / ext4 defaults,noatime 0 1\n"
        f"PARTUUID={_uuid(boot_uuid)} /boot/firmware vfat defaults,uid=0,gid=0,fmask=0133,dmask=0022 0 2\n"
    )


def cmdline(template, slot, root_uuid):
    if slot not in ("A", "B"):
        raise ValueError("SLOT")
    args = [arg for arg in template.split() if not arg.startswith(
        ("root=", "init=", "systemd.run=", "systemd.run_success_action=",
         "systemd.run_failure_action=")) and arg not in ("ro", "rw", "resize")]
    return " ".join(args + [f"root=PARTUUID={_uuid(root_uuid)}", "rw"]) + "\n"


AUTOBOOT = "[all]\ntryboot_a_b=1\nboot_partition=2\n[tryboot]\nboot_partition=3\n"
