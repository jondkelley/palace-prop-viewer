#!/usr/bin/env python3
"""
Palace Prop Viewer
==================
Opens a Palace .prp asset file, lists all 'Prop' assets, and previews
each one positioned on an avatar silhouette using the hOffset/vOffset
worn-position data embedded in each PropHeader.

Usage:
    python3 palace_prop_viewer.py [file.prp]

Requires: Python 3.8+, tkinter (stdlib)
Optional: Pillow (pip install Pillow) — enables smoother scaling/display
"""

import struct
import sys
import os
import io
import colorsys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from PIL import Image, ImageTk
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

# ---------------------------------------------------------------------------
# Constants matching the Palace source (u-user.h)
# ---------------------------------------------------------------------------
FACE_WIDTH  = 44
FACE_HEIGHT = 44
PROP_WIDTH  = 44
PROP_HEIGHT = 44

RT_PROP     = 0x50726F70  # b'Prop'
RT_FAVE     = 0x46617665  # b'Fave'

PF_FACE     = 0x02
PF_GHOST    = 0x04
PF_RARE     = 0x08
PF_ANIMATE  = 0x10
PF_PALINDROME = 0x20

ASSET_FILE_HEADER_SIZE = 16   # 4 × int32
ASSET_MAP_HEADER_SIZE  = 24   # 6 × int32
ASSET_TYPE_REC_SIZE    = 12   # 3 × int32
ASSET_REC_SIZE         = 32   # 8 × int32
PROP_HEADER_SIZE       = 12   # 6 × int16

# ---------------------------------------------------------------------------
# Mac 8-bit System Palette (256 colours)
# ---------------------------------------------------------------------------
# The classic Mac 8-bit system palette is built from a 6×6×6 RGB cube
# (216 colours, entries 0–215 below) plus a 40-step grey ramp. Entry 0 is
# white, entry 255 is black — the remainder fill in as Apple defined them.
# This matches the standard clut ID 8 used by Palace's prop colour indices.

def _build_mac_palette() -> list:
    """
    Return exactly 256 (R,G,B) tuples matching the classic Mac 8-bit system
    palette (clut ID 8, System 7 era).

    Layout:
      entry   0         : white (255,255,255)
      entries 1 – 214   : 6×6×6 RGB cube, R outer / G middle / B inner,
                          levels = {255,204,153,102,51,0} high→low,
                          skipping white (already entry 0) and black (entry 255)
      entries 215 – 254 : 40 uniform greys, light→dark
      entry 255         : black (0,0,0)
    """
    levels = [0xFF, 0xCC, 0x99, 0x66, 0x33, 0x00]
    palette = []

    # Entry 0: white
    palette.append((255, 255, 255))

    # Entries 1–214: 6×6×6 RGB cube (216 colours minus white and black = 214)
    for r in levels:
        for g in levels:
            for b in levels:
                if (r, g, b) == (255, 255, 255):
                    continue  # already entry 0
                if (r, g, b) == (0, 0, 0):
                    continue  # will be entry 255
                palette.append((r, g, b))

    # Entries 215–254: exactly 40 greys, light → dark.
    # The Mac system CLUT stores these at exact multiples of 6:
    #   246, 240, 234, … 12   (246 − i*6  for i in 0..39)
    for i in range(40):
        v = 246 - i * 6
        palette.append((v, v, v))

    # Entry 255: black
    palette.append((0, 0, 0))

    assert len(palette) == 256, f'Palette length error: {len(palette)}'
    return palette

MAC_PALETTE = _build_mac_palette()

# ---------------------------------------------------------------------------
# Face colorisation  (mirrors InitRoomColors / DrawFacePixmap in source)
# ---------------------------------------------------------------------------
# Palace applies a per-user hue-rotation to face props at draw time via
# cTrans[colorNbr * 256 + paletteIndex].  The transform:
#   1. Extracts perceived luminance from the raw palette colour
#      (weights: R×0.260 + G×0.391 + B×0.173, matching the source)
#   2. Assigns full saturation and the chosen hue, keeping that luminance
#   3. Converts back to RGB
# Without this the raw palette indices look garish because face sprites are
# painted in arbitrary base colours intended only as a luminance map.

