"""MonKap's routes for the web UI: /api/monkap/*.

The file logic is in fizgig/monkap (no web server in it); the page is web/static/monkap.js. Every call names a
dataset `folder` and a picture `name` inside it - nothing here accepts a path to anywhere else, and a name that is
not a plain file name of a picture the trainer reads is refused.

Pictures are sent as pixels MonKap decoded itself (no EXIF turning: the trainer's Image.open doesn't turn either,
so the frame on the page is what the trainer will see), and with no-store: nothing from a dataset lands in the
browser's cache.
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
from collections import OrderedDict

from PIL import Image

from fizgig import user_data
from fizgig.monkap import captions, jobs, ledger
from fizgig.monkap.fsutil import IMAGE_EXTS, write_text_atomic
from fizgig.monkap.naming import TemplateError, example_names, normalize_template
from fizgig.monkap.settings import CAPTION_MODES, DELETE_MODES, DELETE_SOFT, RATIO_NAMES, MonkapSettings

MAX_EDGE = 4096                    # the biggest picture a preview or a tile request may ask for
_FIT_EDGE = 2400                   # the whole-picture preview's long edge
_kapture_lock = threading.Lock()   # one crop at a time: two requests must not plan the same name


class NotFound(LookupError):
    """The folder or the picture is not there (a 404, not a mistake in the request)."""


# ------------------------------------------------------------------------------------------------ requests

def _ext(raw) -> str:
    from fizgig.web.captions import caption_ext
    return caption_ext(raw)


def _locate(folder, name) -> tuple[str, str]:
    """(folder, path of the picture): the folder is a real directory and the name a plain picture file name."""
    folder = os.path.abspath(str(folder or ""))
    name = str(name or "")
    if not os.path.isdir(folder):
        raise NotFound("That folder isn't there.")
    if not name or name != os.path.basename(name) or name.startswith("."):
        raise ValueError("Not a file name.")
    if os.path.splitext(name)[1].lower() not in IMAGE_EXTS:
        raise ValueError("MonKap opens the pictures the trainer reads: png, jpg, webp, bmp, avif.")
    path = os.path.join(folder, name)
    if not os.path.isfile(path):
        raise NotFound("That picture isn't in the folder any more.")
    return folder, path


def _int(qs, key, default=0, lo=-(1 << 31), hi=1 << 31) -> int:
    try:
        return min(max(int(float((qs.get(key) or [default])[0])), lo), hi)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------------------------------------ settings

def settings_path() -> str:
    return user_data.user_file("monkap.json")


def load_settings() -> MonkapSettings:
    try:
        with open(settings_path(), encoding="utf-8") as f:
            return MonkapSettings.from_dict(json.load(f))
    except (OSError, ValueError):
        return MonkapSettings()


def save_settings(changes: dict) -> tuple[MonkapSettings, str]:
    """Merge `changes` into the saved settings; (settings as kept, a note about the template if it was changed)."""
    merged = {**load_settings().to_dict(), **{k: v for k, v in (changes or {}).items()}}
    note = ""
    if "template" in (changes or {}):
        try:
            merged["template"], note = normalize_template(str(merged["template"]))
        except TemplateError as exc:
            raise ValueError(str(exc)) from exc
    s = MonkapSettings.from_dict(merged)
    write_text_atomic(settings_path(), json.dumps(s.to_dict(), indent=2), overwrite=True)
    return s, note


def settings_payload(s: MonkapSettings | None = None) -> dict:
    s = s or load_settings()
    return {"settings": s.to_dict(), "examples": example_names(s.template),
            "ratios": list(RATIO_NAMES), "caption_modes": list(CAPTION_MODES), "delete_modes": list(DELETE_MODES)}


# ------------------------------------------------------------------------------------------------ pictures

class _Decoded:
    """The last pictures decoded for the page, kept so zooming into one big picture does not decode it again for
    every tile. Bounded by pixels: a 48 MP picture is ~150 MB decoded."""
    MAX_PIXELS = 160_000_000

    def __init__(self):
        self._lock = threading.Lock()
        self._items: OrderedDict = OrderedDict()
        self._gates: dict = {}

    def get(self, path) -> Image.Image:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
        with self._lock:
            im = self._items.get(key)
            if im is not None:
                self._items.move_to_end(key)
                return im
            gate = self._gates.setdefault(key, threading.Lock())
        with gate:                                    # one decode per picture, however many tiles ask at once
            with self._lock:
                im = self._items.get(key)
            if im is None:
                with Image.open(path) as src:
                    src.load()
                    im = _for_display(src)
                with self._lock:
                    self._items[key] = im
                    while len(self._items) > 1 and sum(i.width * i.height for i in self._items.values()) > self.MAX_PIXELS:
                        self._items.popitem(last=False)
            with self._lock:
                self._gates.pop(key, None)
        return im


_decoded = _Decoded()
_encoded: OrderedDict = OrderedDict()                 # whole-picture previews as sent, a handful
_encoded_lock = threading.Lock()


def _for_display(im: Image.Image) -> Image.Image:
    """RGB, or RGBA when there is transparency: what a browser can show of whatever the file holds."""
    if im.mode in ("RGB", "RGBA"):
        return im
    if im.mode in ("P", "PA"):
        return im.convert("RGBA" if ("transparency" in im.info or im.mode == "PA") else "RGB")
    return im.convert("RGBA" if "A" in im.mode else "RGB")


def _encode(im: Image.Image) -> tuple[bytes, str]:
    buf = io.BytesIO()
    if im.mode == "RGBA":
        im.save(buf, "PNG", compress_level=1)
        return buf.getvalue(), "image/png"
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue(), "image/jpeg"


def picture_size(path) -> list[int]:
    """[width, height] as stored in the file (the trainer's view: no EXIF turning)."""
    with Image.open(path) as im:
        return [int(im.width), int(im.height)]


def fit_preview(path, edge=_FIT_EDGE) -> tuple[bytes, str]:
    """The whole picture, its long edge at most `edge` (never enlarged)."""
    edge = min(max(int(edge), 64), MAX_EDGE)
    st = os.stat(path)
    key = (path, st.st_mtime_ns, st.st_size, edge)
    with _encoded_lock:
        hit = _encoded.get(key)
    if hit is not None:
        return hit
    im = _decoded.get(path)
    if max(im.size) > edge:
        k = edge / max(im.size)
        im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS, reducing_gap=2.0)
    out = _encode(im)
    with _encoded_lock:
        _encoded[key] = out
        while len(_encoded) > 6:
            _encoded.popitem(last=False)
    return out


