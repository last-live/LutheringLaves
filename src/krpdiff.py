# Apply HDiffPatch

from __future__ import annotations
import hashlib
from dataclasses import dataclass, field
from typing import BinaryIO, Callable, Dict, Iterator, List, Optional, Tuple, Union

MAGIC_VERSION = b"HDIFF19"
HDIFF_STREAM_VERSION = b"HDIFF13"
MEM_COPY_CHUNK = 4 << 20
HEAD_READ_LIMIT = 4 << 20
_ADD_TABLES: Dict[int, bytes] = {}
_SPREAD_MASKS: Dict[Tuple[int, int], int] = {}
_ADD_CHUNK = 4096

__all__ = [
    "KrpdiffError",
    "OldRefFile",
    "NewRefFile",
    "CopyFile",
    "DirPatch",
    "OldStream",
    "PatchWriter",
    "parse_dir_patch",
    "looks_like_dir_patch",
    "md5_file",
]

class KrpdiffError(Exception):
    """frpdiff 解析/应用失败"""


def md5_file(path, chunk_size: int = MEM_COPY_CHUNK) -> Tuple[str, int]:
    """流式计算md5"""
    md5 = hashlib.md5()
    size = 0
    with open(path, "rb") as file:
        while True:
            data = file.read(chunk_size)
            if not data:
                break
            size += len(data)
            md5.update(data)
    return md5.hexdigest(), size

class _FileSlice:
    """补丁文件只读切片"""
    __slots__ = ("_file", "_start", "_size", "_pos")

    def __init__(self, file: BinaryIO, start: int, size: int):
        if start < 0 or size < 0:
            raise KrpdiffError("bad slice: %d+%d" % (start, size))
        self._file = file
        self._start = start
        self._size = size
        self._pos = 0

    @property
    def remaining(self) -> int:
        return self._size - self._pos

    def read(self, size: int) -> bytes:
        size = min(size, self.remaining)
        if size <= 0:
            return b""
        self._file.seek(self._start + self._pos)
        data = self._file.read(size)
        if not data:
            raise KrpdiffError("patch data truncated")
        self._pos += len(data)
        return data

    def skip(self, size: int) -> int:
        step = min(size, self.remaining)
        self._pos += step
        return step

def _new_zstd_decompressor():
    """zstd增量解压对象"""
    try:
        from compression import zstd as _zstd  # Python 3.14+
    except ImportError:
        _zstd = None
    if _zstd is not None:
        try:
            return _zstd.ZstdDecompressor()
        except Exception:
            pass
    import importlib
    try:
        zstandard = importlib.import_module("zstandard")
    except ImportError as e:
        raise KrpdiffError("missing zstd module (compression.zstd or zstandard)") from e
    return zstandard.ZstdDecompressor()

class _BytesSlice:
    __slots__ = ("_data", "_start", "_size", "_pos")

    def __init__(self, data: bytes, start: int, size: int):
        self._data = data
        self._start = start
        self._size = min(size, max(len(data) - start, 0))
        self._pos = 0

    @property
    def remaining(self) -> int:
        return self._size - self._pos

    def read(self, size: int) -> bytes:
        size = min(size, self.remaining)
        if size <= 0:
            return b""
        data = self._data[self._start + self._pos:self._start + self._pos + size]
        self._pos += len(data)
        return data

    def skip(self, size: int) -> int:
        step = min(size, self.remaining)
        self._pos += step
        return step