# Palace face-colour presets — hues derived from InitRoomColors in the source:
#   hue_deg = faceColorIndex / 256 * 360  (NbrColors = 256, step ≈ 1.41°/index)
# Index values are chosen to match the visual colours in the original client.
FACE_COLOR_PRESETS = [
    ('Default (flesh)', 15),   # idx ~11  — warm peach/skin
    ('Red',              0),   # idx   0
    ('Orange',          32),   # idx ~23
    ('Yellow',          65),   # idx ~46  — yellow-green, matches original
    ('Green',          120),   # idx ~85
    ('Teal',           170),   # idx ~121
    ('Blue',           210),   # idx ~149
    ('Purple',         270),   # idx ~192
    ('Pink',           330),   # idx ~234
    ('Raw palette',     -1),   # -1 = no colorisation
]

def colorize_face_pixel(r: int, g: int, b: int, hue_deg: float) -> tuple:
    """
    Apply Palace-style face colourisation to a single (r,g,b) source pixel.
    Returns a new (r,g,b) tuple.
    hue_deg: 0–360 target hue.
    """
    # Perceived luminance with Palace's weights (R·0.260 + G·0.391 + B·0.173)
    # Sum of weights = 0.824; normalise to 0–1.
    L = (r * 0.260 + g * 0.391 + b * 0.173) / (255.0 * 0.824)
    L = max(0.0, min(1.0, L))
    h = (hue_deg % 360) / 360.0
    # colorsys.hls_to_rgb(h, l, s) — full saturation, preserve luminance
    nr, ng, nb = colorsys.hls_to_rgb(h, L, 1.0)
    return (int(nr * 255), int(ng * 255), int(nb * 255))


def apply_face_colorization(rgba: list, hue_deg: float) -> list:
    """
    Return a new 2-D RGBA pixel list with face colourisation applied.
    Transparent pixels are left as-is.
    """
    result = []
    for row in rgba:
        new_row = []
        for r, g, b, a in row:
            if a == 0:
                new_row.append((r, g, b, a))
            else:
                cr, cg, cb = colorize_face_pixel(r, g, b, hue_deg)
                new_row.append((cr, cg, cb, a))
        result.append(new_row)
    return result


# ---------------------------------------------------------------------------
# Dataclasses (plain namedtuples for Python 3.6 compatibility)
# ---------------------------------------------------------------------------
from collections import namedtuple

AssetFileHeader = namedtuple('AssetFileHeader',
    ['data_offset', 'data_size', 'asset_map_offset', 'asset_map_size'])

AssetMapHeader = namedtuple('AssetMapHeader',
    ['nbr_types', 'nbr_assets', 'len_names',
     'types_offset', 'recs_offset', 'names_offset'])

AssetTypeRec = namedtuple('AssetTypeRec',
    ['asset_type', 'nbr_assets', 'first_asset'])

AssetRec = namedtuple('AssetRec',
    ['id_nbr', 'r_handle', 'data_offset', 'data_size',
     'last_use_time', 'name_offset', 'flags', 'crc'])

PropHeader = namedtuple('PropHeader',
    ['width', 'height', 'h_offset', 'v_offset', 'script_offset', 'flags'])

PropInfo = namedtuple('PropInfo',
    ['id', 'crc', 'name', 'header', 'pixel_data'])


# ---------------------------------------------------------------------------
# PRP File Parser
# ---------------------------------------------------------------------------

def _unpack_file_header(data: bytes, endian: str) -> AssetFileHeader:
    fmt = f'{endian}4i'
    return AssetFileHeader(*struct.unpack(fmt, data[:ASSET_FILE_HEADER_SIZE]))


def _unpack_map_header(data: bytes, endian: str) -> AssetMapHeader:
    fmt = f'{endian}6i'
    return AssetMapHeader(*struct.unpack(fmt, data[:ASSET_MAP_HEADER_SIZE]))


def _unpack_type_rec(data: bytes, offset: int, endian: str) -> AssetTypeRec:
    fmt = f'{endian}3i'
    raw = struct.unpack(fmt, data[offset:offset + ASSET_TYPE_REC_SIZE])
    # asset_type is stored as big-endian 4-char code regardless of file endian
    return AssetTypeRec(*raw)


def _unpack_asset_rec(data: bytes, offset: int, endian: str) -> AssetRec:
    fmt = f'{endian}8i'
    raw = struct.unpack(fmt, data[offset:offset + ASSET_REC_SIZE])
    return AssetRec(*raw)


