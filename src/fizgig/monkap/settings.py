"""MonKap settings: a small dataclass, sanitised on load, kept in user_data/monkap.json."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields

from .captions import CAPTION_MODES, MODE_COPY
from .naming import DEFAULT_TEMPLATE, TemplateError, normalize_template

RATIO_NAMES = ("1:1", "3:4", "4:3", "9:16", "16:9", "Free")
DELETE_SOFT = "soft"        # into the dataset's removed/ folder, with Undo (the folder Captions > Remove uses)
DELETE_HARD = "hard"        # the file, its caption and its cached latents are erased
DELETE_MODES = (DELETE_SOFT, DELETE_HARD)


@dataclass
class MonkapSettings:
    ratio: str = "1:1"                    # last frame ratio; also the default
    template: str = DEFAULT_TEMPLATE      # output file name, no extension
    caption_mode: str = MODE_COPY         # what a new crop's caption gets
    store_provenance: bool = True         # .monkap/provenance.jsonl
    show_thirds: bool = True              # rule-of-thirds grid in the frame
    confirm_delete: bool = True           # ask before Shift+Delete removes an image
    delete_mode: str = DELETE_SOFT        # soft = recoverable, hard = erased for good

    @classmethod
    def from_dict(cls, raw) -> "MonkapSettings":
        """Tolerant load: unknown keys are dropped, bad values fall back to the defaults."""
        s = cls()
        if not isinstance(raw, dict):
            return s
        known = {f.name for f in fields(cls)}
        for k, v in raw.items():
            if k in known:
                setattr(s, k, v)
        if s.ratio not in RATIO_NAMES:
            s.ratio = "1:1"
        if s.caption_mode not in CAPTION_MODES:
            s.caption_mode = MODE_COPY
        if s.delete_mode not in DELETE_MODES:
            s.delete_mode = DELETE_SOFT
        try:
            s.template, _ = normalize_template(str(s.template))
        except TemplateError:
            s.template = DEFAULT_TEMPLATE
        s.store_provenance = bool(s.store_provenance)
        s.show_thirds = bool(s.show_thirds)
        s.confirm_delete = bool(s.confirm_delete)
        return s

    def to_dict(self) -> dict:
        return asdict(self)