def region(path, box, out_w) -> tuple[bytes, str]:
    """`box` (left, top, right, bottom in the picture's own pixels) at the resolution the screen needs: at most
    `out_w` wide, and at native pixels (no resampling) when the box is smaller than that."""
    im = _decoded.get(path)
    l, t, r, b = box
    l, t, r, b = max(0, l), max(0, t), min(im.width, r), min(im.height, b)
    if r - l < 1 or b - t < 1:
        raise ValueError("That area is empty.")
    out_w = min(max(int(out_w), 1), MAX_EDGE)
    if r - l <= out_w:
        piece = im.crop((l, t, r, b))
    else:
        k = out_w / (r - l)
        piece = im.resize((out_w, max(1, round((b - t) * k))), Image.LANCZOS, box=(l, t, r, b), reducing_gap=2.0)
    return _encode(piece)


# ------------------------------------------------------------------------------------------------ the dataset

_NATURAL = re.compile(r"(\d+)")


def _natural(name: str):
    return [int(p) if p.isdigit() else p for p in _NATURAL.split(name.casefold())]


def list_folder(folder, ext) -> list[list]:
    """[[name, 1 if it has a caption file else 0], ...] for every picture the trainer reads, in natural order
    (img_2 before img_10). Names only: sizes and captions load for the picture on screen."""
    ext = ext.lower()
    pics, have = [], set()
    try:
        with os.scandir(folder) as it:                # the file type comes with the listing: no stat per file
            for e in it:
                stem, x = os.path.splitext(e.name)
                x = x.lower()
                if x == ext:
                    have.add(stem)
                elif x in IMAGE_EXTS and not e.name.startswith(".") and e.is_file():
                    pics.append(e.name)
    except OSError as exc:
        raise NotFound("That folder isn't there.") from exc
    return [[n, int(os.path.splitext(n)[0] in have)] for n in sorted(pics, key=_natural)]


def info(folder, name, ext) -> dict:
    folder, path = _locate(folder, name)
    text = captions.read_caption(path, ext)
    row = next((r for r in reversed(ledger.read_provenance(folder)) if str(r.get("file", "")).lower() == name.lower()), None)
    inherited = bool(text and row and row.get("caption_sha1") == captions.text_digest(text))
    return {"name": name, "size": picture_size(path), "caption": text, "inherited": inherited,
            "mtime": int(os.path.getmtime(path))}


def save_caption(folder, name, text, loaded, ext) -> dict:
    _, path = _locate(folder, name)
    res = captions.save_caption(path, str(text or ""), None if loaded is None else str(loaded), ext)
    backup = os.path.relpath(res.backup, folder) if res.backup else ""
    return {"kind": res.kind, "backup": backup, "conflict": res.conflict,
            "has_caption": res.kind in ("written", "unchanged") and bool(str(text or "").strip())}