def _unpack_prop_header(data: bytes, endian: str) -> PropHeader:
    fmt = f'{endian}6h'
    return PropHeader(*struct.unpack(fmt, data[:PROP_HEADER_SIZE]))


def _read_pascal_string(name_blob: bytes, offset: int) -> str:
    """Read a Pascal string (length-byte prefix) from name_blob at offset."""
    if offset < 0 or offset >= len(name_blob):
        return ''
    length = name_blob[offset]
    start  = offset + 1
    end    = start + length
    if end > len(name_blob):
        end = len(name_blob)
    try:
        return name_blob[start:end].decode('mac_roman', errors='replace')
    except Exception:
        return ''


def _detect_endian(raw: bytes, file_size: int) -> str:
    """Return '>' (big) or '<' (little) based on which interpretation
    yields valid AssetFileHeader offsets."""
    for endian in ('>', '<'):
        try:
            hdr = _unpack_file_header(raw, endian)
            if (0 < hdr.asset_map_offset < file_size and
                    0 < hdr.data_offset <= file_size and
                    0 < hdr.asset_map_size < file_size and
                    hdr.asset_map_offset + hdr.asset_map_size <= file_size):
                return endian
        except Exception:
            continue
    return '>'  # fallback


def parse_prp(path: str) -> list:
    """
    Parse a Palace .prp file and return a list of PropInfo objects
    for every 'Prop' asset found.
    """
    with open(path, 'rb') as f:
        raw = f.read()

    file_size = len(raw)
    if file_size < ASSET_FILE_HEADER_SIZE:
        raise ValueError("File too small to be a valid .prp file.")

    endian = _detect_endian(raw, file_size)
    file_hdr = _unpack_file_header(raw, endian)

    # ---- Read asset map blob ------------------------------------------------
    map_start = file_hdr.asset_map_offset
    map_size  = file_hdr.asset_map_size
    if map_start + map_size > file_size:
        raise ValueError("Asset map extends beyond end of file.")

    map_blob = raw[map_start: map_start + map_size]
    map_hdr  = _unpack_map_header(map_blob, endian)

    # ---- Parse type list ----------------------------------------------------
    type_recs = []
    for i in range(map_hdr.nbr_types):
        off = map_hdr.types_offset + i * ASSET_TYPE_REC_SIZE
        tr  = _unpack_type_rec(map_blob, off, endian)
        type_recs.append(tr)

    # ---- Parse asset records ------------------------------------------------
    asset_recs = []
    for i in range(map_hdr.nbr_assets):
        off = map_hdr.recs_offset + i * ASSET_REC_SIZE
        ar  = _unpack_asset_rec(map_blob, off, endian)
        asset_recs.append(ar)

    # ---- Name blob ----------------------------------------------------------
    names_off  = map_hdr.names_offset
    names_end  = names_off + map_hdr.len_names
    name_blob  = map_blob[names_off:names_end]

    # ---- Find 'Prop' type ---------------------------------------------------
    # The asset_type field is 4 bytes. When read with the file's endian as
    # an int32, big-endian 'Prop' = 0x50726F70. Little-endian would flip it,
    # so we compare the raw bytes of each type record directly.
    prop_type_recs = []
    for tr in type_recs:
        # Reconstruct the 4-byte code from the int to compare
        type_bytes_be = struct.pack('>i', tr.asset_type)
        type_bytes_le = struct.pack('<i', tr.asset_type)
        if type_bytes_be == b'Prop' or type_bytes_le == b'Prop':
            prop_type_recs.append(tr)

    if not prop_type_recs:
        return []  # No props in this file

    # ---- Decode each prop ---------------------------------------------------
    props = []
    data_base = file_hdr.data_offset  # absolute offset to start of data area

    for tr in prop_type_recs:
        for i in range(tr.nbr_assets):
            idx = tr.first_asset + i
            if idx >= len(asset_recs):
                continue
            ar = asset_recs[idx]

            # Prop name (Pascal string)
            name = _read_pascal_string(name_blob, ar.name_offset) if ar.name_offset >= 0 else ''

            # Prop data
            abs_offset = data_base + ar.data_offset
            if abs_offset < 0 or abs_offset + ar.data_size > file_size:
                continue
            blob = raw[abs_offset: abs_offset + ar.data_size]
            if len(blob) < PROP_HEADER_SIZE:
                continue

            ph = _unpack_prop_header(blob, endian)

            # Sanity-check header (mirror the SwapShort fallback in RoomGraphics.c)
            if ph.height < 1 or ph.height > 256 or ph.width < 1 or ph.width > 256:
                # Try opposite endian for the prop header only
                alt = '>' if endian == '<' else '<'
                ph2 = _unpack_prop_header(blob, alt)
                if 1 <= ph2.height <= 256 and 1 <= ph2.width <= 256:
                    ph = ph2

            pixel_data = blob[PROP_HEADER_SIZE:]
            props.append(PropInfo(
                id=ar.id_nbr,
                crc=ar.crc,
                name=name,
                header=ph,
                pixel_data=pixel_data,
            ))

    return props


