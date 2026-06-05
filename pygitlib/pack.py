"""
pygitlib/pack.py

Git packfile reader.

Real git periodically packs loose objects into a single binary .pack file for
efficiency.  Every cloned repository also arrives as a pack.  This module lets
every existing pygit command work transparently regardless of whether objects
are stored loose or packed.

─── File formats ──────────────────────────────────────────────────────────────

.pack header (12 bytes)
    4   "PACK" magic
    4   version (big-endian uint32, always 2 for modern git)
    4   object count (big-endian uint32)

Each object entry
    variable  type+size header  (MSB = continuation, bits 6-4 = type, rest = size)
    variable  type-specific prefix:
                OFS_DELTA  → negative base-offset (variable-length, special encoding)
                REF_DELTA  → 20-byte base SHA-1
    variable  zlib-compressed payload

.pack trailer
    20  SHA-1 of all preceding bytes

.idx (version 2)
    4   "\xff\x74\x4f\x63" magic
    4   version = 2
    256×4  fan-out table: fan[i] = cumulative count of objects whose first
                                   SHA byte ≤ i
    N×20  sorted SHA-1 entries
    N×4   CRC32 per entry
    N×4   offsets (bit 31 set → index into the large-offset table below)
    M×8   large offsets (for pack files > 2 GB)
    20  pack SHA-1
    20  index SHA-1

─── Delta encoding ────────────────────────────────────────────────────────────

OFS_DELTA and REF_DELTA both use the same instruction stream after
decompression:
    variable-length  source size  (decoded by _read_varint)
    variable-length  target size  (decoded by _read_varint)
    repeated instructions:
      byte & 0x80 == 1  →  COPY: conditional 4+3-byte offset/size fields
      byte & 0x80 == 0, byte > 0  →  INSERT: N literal bytes follow
"""

import struct
import zlib
from pathlib import Path


# ---------------------------------------------------------------------------
# Variable-length integer helpers
# ---------------------------------------------------------------------------

def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """
    Decode a little-endian base-128 (LEB128) integer as used in delta headers.
    Returns (value, new_pos).
    """
    value = shift = 0
    while True:
        byte = data[pos]; pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not (byte & 0x80):
            break
    return value, pos


def _read_ofs_offset(data: bytes, pos: int) -> tuple[int, int]:
    """
    Decode the base-offset field of an OFS_DELTA entry.

    Git uses a non-redundant variable-length encoding where each additional
    continuation byte implicitly adds 1 to the accumulated value before the
    next 7-bit shift, eliminating representations that would otherwise waste
    range (e.g. 0x80 0x00 equalling plain 0x00).

    Returns (negative_offset, new_pos).  The actual base_offset in the file
    is:  object_start_offset - returned_value.
    """
    byte = data[pos]; pos += 1
    offset = byte & 0x7F
    while byte & 0x80:
        byte = data[pos]; pos += 1
        offset = ((offset + 1) << 7) | (byte & 0x7F)
    return offset, pos


# ---------------------------------------------------------------------------
# Delta application
# ---------------------------------------------------------------------------

def _apply_delta(base: bytes, delta: bytes) -> bytes:
    """
    Reconstruct a target object by applying a binary delta to a base object.

    Delta instruction set:
      COPY   (bit 7 set)   — selectively present 4 offset + 3 size bytes
                             then copy that slice of *base* to output.
      INSERT (bit 7 clear) — the low 7 bits give N; copy N literal bytes.
    """
    pos = 0
    src_size, pos = _read_varint(delta, pos)
    tgt_size, pos = _read_varint(delta, pos)

    if len(base) != src_size:
        raise ValueError(
            f"Delta source-size mismatch: header says {src_size}, "
            f"base is {len(base)} bytes"
        )

    out = bytearray()
    while pos < len(delta):
        cmd = delta[pos]; pos += 1

        if cmd & 0x80:
            # COPY instruction — up to 4 offset bytes, up to 3 size bytes
            cp_off = cp_size = 0
            if cmd & 0x01: cp_off  |= delta[pos] <<  0; pos += 1
            if cmd & 0x02: cp_off  |= delta[pos] <<  8; pos += 1
            if cmd & 0x04: cp_off  |= delta[pos] << 16; pos += 1
            if cmd & 0x08: cp_off  |= delta[pos] << 24; pos += 1
            if cmd & 0x10: cp_size |= delta[pos] <<  0; pos += 1
            if cmd & 0x20: cp_size |= delta[pos] <<  8; pos += 1
            if cmd & 0x40: cp_size |= delta[pos] << 16; pos += 1
            if cp_size == 0:
                cp_size = 0x10000           # 0 encodes 65 536 per the spec
            out += base[cp_off: cp_off + cp_size]

        elif cmd > 0:
            # INSERT instruction — cmd low-7 bits = byte count
            out += delta[pos: pos + cmd]
            pos += cmd

        else:
            raise ValueError("Corrupt delta: encountered zero-instruction byte")

    if len(out) != tgt_size:
        raise ValueError(
            f"Delta target-size mismatch: header says {tgt_size}, "
            f"got {len(out)} bytes"
        )
    return bytes(out)


