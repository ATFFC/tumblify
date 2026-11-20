# SPDX-FileCopyrightText: 2026 CG Corner addons
# SPDX-License-Identifier: GPL-3.0-only
# Tumblify is distributed under GNU GPL version 3. See LICENSE.

"""Owned disk-cache files for Emitter Bake; independent of Blender's API."""
import ctypes
import json
import math
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
from array import array

HEADER = struct.Struct('<8sIIIIfII28x')
MAGIC = b'PUPCACH1'


def estimated_cache_bytes(bodies, frames):
    # Two guard frames cover rounding in the native output-frame calculation.
    return 64 + max(0, int(bodies)) * (max(0, int(frames)) + 2) * 28


def select_backend(mode, bodies, frames, memory_limit_mb=256):
    if mode not in {'AUTO', 'MEMORY', 'DISK'}:
        raise ValueError('Unknown Emitter Bake cache mode')
    estimate = estimated_cache_bytes(bodies, frames)
    backend = ('DISK' if estimate > max(1, memory_limit_mb) * 1024 * 1024 else 'MEMORY') if mode == 'AUTO' else mode
    return backend, estimate


def read_header(path):
    path = Path(path)
    with path.open('rb') as stream:
        raw = stream.read(HEADER.size)
    if len(raw) != HEADER.size:
        raise RuntimeError('Incomplete Tumblify bake cache header')
    magic, version, frames, bodies, block, fps, components, complete = HEADER.unpack(raw)
    if (magic != MAGIC or version != 1 or components != 7 or complete != 1
            or not (0 < frames <= 2**31-1 and 0 < bodies <= 2**31-1 and 0 < block <= 32)
            or not math.isfinite(fps) or fps <= 0):
        raise RuntimeError('Unsupported or incomplete Tumblify bake cache')
    if path.stat().st_size != HEADER.size + frames * bodies * 28:
        raise RuntimeError('Truncated Tumblify bake cache data')
    return frames, bodies, block, fps


class DiskTransformCache:
    """Read retained cache tracks without loading the entire simulation into RAM."""
    def __init__(self, path):
        self.path = Path(path)
        self.frame_count, self.body_count, self.block_frames, self.fps = read_header(self.path)
        self._stream = self.path.open('rb')

    def body_track(self, index):
        if self._stream is None:
            raise RuntimeError('Transform cache is closed')
        index = int(index)
        if not 0 <= index < self.body_count:
            raise IndexError('Body index is outside the bake cache')
        out = (ctypes.c_float * (self.frame_count * 7))()
        for first in range(0, self.frame_count, self.block_frames):
            count = min(self.block_frames, self.frame_count - first)
            offset = HEADER.size + first * self.body_count * 28 + index * count * 28
            self._stream.seek(offset)
            raw = self._stream.read(count * 28)
            if len(raw) != count * 28:
                raise RuntimeError('Truncated Tumblify body track')
            if sys.byteorder != 'little':
                values = array('f'); values.frombytes(raw); values.byteswap(); raw = values.tobytes()
            ctypes.memmove(ctypes.addressof(out) + first * 28, raw, len(raw))
        return out

    def close(self):
        if self._stream is not None:
            self._stream.close(); self._stream = None

    def __del__(self):
        stream = getattr(self, '_stream', None)
        if stream is not None:
            stream.close()


class BakeCacheFiles:
    """Own one unique run directory; never remove unrelated files or directories."""
    def __init__(self, root, estimated_bytes, metadata):
        root = Path(root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        required = int(estimated_bytes) + 64 * 1024 * 1024
        if shutil.disk_usage(root).free < required:
            raise RuntimeError(f'Not enough free space for Emitter Bake cache ({required / 1024**3:.2f} GiB required)')
        self.directory = Path(tempfile.mkdtemp(prefix='bake_', dir=str(root)))
        self.partial = self.directory / 'transforms.partial'
        self.completed = self.directory / 'transforms.pucache'
        self.metadata = dict(metadata, format='TumblifyBakeCache', format_version=1,
                             phase='SIMULATING', data_file=self.partial.name)
        try:
            self._write_metadata()
        except Exception:
            self.discard()
            raise

    def _write_metadata(self):
        temporary = self.directory / 'manifest.json.tmp'
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(self.metadata, stream, indent=2, ensure_ascii=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, self.directory / 'manifest.json')

    def mark_simulated(self, frames, bodies, fps):
        actual_frames, actual_bodies, _, actual_fps = read_header(self.partial)
        if actual_frames != frames or actual_bodies != bodies or abs(actual_fps-fps) > max(1e-4, abs(fps)*1e-6):
            raise RuntimeError('Native bake cache metadata does not match the job')
        if len(self.metadata.get('sources', ())) != bodies or len(self.metadata.get('instance_scales', ())) != bodies:
            raise RuntimeError('Bake source metadata does not match the body count')
        self.metadata.update(phase='SIMULATED', frames=frames, bodies=bodies, fps=fps)
        self._write_metadata()

    def record_objects(self, names):
        self.metadata['result_objects'] = list(names)
        self.metadata['phase'] = 'WRITING_KEYFRAMES'
        self._write_metadata()

    def finish(self, keep, cancelled):
        # Call only after the worker and every native/Python cache reader are closed.
        ready = False
        if self.partial.exists():
            try:
                frames, bodies, _, fps = read_header(self.partial)
                self.metadata.update(frames=frames, bodies=bodies, fps=fps)
                ready = True
            except (OSError, RuntimeError):
                pass
        if keep and ready:
            os.replace(self.partial, self.completed)
            self.metadata.update(data_file=self.completed.name,
                                 phase='KEYFRAME_WRITE_CANCELLED_OR_FAILED' if cancelled else 'COMPLETE')
            self._write_metadata()
            return str(self.completed)
        self.discard()
        return None

    def discard(self):
        for name in ('transforms.partial', 'transforms.pucache', 'manifest.json.tmp', 'manifest.json'):
            try: (self.directory / name).unlink()
            except FileNotFoundError: pass
        # Refuse recursive deletion; an unexpected user file must survive.
        try: self.directory.rmdir()
        except FileNotFoundError: pass