# ---------------------------------------------------------------------------
# RLE Decoder  (mirrors DrawMansionPropPixMap in RoomGraphics.c)
# ---------------------------------------------------------------------------

def decode_prop_pixels(prop: PropInfo) -> list:
    """
    Decode the RLE pixel stream into a 2-D list of (R,G,B,A) tuples.
    Transparent pixels → alpha=0; opaque pixels → alpha=255.
    Rows are top-to-bottom, columns left-to-right.
    """
    ph   = prop.header
    w    = max(1, ph.width)
    h    = max(1, ph.height)
    data = prop.pixel_data

    # Pre-fill with transparent
    rgba = [[(0, 0, 0, 0)] * w for _ in range(h)]

    sp = 0  # source pointer into data
    for y in range(h):
        x = 0
        while x < w:
            if sp >= len(data):
                break
            cb = data[sp]
            sp += 1
            mc = (cb >> 4) & 0xF   # transparent skip count
            pc =  cb       & 0xF   # opaque pixel count
            x += mc                # advance past transparent pixels
            for _ in range(pc):
                if x >= w or sp >= len(data):
                    sp += max(0, pc - _)  # skip remaining bytes safely
                    break
                idx = data[sp]
                sp += 1
                rgb = MAC_PALETTE[idx]
                rgba[y][x] = (rgb[0], rgb[1], rgb[2], 255)
                x += 1

    return rgba


def rgba_to_ppm(rgba: list, width: int, height: int,
                bg: tuple = (200, 200, 200)) -> bytes:
    """
    Composite RGBA pixels over bg colour and return raw PPM P6 bytes.
    Used as a fallback when Pillow is not available.
    """
    header = f'P6\n{width} {height}\n255\n'.encode()
    rows = []
    for row in rgba:
        row_bytes = bytearray()
        for r, g, b, a in row:
            if a == 0:
                row_bytes += bytes(bg)
            else:
                # Simple alpha composite over bg
                af = a / 255.0
                row_bytes += bytes([
                    int(r * af + bg[0] * (1 - af)),
                    int(g * af + bg[1] * (1 - af)),
                    int(b * af + bg[2] * (1 - af)),
                ])
        rows.append(bytes(row_bytes))
    return header + b''.join(rows)


def rgba_to_pillow(rgba: list, width: int, height: int):
    """Return a Pillow RGBA Image from the decoded pixels."""
    img = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    px  = img.load()
    for y, row in enumerate(rgba):
        for x, pixel in enumerate(row):
            px[x, y] = pixel
    return img


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

PREVIEW_W = 320
PREVIEW_H = 320
AVATAR_CX = PREVIEW_W // 2   # avatar centre x in preview canvas
AVATAR_CY = PREVIEW_H // 2   # avatar centre y in preview canvas

CANVAS_BG = '#6B8E9F'        # muted teal-blue room background

# Avatar silhouette geometry (relative to avatar centre)
FACE_HALF_W = FACE_WIDTH  // 2   # 22
FACE_HALF_H = FACE_HEIGHT // 2   # 22