# ---------------------------------------------------------------------------
# Pack index reader
# ---------------------------------------------------------------------------

_TYPE_NAMES: dict[int, str] = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
_NAME_TYPES: dict[str, int] = {v: k for k, v in _TYPE_NAMES.items()}


class PackIndex:
    """
    Reads a version-2 pack index (.idx) and maps SHA-1 strings to byte offsets
    inside the corresponding .pack file.
    """

    def __init__(self, idx_path: Path):
        data = idx_path.read_bytes()

        if data[:4] != b'\xff\x74\x4f\x63':
            raise ValueError(f"{idx_path.name}: not a v2 pack index (bad magic)")
        version = struct.unpack(">I", data[4:8])[0]
        if version != 2:
            raise ValueError(f"{idx_path.name}: unsupported index version {version}")

        # Fan-out table: 256 big-endian uint32 values
        fan_off = 8
        fan = struct.unpack(">256I", data[fan_off: fan_off + 1024])
        n = fan[255]                         # total number of objects

        sha_off   = fan_off + 1024           # N × 20-byte SHA-1 entries
        crc_off   = sha_off   + n * 20       # N × 4-byte CRC32 entries
        small_off = crc_off   + n *  4       # N × 4-byte offsets
        large_off = small_off + n *  4       # M × 8-byte large offsets

        self._offsets: dict[str, int] = {}
        for i in range(n):
            sha = data[sha_off + i * 20: sha_off + i * 20 + 20].hex()
            raw = struct.unpack(">I", data[small_off + i*4: small_off + i*4 + 4])[0]
            if raw & 0x8000_0000:
                # MSB set → raw & 0x7FFF_FFFF indexes the large-offset table
                li = raw & 0x7FFF_FFFF
                offset = struct.unpack(">Q", data[large_off + li*8: large_off + li*8 + 8])[0]
            else:
                offset = raw
            self._offsets[sha] = offset

    def get_offset(self, sha: str) -> int | None:
        """Return the byte offset of *sha* in the packfile, or None."""
        return self._offsets.get(sha.lower())

    @property
    def shas(self) -> list[str]:
        """All SHA-1 strings stored in this index."""
        return list(self._offsets.keys())


# ---------------------------------------------------------------------------
# Pack file reader
# ---------------------------------------------------------------------------