class _Section:
    """patch的数据段"""
    __slots__ = ("_slice", "_raw", "_raw_left", "_decomp", "_decomp_eof",
                 "_needs_input", "_buf", "size", "consumed")

    def __init__(self, make_slice, offset: int, raw_size: int, compressed_size: int,
                 compress_type: str = "zstd"):
        self.size = raw_size
        self.consumed = 0
        self._buf = b""
        self._raw = compressed_size == 0
        if self._raw:
            self._slice = make_slice(offset, raw_size)
            self._decomp = None
            self._decomp_eof = True
            self._needs_input = False
            self._raw_left = raw_size
        else:
            if compress_type != "zstd":
                raise KrpdiffError("unsupported compression type: %r" % (compress_type,))
            self._slice = make_slice(offset, compressed_size)
            self._decomp = _new_zstd_decompressor()
            self._decomp_eof = False
            self._needs_input = True
            self._raw_left = raw_size

    @property
    def remaining(self) -> int:
        return self.size - self.consumed

    def _fill(self, want: int) -> None:
        while len(self._buf) < want and not self._decomp_eof:
            if self._needs_input:
                if self._slice.remaining <= 0:
                    raise KrpdiffError("compressed section truncated")
                chunk = self._slice.read(min(1 << 20, self._slice.remaining))
            else:
                chunk = b""
            out = self._decomp.decompress(chunk, max_length=max(want - len(self._buf), 1))
            self._needs_input = bool(getattr(self._decomp, "needs_input", False))
            if getattr(self._decomp, "eof", False):
                self._decomp_eof = True
            if not out:
                if self._needs_input and not chunk and self._slice.remaining <= 0:
                    raise KrpdiffError("compressed section truncated")
                if not self._needs_input and not chunk:
                    raise KrpdiffError("zstd decompress stalled")
            self._buf += out

    def read(self, size: int) -> bytes:
        size = min(size, self.remaining)
        if size <= 0:
            return b""
        if self._raw:
            left = min(size, self._raw_left)
            data = self._slice.read(left) if self._slice is not None else b""
            self._raw_left -= len(data)
        else:
            self._fill(size)
            data = self._buf[:size]
            self._buf = self._buf[len(data):]
        self.consumed += len(data)
        return data

    def read_exact(self, size: int) -> bytes:
        parts = []
        while size > 0:
            data = self.read(min(size, MEM_COPY_CHUNK))
            if not data:
                raise KrpdiffError("patch section exhausted (want %d more)" % size)
            parts.append(data)
            size -= len(data)
        return b"".join(parts)

    def skip(self, size: int) -> None:
        size = min(size, self.remaining)
        while size > 0:
            if self._raw:
                step = self._slice.skip(size) if self._slice is not None else 0
            else:
                self._fill(min(size, MEM_COPY_CHUNK))
                step = min(size, len(self._buf))
                self._buf = self._buf[step:]
            if step <= 0:
                raise KrpdiffError("patch section exhausted while skipping")
            self.consumed += step
            size -= step

    @property
    def at_end(self) -> bool:
        return len(self._buf) == 0 and self.remaining == 0 and self._slice.remaining == 0

class _ByteReader:
    __slots__ = ("_section", "_buf", "_pos")

    def __init__(self, section: _Section):
        self._section = section
        self._buf = b""
        self._pos = 0

    def _fill(self, want: int = 1) -> None:
        if len(self._buf) - self._pos >= want:
            return
        if self._pos:
            self._buf = self._buf[self._pos:]
            self._pos = 0
        if want > 64:  # 大块数据直接返回，不进buffer
            return
        while len(self._buf) < want:
            data = self._section.read(max(want - len(self._buf), 1 << 16))
            if not data:
                return
            self._buf += data

    @property
    def at_end(self) -> bool:
        self._fill(1)
        return self._pos >= len(self._buf) and self._section.at_end

    def peek_byte(self) -> int:
        self._fill(1)
        if self._pos >= len(self._buf):
            raise KrpdiffError("patch stream exhausted")
        return self._buf[self._pos]

    def read_byte(self) -> int:
        value = self.peek_byte()
        self._pos += 1
        return value

    def read_exact(self, size: int) -> bytes:
        self._fill(min(size, 64))
        head = self._buf[self._pos:self._pos + size]
        if len(head) == size:
            self._pos += size
            return head
        parts = [head]
        self._pos += len(head)
        self._buf = b""
        self._pos = 0
        remain = size - len(head)
        if remain > 0:
            parts.append(self._section.read_exact(remain))
        return b"".join(parts)

    def read_uint(self, tag_bits: int = 0) -> Tuple[int, int]:
        first = self.read_byte()
        value = first & ((1 << (7 - tag_bits)) - 1)
        if first & (1 << (7 - tag_bits)):
            while True:
                code = self.read_byte()
                value = (value << 7) | (code & 0x7F)
                if not (code & 0x80):
                    break
        tag = (first >> (8 - tag_bits)) if tag_bits else 0
        return value, tag

