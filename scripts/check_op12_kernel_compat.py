#!/usr/bin/env python3
"""Compare OP12 arm64 kernel exports and selected vendor ABI fields offline.

Inputs can be an uncompressed Image, a v3/v4 boot.img, or an AnyKernel3 ZIP.
This is a compatibility check, not a boot test or a complete ABI analysis.
The parser deliberately rejects layouts it cannot validate.
"""

import argparse
import array
import hashlib
import json
import re
import struct
import sys
import zipfile
import zlib
from pathlib import Path

SCHEDULER_TYPES = {
    "task_struct",
    "sched_ext_entity",
    "rq",
    "task_group",
    "scx_dispatch_q",
    "scx_sched_task_stats",
    "scx_iso_masks",
}


def read_image(path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            data = archive.read("Image")
    else:
        data = path.read_bytes()
    if data.startswith(b"ANDROID!"):
        version = struct.unpack_from("<I", data, 40)[0]
        if version not in (3, 4):
            raise ValueError("Only boot header v3/v4 is supported")
        size = struct.unpack_from("<I", data, 8)[0]
        if size > len(data) - 4096:
            raise ValueError("Truncated boot image")
        data = data[4096 : 4096 + size]
    if data[56:60] != b"ARM\x64":
        raise ValueError("Expected an uncompressed arm64 Linux Image")
    return data


def read_kernel_metadata(data):
    match = re.search(rb"Linux version ([^\x00\s]+)", data)
    if match is None:
        raise ValueError("Kernel release is missing")
    start = data.find(b"IKCFG_ST")
    while start >= 0:
        end = data.find(b"IKCFG_ED", start + 8)
        if end >= 0:
            try:
                config = zlib.decompress(data[start + 8 : end], 31).decode()
                return match.group(1).decode(), set(config.splitlines())
            except (zlib.error, UnicodeError):
                pass
        start = data.find(b"IKCFG_ST", start + 8)
    raise ValueError("No readable embedded kernel configuration")


def make_reference(data, system):
    release, config = read_kernel_metadata(data)
    required = [
        "CONFIG_HMBIRD_SCHED=y",
        "CONFIG_MODVERSIONS=y",
        "CONFIG_IKCONFIG=y",
        "CONFIG_DEBUG_INFO_BTF=y",
        "CONFIG_DEBUG_INFO_BTF_MODULES=y",
        "CONFIG_MODULE_ALLOW_BTF_MISMATCH=y",
        "CONFIG_CFI_CLANG=y",
        "CONFIG_ARM64_4K_PAGES=y",
        "CONFIG_ANDROID_KABI_RESERVE=y",
    ]
    if any(entry not in config for entry in required):
        raise ValueError("The reference must be a stock HMBIRD kernel with BTF")
    scheduler_btf = read_btf(data)
    if set(scheduler_btf) != SCHEDULER_TYPES:
        raise ValueError("The reference scheduler layouts are incomplete")
    return {
        "format_version": 1,
        "system": system,
        "reference_sha256": hashlib.sha256(data).hexdigest(),
        "kernel_release": release,
        "required_config": required,
        "exports": read_exports(data),
        "scheduler_btf": scheduler_btf,
        "scope": "Export CRCs and selected scheduler layouts; not a complete ABI or boot test.",
    }


def load_reference(path):
    reference = json.loads(path.read_text())
    if reference.get("format_version") != 1:
        raise ValueError("Unsupported compatibility reference format")
    if len(reference["exports"]) < 1000:
        raise ValueError("The reference export table is incomplete")
    if set(reference["scheduler_btf"]) != SCHEDULER_TYPES:
        raise ValueError("The reference scheduler layouts are incomplete")
    for name, crc in reference["exports"].items():
        if (
            not isinstance(name, str)
            or not isinstance(crc, int)
            or not 0 <= crc < 2**32
        ):
            raise ValueError("Invalid reference export record")
    return reference


def cstring(data, offset, limit=256):
    end = data.find(b"\0", offset, min(offset + limit, len(data)))
    if offset < 0 or end < 0:
        raise ValueError("Invalid string offset")
    return data[offset:end].decode("ascii")


def read_exports(data):
    # Linux 6.1 arm64 uses 12-byte PREL32 kernel_symbol records. The linker
    # places the matching 32-bit CRC arrays directly before the name strings.
    def symbol(offset):
        if offset < 0 or offset + 12 > len(data):
            return None
        value_rel, name_rel, namespace_rel = struct.unpack_from("<iii", data, offset)
        name_offset = offset + 4 + name_rel
        namespace_offset = offset + 8 + namespace_rel
        image_size = struct.unpack_from("<Q", data, 16)[0]
        if not (
            offset < name_offset < len(data)
            and offset < namespace_offset < len(data)
            and 0 <= offset + value_rel < image_size
        ):
            return None
        try:
            name = cstring(data, name_offset)
            namespace = cstring(data, namespace_offset)
        except (ValueError, UnicodeError):
            return None
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z_0-9.]*", name):
            return None
        if namespace and not re.fullmatch(r"[a-zA-Z_][a-zA-Z_0-9]*", namespace):
            return None
        return name, name_offset

    name_offsets = set()
    offset = 0
    while True:
        offset = data.find(b"module_layout\0", offset)
        if offset < 0:
            break
        name_offsets.add(offset)
        offset += 1
    values = array.array("i")
    values.frombytes(data[: len(data) // 4 * 4])
    if sys.byteorder != "little":
        values.byteswap()
    tables = []
    for index, value in enumerate(values):
        if index * 4 + value not in name_offsets:
            continue
        start = index * 4 - 4
        if not symbol(start):
            continue
        while symbol(start - 12):
            start -= 12
        end = start
        while symbol(end):
            end += 12
        entries = [symbol(pos) for pos in range(start, end, 12)]
        if len(entries) < 1000:
            continue
        if end + 4 * len(entries) != min(entry[1] for entry in entries):
            continue
        names = [entry[0] for entry in entries]
        if len(names) != len(set(names)):
            continue
        exports = {
            name: struct.unpack_from("<I", data, end + 4 * index)[0]
            for index, name in enumerate(names)
        }
        tables.append(exports)
    if len(tables) != 1:
        raise ValueError(f"Expected one validated export table, found {len(tables)}")
    return tables[0]


def read_btf(data):
    # BTF kinds have fixed-size payloads, with vlen entries for aggregate kinds.
    entry_sizes = {
        1: 4,
        2: 0,
        3: 12,
        4: 12,
        5: 12,
        6: 8,
        7: 0,
        8: 0,
        9: 0,
        10: 0,
        11: 0,
        12: 0,
        13: 8,
        14: 4,
        15: 12,
        16: 0,
        17: 4,
        18: 0,
        19: 12,
    }
    offset = -1
    while True:
        offset = data.find(b"\x9f\xeb\x01\x00", offset + 1)
        if offset < 0:
            raise ValueError("No supported BTF header found")
        if offset + 24 > len(data):
            continue
        _, _, _, header_size, type_off, type_len, str_off, str_len = struct.unpack_from(
            "<HBBIIIII", data, offset
        )
        if (
            header_size == 24
            and 0 < type_len <= str_off
            and str_len > 0
            and offset + header_size + str_off + str_len <= len(data)
        ):
            break
    strings = data[
        offset + header_size + str_off : offset + header_size + str_off + str_len
    ]
    if strings[:1] != b"\0":
        raise ValueError("Invalid BTF string table")
    position = offset + header_size + type_off
    end = position + type_len
    types = [None]
    while position < end:
        name_offset, info, size = struct.unpack_from("<III", data, position)
        kind, count = (info >> 24) & 31, info & 65535
        if kind not in entry_sizes:
            raise ValueError(f"Unsupported BTF kind {kind}")
        name = cstring(strings, name_offset, len(strings))
        members = []
        if kind in (4, 5):
            for index in range(count):
                member_name, member_type, bit_offset = struct.unpack_from(
                    "<III", data, position + 12 + index * 12
                )
                members.append(
                    (
                        cstring(strings, member_name, len(strings)),
                        member_type,
                        bit_offset & 0xFFFFFF if info >> 31 else bit_offset,
                    )
                )
        types.append((name, kind, size, members))
        entries = count if kind in (4, 5, 6, 13, 15, 19) else 1
        position += 12 + entry_sizes[kind] * entries
    if position != end:
        raise ValueError("Invalid BTF type table length")

    def member_offsets(type_id, base=0):
        result = {}
        for name, child, bit_offset in types[type_id][3]:
            if name:
                result[name] = base + bit_offset
            elif types[child][1] in (4, 5):
                result.update(member_offsets(child, base + bit_offset))
        return result

    summary = {}
    for type_id, entry in enumerate(types[1:], 1):
        name, kind, size, _ = entry
        if kind == 4 and name in SCHEDULER_TYPES:
            summary[name] = {
                "size": size,
                "member_bit_offsets": member_offsets(type_id),
            }
    if not {"task_struct", "rq"}.issubset(summary):
        raise ValueError("Core scheduler BTF types are missing")
    return summary


def module_versions(path):
    data = path.read_bytes()
    if data[:6] != b"\x7fELF\x02\x01":
        raise ValueError(f"Expected a little-endian ELF64 module: {path}")
    section_offset = struct.unpack_from("<Q", data, 40)[0]
    section_size, count, string_index = struct.unpack_from("<HHH", data, 58)
    sections = [
        struct.unpack_from("<IIQQQQIIQQ", data, section_offset + index * section_size)
        for index in range(count)
    ]
    names = sections[string_index]
    strings = data[names[4] : names[4] + names[5]]
    for section in sections:
        if cstring(strings, section[0]) != "__versions":
            continue
        payload = data[section[4] : section[4] + section[5]]
        if len(payload) % 64:
            raise ValueError(f"Unsupported module version record size: {path}")
        return {
            cstring(payload, index + 8, 56): struct.unpack_from("<Q", payload, index)[0]
            for index in range(0, len(payload), 64)
        }
    raise ValueError(f"Module has no __versions section: {path}")


def compare_exports(reference, candidate):
    missing = sorted(reference.keys() - candidate.keys())
    crc_changes = {
        name: {
            "reference": hex(reference[name]),
            "candidate": hex(candidate[name]),
        }
        for name in sorted(reference.keys() & candidate.keys())
        if reference[name] != candidate[name]
    }
    return missing, crc_changes


def compare_scheduler_layouts(reference, candidate):
    abi_changes = []
    for name in reference:
        if name not in candidate:
            abi_changes.append(
                {"field": f"{name}.presence", "reference": True, "candidate": False}
            )
            continue
        for field in ("size", "member_bit_offsets"):
            before, after = reference[name][field], candidate[name][field]
            if field == "size" and before != after:
                abi_changes.append(
                    {"field": f"{name}.sizeof", "reference": before, "candidate": after}
                )
            elif field == "member_bit_offsets":
                for member in sorted(before):
                    # Reserved KABI slots may legitimately gain named fields.
                    if member.startswith("android_kabi_reserved"):
                        continue
                    if member not in after or before[member] != after[member]:
                        abi_changes.append(
                            {
                                "field": f"{name}.{member}.bit_offset",
                                "reference": before[member],
                                "candidate": after.get(member),
                            }
                        )
    return abi_changes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    reference_group = parser.add_mutually_exclusive_group(required=True)
    reference_group.add_argument("--reference", type=Path)
    reference_group.add_argument("--reference-json", type=Path)
    parser.add_argument("--save-reference", type=Path)
    parser.add_argument("--system", default="")
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--module", action="append", default=[], type=Path)
    parser.add_argument("--module-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.candidate is None and args.save_reference is None:
        parser.error("--candidate or --save-reference is required")
    if args.reference_json:
        reference = load_reference(args.reference_json)
    else:
        reference = make_reference(read_image(args.reference), args.system)
    if args.save_reference:
        args.save_reference.write_text(
            json.dumps(reference, indent=2, sort_keys=True) + "\n"
        )
        print(f"Saved compatibility reference: {args.save_reference}")
    if args.candidate is None:
        return 0
    candidate = read_image(args.candidate)
    stock_exports = reference["exports"]
    candidate_exports = read_exports(candidate)
    stock_btf = reference["scheduler_btf"]
    candidate_btf = read_btf(candidate)
    release, config = read_kernel_metadata(candidate)
    missing_config = sorted(set(reference["required_config"]) - config)
    expected = re.match(r"^(\d+\.\d+\.\d+-android\d+)-", reference["kernel_release"])
    if expected is None:
        raise ValueError("Reference kernel release has no Android KMI identifier")
    version_matches = release.startswith(expected.group(1) + "-")
    missing, crc_changes = compare_exports(stock_exports, candidate_exports)
    abi_changes = compare_scheduler_layouts(stock_btf, candidate_btf)
    modules = list(args.module)
    if args.module_dir:
        modules.extend(sorted(args.module_dir.rglob("*.ko")))
    affected = []
    reference_errors = []
    for path in modules:
        versions = module_versions(path)
        required = {
            name: crc for name, crc in versions.items() if name in stock_exports
        }
        wrong_reference = [
            name for name, crc in required.items() if stock_exports[name] != crc
        ]
        if wrong_reference:
            reference_errors.append({"module": path.name, "symbols": wrong_reference})
        absent = sorted(name for name in required if name not in candidate_exports)
        changed = sorted(
            name
            for name, crc in required.items()
            if name in candidate_exports and candidate_exports[name] != crc
        )
        if absent or changed:
            affected.append(
                {"module": path.name, "missing": absent, "crc_mismatch": changed}
            )
    report = {
        "scope": "Static arm64 export/CRC and selected scheduler BTF checks; not a boot test.",
        "reference_sha256": reference["reference_sha256"],
        "candidate_sha256": hashlib.sha256(candidate).hexdigest(),
        "reference_kernel_release": reference["kernel_release"],
        "candidate_kernel_release": release,
        "version_matches": version_matches,
        "missing_config": missing_config,
        "reference_export_count": len(stock_exports),
        "candidate_export_count": len(candidate_exports),
        "missing_exports": missing,
        "changed_export_crcs": crc_changes,
        "scheduler_abi_changes": abi_changes,
        "modules_checked": len(modules),
        "reference_module_crc_errors": reference_errors,
        "affected_modules": affected,
    }
    failed = bool(
        missing
        or crc_changes
        or abi_changes
        or reference_errors
        or missing_config
        or not version_matches
    )
    report["result"] = "FAIL" if failed else "PASS"
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"{report['result']}: missing_exports={len(missing)}, changed_crcs={len(crc_changes)}, "
        f"scheduler_abi_changes={len(abi_changes)}, affected_modules={len(affected)}/{len(modules)}"
    )
    if not version_matches:
        print(f"  Kernel release mismatch: {release}; expected {expected.group(1)}-*")
    for entry in missing_config:
        print(f"  Missing configuration: {entry}")
    for change in abi_changes:
        print(f"  {change['field']}: {change['reference']} -> {change['candidate']}")
    for module in affected[:12]:
        print(
            f"  {module['module']}: {len(module['missing'])} missing, "
            f"{len(module['crc_mismatch'])} CRC mismatches"
        )
    if len(affected) > 12:
        print(f"  {len(affected) - 12} more affected modules; see the JSON report")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (
        ValueError,
        IndexError,
        KeyError,
        OSError,
        struct.error,
        zipfile.BadZipFile,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(2)