class PackFile:
    """
    Reads and decompresses objects from a git packfile (.pack).

    All object types are supported including both delta types:
      OFS_DELTA  — base is an earlier object in this same pack (by offset)
      REF_DELTA  — base is identified by its 20-byte SHA-1 reference

    Delta chains of arbitrary depth are resolved recursively.
    """

    _OFS = 6
    _REF = 7

    def __init__(self, pack_path: Path):
        self._path = pack_path
        self._data = pack_path.read_bytes()

        if self._data[:4] != b"PACK":
            raise ValueError(f"{pack_path.name}: not a packfile (bad magic)")
        self._version   = struct.unpack(">I", self._data[4:8])[0]
        self._n_objects = struct.unpack(">I", self._data[8:12])[0]

    @property
    def n_objects(self) -> int:
        return self._n_objects

    def get_object_at(self, offset: int) -> tuple[str, bytes]:
        """
        Fully resolve the object at byte *offset* (following any delta chain).
        Returns ``(type_name, raw_bytes)``.
        """
        type_num, data = self._read_at(offset)
        return _TYPE_NAMES.get(type_num, f"type{type_num}"), data

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    def _read_header(self, pos: int) -> tuple[int, int, int]:
        """
        Decode the variable-length object-type + size header.
        Returns (obj_type, uncompressed_size, new_pos).

        First byte layout:
          bit 7        continuation flag
          bits 6-4     object type  (3 bits)
          bits 3-0     low 4 bits of uncompressed size
        Subsequent bytes (while continuation flag is set):
          bit 7        continuation flag
          bits 6-0     next 7 bits of uncompressed size
        """
        byte = self._data[pos]; pos += 1
        obj_type = (byte >> 4) & 0x7
        size     = byte & 0xF
        shift    = 4
        while byte & 0x80:
            byte  = self._data[pos]; pos += 1
            size |= (byte & 0x7F) << shift
            shift += 7
        return obj_type, size, pos

    def _decompress_at(self, pos: int) -> tuple[bytes, int]:
        """
        Decompress one zlib stream starting at *pos*.
        Returns (decompressed_bytes, byte_position_after_stream).

        The decompressor stops exactly at the end-of-stream marker;
        unused_data contains everything that follows (the next object, etc.).
        """
        dec  = zlib.decompressobj()
        data = self._data[pos:]
        try:
            out = dec.decompress(data)
        except zlib.error as exc:
            raise ValueError(
                f"Corrupt pack {self._path.name}: zlib error at offset {pos}: {exc}"
            )
        consumed = len(data) - len(dec.unused_data)
        return bytes(out), pos + consumed

    def _read_at(self, offset: int) -> tuple[int, bytes]:
        """
        Recursively resolve an object, following OFS / REF delta chains.
        Returns (numeric_type, raw_bytes).
        """
        obj_type, _size, pos = self._read_header(offset)

        if obj_type in _TYPE_NAMES:                      # commit / tree / blob / tag
            data, _ = self._decompress_at(pos)
            return obj_type, data

        if obj_type == self._OFS:
            neg_off, pos    = _read_ofs_offset(self._data, pos)
            delta, _        = self._decompress_at(pos)
            base_type, base = self._read_at(offset - neg_off)
            return base_type, _apply_delta(base, delta)

        if obj_type == self._REF:
            base_sha        = self._data[pos: pos + 20].hex()
            delta, _        = self._decompress_at(pos + 20)
            base_type, base = _resolve_ref_base(self._path.parent, base_sha)
            return base_type, _apply_delta(base, delta)

        raise ValueError(
            f"Unknown pack object type {obj_type} at offset {offset} "
            f"in {self._path.name}"
        )


# ---------------------------------------------------------------------------
# REF_DELTA base resolution and pack discovery
# ---------------------------------------------------------------------------

def _resolve_ref_base(pack_dir: Path, sha: str) -> tuple[int, bytes]:
    """
    Find the base object for a REF_DELTA entry.

    Searches (in order): loose objects → every .pack in *pack_dir*.
    Returns (numeric_type, raw_bytes).
    """
    git_dir = pack_dir.parent.parent      # .git/objects/pack → .git

    # Try the loose object store first
    loose = git_dir / "objects" / sha[:2] / sha[2:]
    if loose.exists():
        raw = zlib.decompress(loose.read_bytes())
        null   = raw.index(b"\x00")
        type_str, _ = raw[:null].decode().split(" ", 1)
        return _NAME_TYPES.get(type_str, 3), raw[null + 1:]

    # Fall back to any other packs in the same directory
    for idx_path in sorted(pack_dir.glob("*.idx")):
        idx    = PackIndex(idx_path)
        offset = idx.get_offset(sha)
        if offset is not None:
            pf = PackFile(idx_path.with_suffix(".pack"))
            tn, data = pf.get_object_at(offset)
            return _NAME_TYPES.get(tn, 3), data

    raise FileNotFoundError(
        f"REF_DELTA base {sha[:7]} not found as loose object or in any pack"
    )


def find_packs(git_dir: Path) -> list[tuple[PackIndex, PackFile]]:
    """
    Return all ``(PackIndex, PackFile)`` pairs for packs present in *git_dir*.
    Silently skips corrupt or unreadable pack files.
    """
    pack_dir = git_dir / "objects" / "pack"
    if not pack_dir.is_dir():
        return []
    pairs: list[tuple[PackIndex, PackFile]] = []
    for idx_path in sorted(pack_dir.glob("*.idx")):
        pack_path = idx_path.with_suffix(".pack")
        if pack_path.exists():
            try:
                pairs.append((PackIndex(idx_path), PackFile(pack_path)))
            except Exception:
                pass
    return pairs


# ---------------------------------------------------------------------------
# High-level entry point (used by objects.read_object as a fallback)
# ---------------------------------------------------------------------------

def read_packed_object(git_dir: Path, sha: str) -> tuple[str, bytes]:
    """
    Search all pack files for *sha* and return ``(type_name, raw_bytes)``.
    Raises ``FileNotFoundError`` when the object is not found in any pack.
    """
    for idx, pack in find_packs(git_dir):
        offset = idx.get_offset(sha)
        if offset is not None:
            return pack.get_object_at(offset)
    raise FileNotFoundError(
        f"Object {sha[:7]} not found in any packfile"
    )