class OldStream:
    """拼接旧文件"""
    __slots__ = ("_entries", "_handles", "size")

    def __init__(self, entries: List[Tuple[str, int]]):
        self._entries = []
        self._handles: Dict[str, BinaryIO] = {}
        offset = 0
        for path, size in entries:
            if size < 0:
                raise KrpdiffError("bad old file size: %r" % (path,))
            self._entries.append((offset, size, str(path)))
            offset += size
        self.size = offset

    @classmethod
    def from_refs(cls, refs, base_dir, verify: Optional[Callable[[str, int], int]] = None) -> "OldStream":
        """按 old_ref_files 构造"""
        entries: List[Tuple[str, int]] = []
        for ref in refs:
            path = base_dir / ref.old_path if base_dir is not None else ref.old_path
            size = None
            if verify is not None:
                size = verify(str(path), ref.old_size)
            else:
                size = ref.old_size
                if size is None:
                    size = _file_size(str(path))
            if ref.old_size is not None and size != ref.old_size:
                raise KrpdiffError("旧文件大小错误: %s 实际 %d 期望 %d" % (path, size, ref.old_size))
            entries.append((str(path), int(size)))
        return cls(entries)

    def _handle(self, path: str) -> BinaryIO:
        handle = self._handles.get(path)
        if handle is None:
            handle = open(path, "rb")
            if len(self._handles) >= 4:  # less handle
                old_path, old_handle = next(iter(self._handles.items()))
                old_handle.close()
                self._handles.pop(old_path)
            self._handles[path] = handle
        return handle

    def read_at(self, pos: int, size: int) -> bytes:
        if pos < 0 or size < 0 or pos + size > self.size:
            raise KrpdiffError("old stream read out of range: %d+%d/%d" % (pos, size, self.size))
        parts: List[bytes] = []
        remain = size
        cursor = pos
        for offset, part_size, path in self._entries:
            if remain <= 0:
                break
            if cursor >= offset + part_size or cursor < offset:
                continue
            inner = cursor - offset
            step = min(part_size - inner, remain)
            handle = self._handle(path)
            handle.seek(inner)
            data = handle.read(step)
            if len(data) != step:
                raise KrpdiffError("old file read error: %s" % path)
            parts.append(data)
            cursor += step
            remain -= step
        if remain:
            raise KrpdiffError("old stream read incomplete")
        return parts[0] if len(parts) == 1 else b"".join(parts)

    def close(self) -> None:
        for handle in self._handles.values():
            handle.close()
        self._handles.clear()

def _file_size(path: str) -> int:
    import os
    return os.path.getsize(path)

def _add_table(value: int) -> bytes:
    table = _ADD_TABLES.get(value)
    if table is None:
        table = bytes((i + value) & 0xFF for i in range(256))
        _ADD_TABLES[value] = table
    return table