def kapture(folder, name, box, parent_text, trigger, ext) -> dict:
    folder, path = _locate(folder, name)
    try:
        box = tuple(int(v) for v in box)
        if len(box) != 4:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("The selection is not a box.") from None
    s = load_settings()
    with _kapture_lock:
        for attempt in range(3):                      # another program may take the planned name meanwhile
            try:
                res = jobs.kapture(path, box, str(parent_text or ""), str(trigger or ""), s.template,
                                   s.caption_mode, s.store_provenance, ext)
                break
            except FileExistsError:
                if attempt == 2:
                    raise
    return {"name": os.path.basename(res.path), "size": list(res.size), "caption": res.caption,
            "notes": res.notes, "mode": s.caption_mode if res.caption is not None else captions.MODE_EMPTY}


def delete(folder, name, ext) -> dict:
    """Soft (the default): into the dataset's removed/ folder with Undo - the same folder and the same Undo as
    Captions > Remove. Hard (the user's choice in MonKap's settings): erased, with its cached latents."""
    from fizgig.web import server
    folder, path = _locate(folder, name)
    if load_settings().delete_mode == DELETE_SOFT:
        return {"mode": DELETE_SOFT, "moved": server.remove_from_dataset(folder, name, ext)}
    from fizgig.families.launch import cache_dir_for
    root = server.pref_path(server.load_prefs(), "cache_dir")
    res = jobs.delete_permanently(path, cache_dir_for(root, folder) if root else "", ext)
    return {"mode": "hard", "purged": res.purged, "notes": res.notes}


def undelete(folder, moved) -> dict:
    from fizgig.web import server
    return {"restored": server.restore_to_dataset(str(folder or ""), moved or [])}


# ------------------------------------------------------------------------------------------------ routes

def get(h, path, qs) -> None:
    """GET /api/monkap/...; h is the request handler (its json() and _send())."""
    one = lambda k, d="": (qs.get(k) or [d])[0]
    try:
        if path == "/api/monkap/list":
            ext = _ext(one("ext"))
            folder = os.path.abspath(one("folder"))
            return h.json({"folder": folder, "ext": ext, "items": list_folder(folder, ext)})
        if path == "/api/monkap/info":
            return h.json(info(one("folder"), one("name"), _ext(one("ext"))))
        if path in ("/api/monkap/thumb", "/api/monkap/view", "/api/monkap/tile"):
            folder, pic = _locate(one("folder"), one("name"))
            if path == "/api/monkap/thumb":
                from fizgig.web.server import _thumb
                data = _thumb(pic)
                if data is None:
                    raise NotFound("No thumbnail for that picture.")
                return h._send(200, data, "image/jpeg")
            if path == "/api/monkap/view":
                data, ctype = fit_preview(pic, _int(qs, "edge", _FIT_EDGE, 64, MAX_EDGE))
            else:
                box = (_int(qs, "x0"), _int(qs, "y0"), _int(qs, "x1"), _int(qs, "y1"))
                data, ctype = region(pic, box, _int(qs, "w", 1024, 1, MAX_EDGE))
            return h._send(200, data, ctype)
        if path == "/api/monkap/settings":
            return h.json(settings_payload())
        if path == "/api/monkap/template":
            try:
                tpl, note = normalize_template(one("t"))
            except TemplateError as exc:
                return h.json({"ok": False, "error": str(exc)})
            return h.json({"ok": True, "template": tpl, "note": note, "examples": example_names(tpl)})
    except NotFound as exc:
        return h.json({"error": str(exc)}, 404)
    except ValueError as exc:
        return h.json({"error": str(exc)}, 400)
    except (OSError, Image.DecompressionBombError) as exc:
        return h.json({"error": f"MonKap could not read that picture ({type(exc).__name__}: {exc})."}, 500)
    return h.json({"error": "not found"}, 404)


def post(h, path, b) -> None:
    """POST /api/monkap/...; b is the JSON body."""
    try:
        ext = _ext(b.get("ext"))
        if path == "/api/monkap/caption":
            return h.json(save_caption(b.get("folder"), b.get("name"), b.get("text"), b.get("loaded"), ext))
        if path == "/api/monkap/kapture":
            return h.json(kapture(b.get("folder"), b.get("name"), b.get("box"), b.get("text"), b.get("trigger"), ext))
        if path == "/api/monkap/delete":
            return h.json(delete(b.get("folder"), b.get("name"), ext))
        if path == "/api/monkap/undelete":
            return h.json(undelete(b.get("folder"), b.get("moved")))
        if path == "/api/monkap/settings":
            s, note = save_settings(b.get("settings") or {})
            return h.json({**settings_payload(s), "note": note})
    except NotFound as exc:
        return h.json({"error": str(exc)}, 404)
    except ValueError as exc:
        return h.json({"error": str(exc)}, 400)
    except OSError as exc:
        return h.json({"error": f"MonKap could not do that ({type(exc).__name__}: {exc})."}, 500)
    return h.json({"error": "not found"}, 404)