class PropViewer(tk.Tk):
    def __init__(self, initial_path: str = None):
        super().__init__()
        self.title('Palace Prop Viewer')
        self.resizable(True, True)
        self.minsize(700, 480)

        self._props: list = []
        self._current_prop: PropInfo = None
        self._photo_image = None   # keep reference to avoid GC

        # Face colorisation state — default to flesh tone (hue 15°)
        self._face_hue = tk.DoubleVar(value=15.0)
        self._face_hue.trace_add('write', self._on_face_hue_changed)

        self._build_ui()
        self._draw_empty_preview()

        if initial_path and os.path.isfile(initial_path):
            self.after(100, lambda: self._load_file(initial_path))

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # ── Top toolbar ──────────────────────────────────────────────
        toolbar = ttk.Frame(self, padding=4)
        toolbar.pack(fill='x', side='top')

        self._open_btn = ttk.Button(toolbar, text='Open .prp File…',
                                    command=self._open_file)
        self._open_btn.pack(side='left', padx=(0, 8))

        self._file_label = ttk.Label(toolbar, text='No file loaded',
                                     foreground='#555555')
        self._file_label.pack(side='left')

        # ── Face colour controls (right side of toolbar) ──────────────
        ttk.Separator(toolbar, orient='vertical').pack(
            side='left', fill='y', padx=10)

        ttk.Label(toolbar, text='Face color:').pack(side='left', padx=(0, 4))

        # Preset dropdown
        preset_names = [p[0] for p in FACE_COLOR_PRESETS]
        self._face_preset_var = tk.StringVar(value=preset_names[0])
        preset_cb = ttk.Combobox(toolbar, textvariable=self._face_preset_var,
                                 values=preset_names, width=16,
                                 state='readonly')
        preset_cb.pack(side='left', padx=(0, 6))
        preset_cb.bind('<<ComboboxSelected>>', self._on_preset_selected)

        # Hue slider (0–360)
        ttk.Label(toolbar, text='Hue:').pack(side='left')
        hue_scale = ttk.Scale(toolbar, from_=0, to=360,
                              variable=self._face_hue,
                              orient='horizontal', length=120)
        hue_scale.pack(side='left', padx=(2, 4))

        # Live colour swatch
        self._swatch = tk.Label(toolbar, width=3, relief='sunken',
                                bg=self._hue_to_hex(15.0))
        self._swatch.pack(side='left', padx=(0, 4))

        # ── Main pane ────────────────────────────────────────────────
        paned = ttk.PanedWindow(self, orient='horizontal')
        paned.pack(fill='both', expand=True, padx=4, pady=(0, 4))

        # Left: prop list
        left_frame = ttk.Frame(paned, padding=2)
        paned.add(left_frame, weight=1)
        self._build_prop_list(left_frame)

        # Right: preview + info
        right_frame = ttk.Frame(paned, padding=2)
        paned.add(right_frame, weight=2)
        self._build_preview(right_frame)

        # ── Status bar ───────────────────────────────────────────────
        self._status = ttk.Label(self, text='Ready', anchor='w',
                                 relief='sunken', padding=(4, 2))
        self._status.pack(fill='x', side='bottom')

    def _build_prop_list(self, parent):
        ttk.Label(parent, text='Props', font=('TkDefaultFont', 10, 'bold')
                  ).pack(anchor='w')

        cols = ('id', 'name', 'size', 'flags')
        tree = ttk.Treeview(parent, columns=cols, show='headings',
                            selectmode='browse')
        tree.heading('id',    text='ID')
        tree.heading('name',  text='Name')
        tree.heading('size',  text='W×H')
        tree.heading('flags', text='Flags')

        tree.column('id',    width=80,  anchor='e')
        tree.column('name',  width=110, anchor='w')
        tree.column('size',  width=60,  anchor='center')
        tree.column('flags', width=90,  anchor='w')

        vsb = ttk.Scrollbar(parent, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)

        tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')

        tree.bind('<<TreeviewSelect>>', self._on_select)
        self._tree = tree

    def _build_preview(self, parent):
        # Canvas
        canvas_frame = ttk.LabelFrame(parent, text='Preview', padding=4)
        canvas_frame.pack(fill='both', expand=True)

        self._canvas = tk.Canvas(canvas_frame,
                                 width=PREVIEW_W, height=PREVIEW_H,
                                 bg=CANVAS_BG, highlightthickness=0)
        self._canvas.pack(fill='both', expand=True)

        # Info panel below canvas
        info_frame = ttk.Frame(parent, padding=(4, 4))
        info_frame.pack(fill='x')

        labels = ('ID:', 'CRC:', 'Size:', 'hOffset:', 'vOffset:', 'Flags:')
        self._info_vars = {}
        for i, lbl in enumerate(labels):
            ttk.Label(info_frame, text=lbl, foreground='#444').grid(
                row=i // 3, column=(i % 3) * 2, sticky='e', padx=(8, 2))
            var = tk.StringVar(value='—')
            ttk.Label(info_frame, textvariable=var, foreground='#000').grid(
                row=i // 3, column=(i % 3) * 2 + 1, sticky='w')
            self._info_vars[lbl] = var

    # ------------------------------------------------------------------
    # File loading
    # ------------------------------------------------------------------

    def _open_file(self):
        path = filedialog.askopenfilename(
            title='Open Palace Prop File',
            filetypes=[('Palace Prop Files', '*.prp *.PRP'),
                       ('All Files', '*.*')])
        if path:
            self._load_file(path)

    def _load_file(self, path: str):
        try:
            props = parse_prp(path)
        except Exception as exc:
            messagebox.showerror('Parse Error',
                                 f'Could not parse file:\n{exc}')
            return

        self._props = props
        self._file_label.config(
            text=f'{os.path.basename(path)}  —  {len(props)} prop(s)')
        self.title(f'Palace Prop Viewer — {os.path.basename(path)}')
        self._populate_list(props)
        self._draw_empty_preview()
        self._clear_info()
        self._status.config(
            text=f'Loaded {len(props)} prop(s) from {path}')

    def _populate_list(self, props: list):
        self._tree.delete(*self._tree.get_children())
        for i, p in enumerate(props):
            flag_str = _flags_str(p.header.flags)
            self._tree.insert('', 'end', iid=str(i), values=(
                p.id,
                p.name or '',
                f'{p.header.width}×{p.header.height}',
                flag_str,
            ))

    # ------------------------------------------------------------------
    # Face colour callbacks
    # ------------------------------------------------------------------

    def _on_face_hue_changed(self, *_):
        hue = self._face_hue.get()
        self._swatch.config(bg=self._hue_to_hex(hue))
        # Update preset label if hue matches a preset exactly
        for name, preset_hue in FACE_COLOR_PRESETS:
            if preset_hue == -1:
                continue
            if abs(preset_hue - hue) < 0.5:
                self._face_preset_var.set(name)
                break
        if self._current_prop:
            self._render_preview(self._current_prop)

    def _on_preset_selected(self, _event=None):
        name = self._face_preset_var.get()
        for preset_name, hue in FACE_COLOR_PRESETS:
            if preset_name == name:
                self._face_hue.set(float(max(hue, 0)))  # -1 → 0 for slider
                # For "Raw palette" disable colorisation via the hue var sentinel
                if hue == -1:
                    self._face_hue.set(-1.0)
                if self._current_prop:
                    self._render_preview(self._current_prop)
                break

    @staticmethod
    def _hue_to_hex(hue_deg: float) -> str:
        """Convert a hue angle to a saturated hex colour for the swatch."""
        if hue_deg < 0:
            return '#888888'
        r, g, b = colorsys.hls_to_rgb(hue_deg / 360.0, 0.55, 1.0)
        return f'#{int(r*255):02X}{int(g*255):02X}{int(b*255):02X}'

    # ------------------------------------------------------------------
    # Selection & preview
    # ------------------------------------------------------------------

    def _on_select(self, _event=None):
        sel = self._tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if idx >= len(self._props):
            return
        prop = self._props[idx]
        self._current_prop = prop
        self._render_preview(prop)
        self._update_info(prop)
        self._status.config(
            text=f'Prop ID {prop.id}  |  '
                 f'hOffset={prop.header.h_offset}  '
                 f'vOffset={prop.header.v_offset}')

    def _render_preview(self, prop: PropInfo):
        ph   = prop.header
        rgba = decode_prop_pixels(prop)

        # Apply face colourisation when the prop is a face sprite and the
        # user hasn't chosen "Raw palette" (sentinel hue = -1).
        if (ph.flags & PF_FACE) and self._face_hue.get() >= 0:
            rgba = apply_face_colorization(rgba, self._face_hue.get())

        # ── Compute actual canvas dimensions ─────────────────────────
        cw = self._canvas.winfo_width()  or PREVIEW_W
        ch = self._canvas.winfo_height() or PREVIEW_H
        cx = cw // 2   # avatar centre
        cy = ch // 2

        self._canvas.delete('all')
        self._draw_background(cw, ch)
        self._draw_avatar_silhouette(cx, cy)

        # ── Position the prop ─────────────────────────────────────────
        # ComputePropRect: ox = roomPos.h + hOffset - FaceWidth/2
        #                  oy = roomPos.v + vOffset - FaceHeight/2
        prop_x = cx + ph.h_offset - FACE_HALF_W
        prop_y = cy + ph.v_offset - FACE_HALF_H

        # Render prop image
        scale = 3  # Zoom up 3× for readability (props are only 44×44)
        self._draw_prop(rgba, ph.width, ph.height, prop_x, prop_y,
                        scale, ghost=(ph.flags & PF_GHOST) != 0)

        # Reference crosshair at avatar centre
        r = 3
        self._canvas.create_line(cx - 12, cy, cx + 12, cy,
                                 fill='#FF4444', width=1, tags='cross')
        self._canvas.create_line(cx, cy - 12, cx, cy + 12,
                                 fill='#FF4444', width=1, tags='cross')
        self._canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                 outline='#FF4444', width=1, tags='cross')

        # (bounding box is drawn inside _draw_prop)

    def _draw_prop(self, rgba, width, height, ox, oy, scale=1, ghost=False):
        """Render the prop onto the canvas at (ox, oy) with pixel scaling."""
        if HAS_PILLOW:
            self._draw_prop_pillow(rgba, width, height, ox, oy, scale, ghost)
        else:
            self._draw_prop_ppm(rgba, width, height, ox, oy, scale, ghost)

    def _draw_prop_pillow(self, rgba, width, height, ox, oy, scale, ghost):
        img = rgba_to_pillow(rgba, width, height)
        if ghost:
            # Reduce opacity by 50 % for ghost props
            r, g, b, a = img.split()
            a = a.point(lambda x: x // 2)
            img = Image.merge('RGBA', (r, g, b, a))

        # Composite over the canvas background colour
        bg_rgb = _hex_to_rgb(CANVAS_BG)
        bg = Image.new('RGBA', img.size, bg_rgb + (255,))
        comp = Image.alpha_composite(bg, img)
        comp = comp.convert('RGB')

        if scale > 1:
            comp = comp.resize((width * scale, height * scale),
                               Image.LANCZOS)
        photo = ImageTk.PhotoImage(comp)
        self._photo_image = photo   # prevent GC
        self._canvas.create_image(ox, oy, anchor='nw', image=photo)

        # Dashed bounding box
        self._canvas.create_rectangle(
            ox, oy, ox + width * scale, oy + height * scale,
            outline='#FFFF00', dash=(3, 3), width=1)

    def _draw_prop_ppm(self, rgba, width, height, ox, oy, scale, ghost):
        """Fallback renderer using tkinter PhotoImage (no Pillow)."""
        bg = _hex_to_rgb(CANVAS_BG)
        if ghost:
            # Mix pixel 50/50 with bg
            mixed = []
            for row in rgba:
                new_row = []
                for r, g, b, a in row:
                    if a == 0:
                        new_row.append((bg[0], bg[1], bg[2], 0))
                    else:
                        new_row.append((
                            (r + bg[0]) // 2,
                            (g + bg[1]) // 2,
                            (b + bg[2]) // 2,
                            128))
                mixed.append(new_row)
            rgba = mixed

        ppm = rgba_to_ppm(rgba, width, height, bg)
        photo = tk.PhotoImage(data=ppm, format='ppm')

        if scale > 1:
            photo = photo.zoom(scale)

        self._photo_image = photo
        self._canvas.create_image(ox, oy, anchor='nw', image=photo)
        self._canvas.create_rectangle(
            ox, oy, ox + width * scale, oy + height * scale,
            outline='#FFFF00', dash=(3, 3), width=1)

    def _draw_background(self, cw, ch):
        # Subtle tile lines to suggest a room
        for x in range(0, cw, 32):
            self._canvas.create_line(x, 0, x, ch, fill='#5A7D8E', width=1)
        for y in range(0, ch, 32):
            self._canvas.create_line(0, y, cw, y, fill='#5A7D8E', width=1)

    def _draw_avatar_silhouette(self, cx, cy):
        """Draw a simple stick-figure avatar centred at (cx, cy)."""
        # Head oval (matches FaceWidth×FaceHeight = 44×44 area)
        hw = FACE_HALF_W
        hh = FACE_HALF_H
        # The face rect in Palace is anchored at roomPos - (FaceWidth/2, FaceHeight/2)
        fx = cx - hw
        fy = cy - hh

        # Body (below face)
        body_top    = fy + FACE_HEIGHT
        body_bottom = body_top + 40
        body_left   = cx - 14
        body_right  = cx + 14

        # Legs
        leg_bottom = body_bottom + 30

        colour = '#C8C8A0'
        outline = '#A0A080'

        # Legs
        self._canvas.create_line(cx - 8, body_bottom, cx - 14, leg_bottom,
                                 fill=colour, width=3)
        self._canvas.create_line(cx + 8, body_bottom, cx + 14, leg_bottom,
                                 fill=colour, width=3)
        # Arms
        self._canvas.create_line(body_left,  body_top + 10,
                                 body_left  - 18, body_top + 28,
                                 fill=colour, width=3)
        self._canvas.create_line(body_right, body_top + 10,
                                 body_right + 18, body_top + 28,
                                 fill=colour, width=3)
        # Body
        self._canvas.create_rectangle(body_left, body_top,
                                      body_right, body_bottom,
                                      fill=colour, outline=outline, width=1)
        # Face oval
        self._canvas.create_oval(fx, fy, fx + FACE_WIDTH, fy + FACE_HEIGHT,
                                 fill='#E0C890', outline=outline, width=1)
        # Simple eyes
        self._canvas.create_oval(cx - 8, cy - 6, cx - 4, cy - 2,
                                 fill='#444444', outline='')
        self._canvas.create_oval(cx + 4, cy - 6, cx + 8, cy - 2,
                                 fill='#444444', outline='')
        # Simple smile
        self._canvas.create_arc(cx - 8, cy, cx + 8, cy + 10,
                                start=200, extent=140,
                                style='arc', outline='#444444', width=1)

        # Face bounding box (light dashed — shows the 44×44 face area)
        self._canvas.create_rectangle(fx, fy, fx + FACE_WIDTH, fy + FACE_HEIGHT,
                                      outline='#AACCFF', dash=(2, 4), width=1)

    def _draw_empty_preview(self):
        cw = self._canvas.winfo_width()  or PREVIEW_W
        ch = self._canvas.winfo_height() or PREVIEW_H
        self._canvas.delete('all')
        self._draw_background(cw, ch)
        self._draw_avatar_silhouette(cw // 2, ch // 2)
        self._canvas.create_text(
            cw // 2, ch - 20,
            text='Select a prop from the list →',
            fill='#DDDDDD', font=('TkDefaultFont', 9))

    def _clear_info(self):
        for var in self._info_vars.values():
            var.set('—')

    def _update_info(self, prop: PropInfo):
        ph = prop.header
        self._info_vars['ID:'].set(str(prop.id))
        self._info_vars['CRC:'].set(f'0x{prop.crc & 0xFFFFFFFF:08X}')
        self._info_vars['Size:'].set(f'{ph.width}×{ph.height}')
        self._info_vars['hOffset:'].set(str(ph.h_offset))
        self._info_vars['vOffset:'].set(str(ph.v_offset))
        self._info_vars['Flags:'].set(_flags_str(ph.flags) or 'none')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flags_str(flags: int) -> str:
    parts = []
    if flags & PF_FACE:       parts.append('Face')
    if flags & PF_GHOST:      parts.append('Ghost')
    if flags & PF_RARE:       parts.append('Rare')
    if flags & PF_ANIMATE:    parts.append('Anim')
    if flags & PF_PALINDROME: parts.append('Pali')
    return ' '.join(parts)


def _hex_to_rgb(hex_str: str) -> tuple:
    hex_str = hex_str.lstrip('#')
    return tuple(int(hex_str[i:i+2], 16) for i in (0, 2, 4))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    initial = sys.argv[1] if len(sys.argv) > 1 else None
    app = PropViewer(initial_path=initial)

    # Redraw preview when canvas is resized
    def _on_resize(event):
        if app._current_prop:
            app._render_preview(app._current_prop)
        else:
            app._draw_empty_preview()

    app._canvas.bind('<Configure>', _on_resize)
    app.mainloop()


if __name__ == '__main__':
    main()