def _spread_bytes(value: int, lanes: int) -> int:
    step = lanes >> 1
    while step >= 1:
        key = (lanes, step)
        mask = _SPREAD_MASKS.get(key)
        if mask is None:
            pattern = b"\xff" * step + b"\x00" * step
            mask = int.from_bytes(pattern * (lanes // step), "little")
            _SPREAD_MASKS[key] = mask
        value = (value | (value << (8 * step))) & mask
        step >>= 1
    return value

def _add_bytes_lanes(dst: bytes, src: bytes) -> bytes:
    size = len(dst)
    out = bytearray(size)
    for offset in range(0, size, _ADD_CHUNK):
        left = dst[offset:offset + _ADD_CHUNK]
        right = src[offset:offset + _ADD_CHUNK]
        span = len(left)
        lanes = 1
        while lanes < span:
            lanes <<= 1
        pad = b"\x00" * (lanes - span)
        x = _spread_bytes(int.from_bytes(left + pad, "little"), lanes)
        y = _spread_bytes(int.from_bytes(right + pad, "little"), lanes)
        total = x + y  # <= 511
        out[offset:offset + span] = total.to_bytes(2 * lanes, "little")[0:2 * lanes:2][:span]
    return bytes(out)

def _add_bytes(dst: bytes, src: bytes) -> bytes:
    if not src:
        return dst
    if len(src) <= 4096:
        return bytes((a + b) & 0xFF for a, b in zip(dst, src))
    return _add_bytes_lanes(dst, src)

class _ByteRle:
    """HDiffPatch RLE"""
    __slots__ = ("_ctrl", "_code", "_set_len", "_set_value", "_copy_len")

    def __init__(self, ctrl: _ByteReader, code: _ByteReader):
        self._ctrl = ctrl
        self._code = code
        self._set_len = 0
        self._set_value = 0
        self._copy_len = 0

    def _next(self) -> None:
        if self._ctrl.at_end:
            raise KrpdiffError("rle control stream exhausted")
        type_bits = self._ctrl.peek_byte() >> 6
        length, _ = self._ctrl.read_uint(2)
        length += 1
        if type_bits == 0:  # rle0
            self._set_len = length
            self._set_value = 0
        elif type_bits == 1:  # rle255
            self._set_len = length
            self._set_value = 255
        elif type_bits == 2:  # rle <value>
            self._set_value = self._code.read_byte()
            self._set_len = length
        else:  # unrle
            self._copy_len = length

    def consume(self, size: int, dst: Optional[bytearray] = None) -> None:
        done = 0
        while done < size:
            if self._set_len == 0 and self._copy_len == 0:
                self._next()
            if self._set_len:
                step = min(self._set_len, size - done)
                if dst is not None and self._set_value:
                    dst[done:done + step] = bytes(dst[done:done + step]).translate(
                        _add_table(self._set_value))
                self._set_len -= step
                done += step
            else:
                step = min(self._copy_len, size - done)
                data = self._code.read_exact(step)
                if dst is not None:
                    dst[done:done + step] = _add_bytes(bytes(dst[done:done + step]), data)
                self._copy_len -= step
                done += step

    @property
    def at_end(self) -> bool:
        return (self._set_len == 0 and self._copy_len == 0
                and self._ctrl.at_end and self._code.at_end)

class PatchWriter:
    """写补丁输出文件：先写临时文件，校验通过后再替换目标文件"""
    def __init__(self, path, expect_size: Optional[int] = None,
                 expect_md5: Optional[str] = None):
        from pathlib import Path
        self.path = Path(path)
        self.expect_size = expect_size
        self.expect_md5 = expect_md5
        self.written = 0
        self.md5 = hashlib.md5()
        self._temp = self.path.with_name(self.path.name + ".patchtmp")
        self._file = None

    def __enter__(self) -> "PatchWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._temp, "wb")
        return self

    def write(self, data: bytes) -> None:
        self._file.write(data)
        self.md5.update(data)
        self.written += len(data)

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._file is not None:
            self._file.close()
            self._file = None
        if exc_type is not None:
            self.abort()
            return False
        if self.expect_size is not None and self.written != self.expect_size:
            self.abort()
            raise KrpdiffError("补丁输出大小错误: %s 实际 %d 期望 %d"
                               % (self.path, self.written, self.expect_size))
        digest = self.md5.hexdigest()
        if self.expect_md5 and digest != self.expect_md5:
            self.abort()
            raise KrpdiffError("补丁输出 MD5 错误: %s 实际 %s 期望 %s"
                               % (self.path, digest, self.expect_md5))
        self._temp.replace(self.path)
        return False

    def abort(self) -> None:
        try:
            if self._temp.exists():
                self._temp.unlink()
        except OSError:
            pass

@dataclass
class OldRefFile:
    old_path: str
    old_size: Optional[int] = None

@dataclass
class NewRefFile:
    new_path: str
    new_size: int

@dataclass
class CopyFile:
    old_path: str
    new_path:str

@dataclass
class _HDiffHead:
    """HDIFF13 diff header"""
    new_data_size: int
    old_data_size: int
    cover_count: int
    cover_buf_size: int
    compress_cover_buf_size: int
    rle_ctrl_buf_size: int
    compress_rle_ctrl_buf_size: int
    rle_code_buf_size: int
    compress_rle_code_buf_size: int
    new_data_diff_size: int
    compress_new_data_diff_size: int
    compress_type: str = "zstd"
    head_end_pos: int = 0


@dataclass
class DirPatch:
    """解析后的目录补丁"""
    old_ref_files: Tuple[OldRefFile, ...] # 旧引用文件，旧流拼接顺序
    new_ref_files: Tuple[NewRefFile, ...] # 新引用文件
    copy_files: Tuple[CopyFile, ...] # 复制类文件
    execute_files: Tuple[str, ...] # 需要补可执行位的新文件路径
    new_dirs: Tuple[str, ...] # 新目录
    empty_files: Tuple[str, ...] # 空文件
    old_stream_size: int # 旧引用文件合并总大小，用于校验
    new_stream_size: int # 新引用文件合并总大小
    _data: bytes = field(default=b"", repr=False) # 原始补丁数据
    _source: Optional[BinaryIO] = field(default=None, repr=False)
    _hdiff_offset: int = field(default=0, repr=False) # 差分流起始偏移

    def _make_slice(self, offset: int, size: int):
        if self._source is not None:
            return _FileSlice(self._source, offset, size)
        return _BytesSlice(self._data, offset, size)

    def _read_hdiff_head(self) -> "_HDiffHead":
        raw = self._read_at(self._hdiff_offset, 256)
        end = raw.find(b"&")
        if end < 0:
            raise KrpdiffError("broken hdiff head: missing version")
        version = raw[:end]
        if version != HDIFF_STREAM_VERSION:
            raise KrpdiffError("unsupported hdiff stream version: %r" % (version,))
        stop = raw.find(b"\x00", end + 1)
        if stop < 0:
            raise KrpdiffError("broken hdiff head: missing compress type")
        compress_type = raw[end + 1:stop].decode("ascii", "replace")
        pos = stop + 1
        values: List[int] = []
        for _ in range(11):
            value, pos, _ = _read_uint(raw, pos)
            values.append(value)
        head = _HDiffHead(*values)
        head.compress_type = compress_type
        head.head_end_pos = self._hdiff_offset + pos
        return head

    def iter_new_stream(self, old_stream) -> Iterator[bytes]:
        head = self._read_hdiff_head()
        if head.new_data_size != self.new_stream_size:
            raise KrpdiffError("新版数据大小错误: 补丁 %d, 头部 %d"
                               % (self.new_stream_size, head.new_data_size))
        if head.old_data_size != self.old_stream_size:
            raise KrpdiffError("旧版数据大小错误: 补丁 %d, 头部 %d"
                               % (self.old_stream_size, head.old_data_size))
        if old_stream.size != head.old_data_size:
            raise KrpdiffError("旧文件总大小错误: 实际 %d, 补丁要求 %d"
                               % (old_stream.size, head.old_data_size))
        pos = head.head_end_pos
        cover = _Section(self._make_slice, pos, head.cover_buf_size,
                         head.compress_cover_buf_size, head.compress_type)
        pos += head.compress_cover_buf_size or head.cover_buf_size
        rle_ctrl = _Section(self._make_slice, pos, head.rle_ctrl_buf_size,
                            head.compress_rle_ctrl_buf_size, head.compress_type)
        pos += head.compress_rle_ctrl_buf_size or head.rle_ctrl_buf_size
        rle_code = _Section(self._make_slice, pos, head.rle_code_buf_size,
                            head.compress_rle_code_buf_size, head.compress_type)
        pos += head.compress_rle_code_buf_size or head.rle_code_buf_size
        new_data = _Section(self._make_slice, pos, head.new_data_diff_size,
                            head.compress_new_data_diff_size, head.compress_type)
        covers = _ByteReader(cover)
        rle = _ByteRle(_ByteReader(rle_ctrl), _ByteReader(rle_code))
        old_pos_back = 0
        new_pos = 0

        def copy_new_data(size: int) -> Iterator[bytes]:
            left = size
            while left > 0:
                data = new_data.read(min(left, MEM_COPY_CHUNK))
                if not data:
                    raise KrpdiffError("newDataDiff error")
                left -= len(data)
                yield data

        for _ in range(head.cover_count):
            delta, sign = covers.read_uint(1)
            old_pos = old_pos_back - delta if sign else old_pos_back + delta
            gap, _ = covers.read_uint()
            length, _ = covers.read_uint()
            new_pos += gap
            if new_pos + length > head.new_data_size:
                raise KrpdiffError("cover oob new data")
            if old_pos + length > head.old_data_size:
                raise KrpdiffError("cover oob old data")
            if new_pos > head.new_data_size:
                raise KrpdiffError("cover oob")
            cur = new_pos - gap
            if gap > 0:
                yield from copy_new_data(gap)
                rle.consume(gap)
            left = length
            read_pos = old_pos
            while left > 0:
                step = min(left, MEM_COPY_CHUNK)
                buf = bytearray(old_stream.read_at(read_pos, step))
                rle.consume(step, buf)
                yield bytes(buf)
                read_pos += step
                left -= step
            new_pos += length
            old_pos_back = old_pos + length
        if new_pos < head.new_data_size:
            tail = head.new_data_size - new_pos
            yield from copy_new_data(tail)
            rle.consume(tail)
            new_pos += tail
        if new_pos != head.new_data_size:
            raise KrpdiffError("length error: %d != %d" % (new_pos, head.new_data_size))
        if new_data.remaining or not covers.at_end or not rle.at_end:
            raise KrpdiffError("patch data still remain")

    def apply_streaming(self, old_stream, open_writer) -> List[str]:
        """按文件流式还原"""
        produced: List[str] = []
        chunks = self.iter_new_stream(old_stream)
        carry = b""
        exhausted = False
        for ref in self.new_ref_files:
            remain = ref.new_size
            with open_writer(ref.new_path, ref.new_size) as writer:
                while remain > 0:
                    if not carry:
                        try:
                            carry = next(chunks)
                        except StopIteration:
                            raise KrpdiffError("stream error") from None
                    take = carry[:remain]
                    carry = carry[remain:]
                    writer.write(take)
                    remain -= len(take)
            produced.append(ref.new_path)
        for _ in chunks:
            exhausted = False
            break
        if carry:
            raise KrpdiffError("stream length oob")
        return produced

    def _read_at(self, offset: int, size: int) -> bytes:
        if self._source is not None:
            self._source.seek(offset)
            data = self._source.read(size)
        else:
            data = self._data[offset:offset + size]
        if len(data) != size:
            raise KrpdiffError("patch data eroor(%d/%d)" % (len(data), size))
        return data

def parse_dir_patch(source: Union[bytes, bytearray, memoryview, BinaryIO]) -> DirPatch:
    """解析 krpdiff 目录补丁
    """
    fileobj = None
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
    else:
        fileobj = source
        if not hasattr(fileobj, "seek") or not hasattr(fileobj, "read"):
            raise TypeError("source must be bytes or seekable file object")
        fileobj.seek(0)
        data = fileobj.read(HEAD_READ_LIMIT)
        if not data:
            raise KrpdiffError("empty patch source")
    if not looks_like_dir_patch(data):
        raise KrpdiffError("not dir patch")
    pos = 0
    # header "HDIFF19&{compressType}&{checksumType}\0"
    end = data.find(b"&", pos)
    if end < 0:
        raise KrpdiffError("broken header: missing version")
    version = data[pos:end].decode("ascii", "replace")
    if data[pos:end] != MAGIC_VERSION:
        raise KrpdiffError("unsupported patch version: %r" % (version,))
    pos = end + 1
    end = data.find(b"&", pos)
    if end < 0:
        raise KrpdiffError("broken header: missing compression type")
    compress_type = data[pos:end].decode("ascii", "replace")
    pos = end + 1
    end = data.find(b"\0", pos)
    if end < 0:
        raise KrpdiffError("broken header: missing check terminator")
    checksum_type = data[pos:end].decode("ascii", "replace")
    pos = end + 1
    def rd(tag_bits: int = 0) -> Tuple[int, int]:
        nonlocal pos
        value, pos, tag = _read_uint(data, pos, tag_bits)
        return value, tag
    # 目录补丁头部字段
    # 参考HDiffPatch dir_patch.c
    rd() # oldPathIsDir
    rd() # newPathIsDir
    old_path_count, _ = rd()
    old_path_sum_size, _ = rd()
    new_path_count, _ = rd()
    new_path_sum_size, _ = rd()
    old_ref_count, _ = rd()
    saved_old_ref_size, _ = rd() # 旧引用文件合并总大小
    new_ref_count, _ = rd()
    saved_new_ref_size, _ = rd() # 新引用文件合并总大小
    same_pair_count, _ = rd()
    rd()  # sameFileSize
    new_execute_count, _ = rd()
    rd()  # privateReservedDataSize, kuro似乎写错了
    private_extern_size, _ = rd()
    extern_size, _ = rd()
    head_data_size, _ = rd()
    head_data_compressed_size, _ = rd()
    checksum_byte_size, _ = rd()
    # 4组校验和
    pos += checksum_byte_size * 4
    if pos > len(data):
        raise KrpdiffError("patch header truncation")
    # 数据区偏移：目录头 -> 私有数据 -> 外部数据 -> 差分流
    head_data_offset = pos
    pos += head_data_compressed_size if head_data_compressed_size else head_data_size
    pos += private_extern_size
    pos += extern_size
    hdiff_offset = pos
    if hdiff_offset >= len(data):
        raise KrpdiffError("patch data truncated: missing diff stream")
    # 解压并解析目录头
    head_raw_size = head_data_compressed_size or head_data_size
    if fileobj is not None and head_data_offset + head_raw_size > len(data):
        fileobj.seek(0)
        data = fileobj.read(head_data_offset + head_raw_size)
        if len(data) < head_data_offset + head_raw_size:
            raise KrpdiffError("patch head truncated")
    head_raw = data[head_data_offset:head_data_offset + head_raw_size]
    head = _decompress_dir_head(head_raw, head_data_size, head_data_compressed_size, compress_type)
    path_sum_size = old_path_sum_size + new_path_sum_size
    if path_sum_size > len(head):
        raise KrpdiffError("dir header truncated: path table oob")
    strings: List[str] = []
    blob = head[:path_sum_size]
    p = 0
    while p < path_sum_size:
        q = blob.find(b"\0", p)
        if q < 0:
            raise KrpdiffError("path table missing terminator")
        strings.append(blob[p:q].decode("utf-8"))
        p = q + 1
    if len(strings) != old_path_count + new_path_count:
        raise KrpdiffError(
            "路径数量错误: 实际 %d, 期望 %d" % (len(strings), old_path_count + new_path_count)
        )
    old_paths = tuple(strings[:old_path_count])
    new_paths = tuple(strings[old_path_count:])
    # 文件索引与列表
    p = path_sum_size
    old_ref_indices, p = _read_inc_list(head, p, old_ref_count, old_path_count, "oldRefList")
    new_ref_indices, p = _read_inc_list(head, p, new_ref_count, new_path_count, "newRefList")
    p_after_new_ref = p
    # krpdiff多一份表
    old_ref_sizes: Optional[List[int]] = None
    new_ref_sizes: Optional[List[int]] = None
    try:
        cand_old, p2 = _read_size_list(head, p, old_ref_count)
        cand_new, p3 = _read_size_list(head, p2, new_ref_count)
        if sum(cand_old) == saved_old_ref_size and sum(cand_new) == saved_new_ref_size:
            old_ref_sizes, new_ref_sizes, p = cand_old, cand_new, p3
    except KrpdiffError:
        pass
    if new_ref_sizes is None:
        p = p_after_new_ref
        new_ref_sizes, p = _read_size_list(head, p, new_ref_count)
        if sum(new_ref_sizes) != saved_new_ref_size:
            raise KrpdiffError("size mismatch")
    same_pairs: List[Tuple[int, int]] = [] # (newIndex, oldIndex)
    back_new = -1
    back_old = -1
    for _ in range(same_pair_count):
        inc_new, p, _ = _read_uint(head, p)
        back_new += 1 + inc_new
        if not (-1 < back_new < new_path_count):
            raise KrpdiffError("new index oob")
        inc_old, p, sign = _read_uint(head, p, 1)
        back_old = back_old + 1 + inc_old if sign == 0 else back_old + 1 - inc_old
        if not (-1 < back_old < old_path_count):
            raise KrpdiffError("new index oob")
        same_pairs.append((back_new, back_old))
    execute_indices, p = _read_inc_list(head, p, new_execute_count, new_path_count, "newExecuteList")
    # 跳过私有数据尾部
    old_ref_files: List[OldRefFile] = []
    for index, old_index in enumerate(old_ref_indices):
        old_rel = old_paths[old_index]
        if (not old_rel) or old_rel.endswith("/"):
            raise KrpdiffError("old ref path error: %r" % (old_rel,))
        old_size = old_ref_sizes[index] if old_ref_sizes is not None else None
        old_ref_files.append(OldRefFile(old_rel, old_size))
    new_ref_files: List[NewRefFile] = []
    for new_index, size in zip(new_ref_indices, new_ref_sizes):
        new_rel = new_paths[new_index]
        if (not new_rel) or new_rel.endswith("/"):
            raise KrpdiffError("new ref path error: %r" % (new_rel,))
        new_ref_files.append(NewRefFile(new_rel, size))
    copy_files = tuple(
        CopyFile(old_paths[old_index], new_paths[new_index]) for new_index, old_index in same_pairs
    )
    ref_new_set = set(new_ref_indices)
    copy_new_set = {pair[0] for pair in same_pairs}
    new_dirs: List[str] = []
    empty_files: List[str] = []
    for index, rel in enumerate(new_paths):
        if (index in ref_new_set) or (index in copy_new_set):
            continue
        if (rel == "") or rel.endswith("/"):
            new_dirs.append(rel)
        else:
            empty_files.append(rel)
    execute_files = tuple(new_paths[i] for i in execute_indices)
    return DirPatch(
        old_ref_files=tuple(old_ref_files),
        new_ref_files=tuple(new_ref_files),
        copy_files=copy_files,
        execute_files=execute_files,
        new_dirs=tuple(new_dirs),
        empty_files=tuple(empty_files),
        old_stream_size=saved_old_ref_size,
        new_stream_size=saved_new_ref_size,
        _data=data,
        _source=fileobj,
        _hdiff_offset=hdiff_offset,
    )
def looks_like_dir_patch(data) -> bool:
    """快速判断 krpdiff/HDIFF19 类patch"""
    return (
        isinstance(data, (bytes, bytearray, memoryview)) and bytes(data[:7]) == MAGIC_VERSION
    )

def _read_uint(data: bytes, pos: int, tag_bits: int = 0) -> Tuple[int, int, int]:
    """读取 HDiffPatch 变长整数，对应 ``unpackUIntWithTag``"""
    if pos >= len(data):
        raise KrpdiffError("data oob")
    first = data[pos]
    pos += 1
    value = first & ((1 << (7 - tag_bits)) - 1)
    if first & (1 << (7 - tag_bits)):
        while True:
            if pos >= len(data):
                raise KrpdiffError("data oob")
            code = data[pos]
            pos += 1
            value = (value << 7) | (code & 0x7F)
            if not (code & 0x80):
                break
    tag = (first >> (8 - tag_bits)) if tag_bits else 0
    return value, pos, tag

def _read_inc_list(data: bytes, pos: int, count: int, end_value: int, what: str) -> Tuple[List[int], int]:
    """读取递增索引表"""
    out: List[int] = []
    back = -1
    for _ in range(count):
        inc, pos, _ = _read_uint(data, pos)
        back += 1 + inc
        if not (-1 < back < end_value):
            raise KrpdiffError("%s index oob" % what)
        out.append(back)
    return out, pos

def _read_size_list(data: bytes, pos: int, count: int) -> Tuple[List[int], int]:
    """读取文件size表"""
    out: List[int] = []
    for _ in range(count):
        value, pos, _ = _read_uint(data, pos)
        out.append(value)
    return out, pos

def _decompress_dir_head(raw: bytes, uncompressed_size: int, compressed_size: int, compress_type: str) -> bytes:
    if compressed_size == 0:
        return raw
    if compress_type != "zstd":
        raise KrpdiffError("unsupported compression type: %r" % (compress_type,))
    out = _zstd_decompress(raw, uncompressed_size)
    if len(out) != uncompressed_size:
        raise KrpdiffError(
            "目录头解压后大小错误: 实际 %d 字节, 期望 %d 字节" % (len(out), uncompressed_size)
        )
    return out

def _zstd_decompress(data: bytes, max_size: int) -> bytes:
    try:
        from compression import zstd as _zstd # Python 3.14+
    except ImportError:
        _zstd = None
    if _zstd is not None:
        try:
            return _zstd.decompress(data)
        except Exception:
            pass
    try:
        import zstandard
    except ImportError as e:
        raise KrpdiffError("missing zstandard module") from e
    return zstandard.ZstdDecompressor().decompress(data, max_output_size=max_size)
