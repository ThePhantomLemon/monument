#!/usr/bin/env python3
"""Build Monument's cards for Tabletop Simulator.

    python build.py           render the cards and write the TTS saved object
    python build.py --push    ...then commit and push, so TTS can load the new images
    python build.py --local   point TTS at the images on this PC (solo testing, no push)

Card data lives in cards/cards.csv, the look in style.toml, settings in config.toml.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit("Python 3.11 or newer is needed.")
import tomllib

try:
    from PIL import Image, ImageDraw, ImageFont
except ModuleNotFoundError:
    sys.exit("Pillow is missing. Install it with:  python -m pip install -r requirements.txt")

ROOT = Path(__file__).resolve().parent
CARDS_CSV = ROOT / "cards" / "cards.csv"
OUT = ROOT / "out"
IMAGES = OUT / "images"      # committed: TTS downloads these from GitHub
PREVIEW = OUT / "preview"    # not committed: one PNG per card for a quick look
TTS_OUT = OUT / "tts"

GROUPS = ["Starting", "Draft", "Shared"]       # values allowed in the csv's deck column
PER_PLAYER = {"Starting", "Draft"}             # every player gets a copy of these decks
DECK_NAMES = {"Starting": "Starting deck", "Draft": "Draft pool", "Shared": "Shared"}
REQUIRED_COLUMNS = ["id", "name", "deck", "type", "copies"]
PLACEHOLDER_USER = "YOUR-GITHUB-USERNAME"


# ---------------------------------------------------------------- loading

@dataclass
class Card:
    id: str
    name: str
    deck: str
    type: str
    copies: int
    cost: str
    build: str
    text: str
    role: str
    version: str


def load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def read_csv_rows(path: Path) -> tuple[list[str], list[dict]]:
    # Excel may save as UTF-8 (with or without BOM) or as Windows-1252.
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with open(path, newline="", encoding=encoding) as f:
                reader = csv.DictReader(f)
                return list(reader.fieldnames or []), list(reader)
        except UnicodeDecodeError:
            continue
    sys.exit(f"Couldn't read {path.name}: save it as CSV UTF-8.")


def load_cards(path: Path, style: dict, default_version: str) -> list[Card]:
    columns, rows = read_csv_rows(path)
    columns = [c.strip().lower() for c in columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        sys.exit(f"{path.name} is missing columns: {', '.join(missing)}")

    cards, problems, seen = [], [], set()
    for line, raw in enumerate(rows, start=2):
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        if not any(r.values()):
            continue
        where = f"{path.name} line {line}"
        if not r["id"] or not r["name"]:
            problems.append(f"{where}: every card needs an id and a name")
            continue
        if r["id"] in seen:
            problems.append(f"{where}: id {r['id']} is used twice")
        seen.add(r["id"])
        if r["deck"] not in GROUPS:
            problems.append(f"{where}: deck must be one of {', '.join(GROUPS)}, not '{r['deck']}'")
        if r["type"] not in style["types"]:
            problems.append(f"{where}: type '{r['type']}' has no colour in style.toml [types]")
        try:
            copies = int(r["copies"] or "1")
        except ValueError:
            problems.append(f"{where}: copies must be a whole number, not '{r['copies']}'")
            copies = 0
        cards.append(Card(
            id=r["id"], name=r["name"], deck=r["deck"], type=r["type"], copies=copies,
            cost=r.get("cost", ""), build=r.get("build", ""), text=r.get("text", ""),
            role=r.get("role", ""), version=r.get("version", "") or default_version,
        ))
    if problems:
        sys.exit("Fix these in the card list, then build again:\n  " + "\n  ".join(problems))
    return [c for c in cards if c.copies > 0]


# ---------------------------------------------------------------- rendering

class Fonts:
    def __init__(self, style: dict):
        self.paths = {kind: ROOT / rel for kind, rel in style["fonts"].items()}
        for kind, p in self.paths.items():
            if not p.exists():
                sys.exit(f"Font file for '{kind}' not found: {p}")
        self._cache: dict = {}

    def get(self, kind: str, size: int) -> ImageFont.FreeTypeFont:
        key = (kind, size)
        if key not in self._cache:
            self._cache[key] = ImageFont.truetype(str(self.paths[kind]), size)
        return self._cache[key]


def rich_words(text: str, keywords: list[str]) -> list[list[tuple[str, bool]]]:
    """Split text into paragraphs of (word, is_bold) pairs. Keywords are bold."""
    text = text.replace("\\n", "\n")
    pattern = "(" + "|".join(re.escape(k) for k in sorted(keywords, key=len, reverse=True)) + ")"
    paragraphs = []
    for para in text.split("\n"):
        words = []
        pieces = re.split(pattern, para) if keywords else [para]
        for piece in pieces:
            bold = piece in keywords
            words.extend((w, bold) for w in piece.split())
        paragraphs.append(words)
    return paragraphs


def wrap(paragraphs, fonts: Fonts, size: int, width: float):
    """Lay words out into lines no wider than width. None marks a paragraph gap."""
    regular, bold = fonts.get("regular", size), fonts.get("bold", size)
    space = regular.getlength(" ")
    lines: list = []
    for p, words in enumerate(paragraphs):
        if p:
            lines.append(None)
        line, used = [], 0.0
        for word, is_bold in words:
            w = (bold if is_bold else regular).getlength(word)
            extra = w if not line else space + w
            if line and used + extra > width:
                lines.append(line)
                line, used = [(word, is_bold)], w
            else:
                line.append((word, is_bold))
                used += extra
        lines.append(line)
    return lines


def text_height(lines, size: int, spacing: float) -> float:
    lh = size * spacing
    return sum(lh / 2 if line is None else lh for line in lines)


def draw_lines(draw, lines, fonts: Fonts, size: int, x: float, y: float, color: str, spacing: float):
    regular, bold = fonts.get("regular", size), fonts.get("bold", size)
    space = regular.getlength(" ")
    lh = size * spacing
    for line in lines:
        if line is None:
            y += lh / 2
            continue
        cx = x
        for i, (word, is_bold) in enumerate(line):
            font = bold if is_bold else regular
            if i:
                cx += space
            draw.text((cx, y), word, font=font, fill=color)
            cx += font.getlength(word)
        y += lh


def fit_single_line(fonts: Fonts, kind: str, text: str, size: int, width: float, smallest: int = 14):
    while size > smallest and fonts.get(kind, size).getlength(text) > width:
        size -= 1
    return fonts.get(kind, size)


def render_card(card: Card, style: dict, fonts: Fonts) -> Image.Image:
    c, sz, tx, track = style["card"], style["sizes"], style["text"], style["track"]
    W, H, fr, pad, hh = c["width"], c["height"], c["frame"], c["padding"], c["header_height"]
    color = style["types"][card.type]["color"]
    spacing = tx.get("line_spacing", 1.3)

    img = Image.new("RGB", (W, H), color)
    d = ImageDraw.Draw(img)
    radius = c["corner_radius"]
    d.rounded_rectangle([fr, fr, W - fr - 1, H - fr - 1], radius=radius, fill=c["background"])

    # Name band, rounded at the top only.
    d.rounded_rectangle([fr, fr, W - fr - 1, fr + hh], radius=radius, fill=color)
    d.rectangle([fr, fr + hh - radius, W - fr - 1, fr + hh], fill=color)
    left = fr + pad
    name_width = W - 2 * (fr + pad)

    # Idea cost in a circle at the top right.
    if card.cost:
        dia = hh - 14
        cx, cy = W - fr - pad - dia / 2, fr + hh / 2
        d.ellipse([cx - dia / 2, cy - dia / 2, cx + dia / 2, cy + dia / 2], fill="#FFFFFF")
        cost_font = fit_single_line(fonts, "bold", card.cost, sz["cost"], dia - 12)
        d.text((cx, cy), card.cost, font=cost_font, fill=c["text_color"], anchor="mm")
        name_width -= dia + 12
    name_font = fit_single_line(fonts, "bold", card.name, sz["name"], name_width, smallest=18)
    d.text((left, fr + hh / 2), card.name, font=name_font, fill="#FFFFFF", anchor="lm")

    # Type line, then the build cost for anything built from blueprints.
    y = fr + hh + 14
    type_line = f"{card.type}  ·  {DECK_NAMES[card.deck]}".upper()
    d.text((left, y), type_line, font=fonts.get("semibold", sz["type_line"]), fill=c["muted_color"])
    y += sz["type_line"] + 12
    if card.build:
        label_font = fonts.get("bold", sz["build_line"])
        d.text((left, y), "Build:", font=label_font, fill=c["text_color"])
        bx = left + label_font.getlength("Build: ")
        d.text((bx, y), card.build, font=fonts.get("regular", sz["build_line"]), fill=c["text_color"])
        y += sz["build_line"] * spacing + 6
    d.line([left, y, W - fr - pad, y], fill=c["muted_color"], width=1)
    y += 16

    # Card text, shrunk until it fits above the track band (or the footer).
    has_track = card.type in track["types"]
    bottom = (H - track["from_bottom"] - 16) if has_track else (H - fr - 44)
    width = W - 2 * (fr + pad)
    paragraphs = rich_words(card.text, tx["keywords"])
    size = sz["body"]
    lines = wrap(paragraphs, fonts, size, width)
    while size > sz["body_min"] and text_height(lines, size, spacing) > bottom - y:
        size -= 1
        lines = wrap(paragraphs, fonts, size, width)
    if text_height(lines, size, spacing) > bottom - y:
        print(f"  warning: text on {card.id} {card.name} doesn't fit even at size {size}")
    draw_lines(d, lines, fonts, size, left, y, c["text_color"], spacing)

    # The track band runs edge to edge so neighbouring cards join up.
    if has_track:
        ty = H - track["from_bottom"]
        d.rectangle([0, ty, W, ty + track["height"]], fill=track["color"])

    # Id and version, so testers can tell which printing they have.
    footer = fonts.get("regular", sz["footer"])
    fy = H - fr - 12
    d.text((left, fy), card.id, font=footer, fill=c["muted_color"], anchor="ls")
    d.text((W - fr - pad, fy), card.version, font=footer, fill=c["muted_color"], anchor="rs")
    return img


def render_back(group: str, game_name: str, style: dict, fonts: Fonts) -> Image.Image:
    c, back = style["card"], style["backs"][group]
    W, H, fr = c["width"], c["height"], c["frame"]
    img = Image.new("RGB", (W, H), back["color"])
    d = ImageDraw.Draw(img)
    inset = fr + 10
    d.rounded_rectangle([inset, inset, W - inset - 1, H - inset - 1],
                        radius=c["corner_radius"], outline="#FFFFFF", width=3)
    title = fit_single_line(fonts, "bold", game_name.upper(), 46, W - 2 * inset - 40)
    d.text((W / 2, H / 2 - 18), game_name.upper(), font=title, fill="#FFFFFF", anchor="mm")
    d.text((W / 2, H / 2 + 34), back["label"], font=fonts.get("regular", 24), fill="#E9E3D7", anchor="mm")
    return img


# ---------------------------------------------------------------- sheets and files

def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def write_hashed(img: Image.Image, stem: str, written: set[str]) -> Path:
    """Save under a name that changes whenever the picture changes, so TTS never shows stale cards."""
    data = png_bytes(img)
    path = IMAGES / f"{stem}-{hashlib.sha1(data).hexdigest()[:10]}.png"
    if not path.exists():
        path.write_bytes(data)
    written.add(path.name)
    return path


def build_sheets(cards: list[Card], faces: dict, style: dict):
    """Pack unique faces into grid images. Yields (image, columns, rows, cards on it)."""
    W, H = style["card"]["width"], style["card"]["height"]
    max_cols, max_rows = style["sheet"]["max_columns"], style["sheet"]["max_rows"]
    per_sheet = max_cols * max_rows
    for start in range(0, len(cards), per_sheet):
        chunk = cards[start:start + per_sheet]
        cols = min(max_cols, max(2, len(chunk)))
        rows = max(2, math.ceil(len(chunk) / cols))
        sheet = Image.new("RGB", (cols * W, rows * H), "#FFFFFF")
        for i, card in enumerate(chunk):
            sheet.paste(faces[card.id], ((i % cols) * W, (i // cols) * H))
        yield sheet, cols, rows, chunk


def image_url(path: Path, cfg: dict, local: bool) -> str:
    if local:
        return path.resolve().as_uri()
    g = cfg["github"]
    rel = path.relative_to(ROOT).as_posix()
    return f"https://raw.githubusercontent.com/{g['user']}/{g['repo']}/{g['branch']}/{rel}"


# ---------------------------------------------------------------- Tabletop Simulator objects

def guid(*parts: str) -> str:
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:6]


def transform(face_down: bool = True) -> dict:
    return {"posX": 0, "posY": 1, "posZ": 0, "rotX": 0, "rotY": 180,
            "rotZ": 180 if face_down else 0, "scaleX": 1, "scaleY": 1, "scaleZ": 1}


COMMON = {"Locked": False, "Grid": True, "Snap": True, "Autoraise": True,
          "Sticky": True, "Tooltip": True, "GridProjection": False}


def description(card: Card) -> str:
    head = "  ·  ".join(x for x in (f"Cost: {card.cost} ideas" if card.cost else "",
                                     f"Build: {card.build}" if card.build else "") if x)
    body = card.text.replace("\\n", "\n")
    return f"{head}\n{body}" if head else body


def card_object(card: Card, ref: tuple[int, int], custom_decks: dict, uid: str,
                face_down: bool = True) -> dict:
    key, index = ref
    return {
        "GUID": guid(uid), "Name": "Card", "Transform": transform(face_down),
        "Nickname": card.name, "Description": description(card),
        "GMNotes": f"{card.id} {card.version}" + (f" | {card.role}" if card.role else ""),
        "CardID": key * 100 + index, "CustomDeck": {str(key): custom_decks[key]},
        "Hands": True, "HideWhenFaceDown": True, "SidewaysCard": False,
        "ColorDiffuse": {"r": 0.713, "g": 0.713, "b": 0.713}, **COMMON,
    }


def deck_object(name: str, cards: list[Card], refs: dict, custom_decks: dict, uid: str,
                face_down: bool = True) -> dict | None:
    contained = []
    for card in cards:
        for n in range(card.copies):
            contained.append(card_object(card, refs[card.id], custom_decks, f"{uid}|{card.id}|{n}", face_down))
    if not contained:
        return None
    if len(contained) == 1:
        single = contained[0]
        single["Nickname"] = cards[0].name
        return single
    keys = sorted({refs[c.id][0] for c in cards})
    return {
        "GUID": guid(uid), "Name": "Deck", "Transform": transform(face_down), "Nickname": name,
        "Description": "", "DeckIDs": [c["CardID"] for c in contained],
        "CustomDeck": {str(k): custom_decks[k] for k in keys}, "ContainedObjects": contained,
        "Hands": False, "HideWhenFaceDown": True, "SidewaysCard": False,
        "ColorDiffuse": {"r": 0.713, "g": 0.713, "b": 0.713}, **COMMON,
    }


def saved_object(bag: dict) -> dict:
    return {"SaveName": "", "GameMode": "", "Gravity": 0.5, "PlayArea": 0.5, "Date": "",
            "Table": "", "Sky": "", "Note": "", "Rules": "", "XmlUI": "", "LuaScript": "",
            "LuaScriptState": "", "ObjectStates": [bag], "TabStates": {}, "VersionNumber": ""}


# ---------------------------------------------------------------- TTS folder and git

def windows_documents() -> Path | None:
    """The real Documents folder, even when OneDrive has moved it."""
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        u = uuid.UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")  # FOLDERID_Documents
        g = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        out = ctypes.c_wchar_p()
        fn = ctypes.windll.shell32.SHGetKnownFolderPath
        fn.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
        if fn(ctypes.byref(g), 0, None, ctypes.byref(out)) == 0 and out.value:
            path = Path(out.value)
            ctypes.windll.ole32.CoTaskMemFree(out)
            return path
    except Exception:
        pass
    return None


def tts_saved_objects_dir(cfg: dict) -> Path | None:
    custom = cfg.get("tts", {}).get("saved_objects_dir", "").strip()
    if custom:
        return Path(os.path.expandvars(os.path.expanduser(custom)))
    tail = Path("My Games/Tabletop Simulator/Saves/Saved Objects")
    candidates = []
    if sys.platform == "win32":
        docs = windows_documents()
        if docs:
            candidates.append(docs / tail)
        if os.environ.get("OneDrive"):
            candidates.append(Path(os.environ["OneDrive"]) / "Documents" / tail)
        candidates.append(Path.home() / "Documents" / tail)
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library/Tabletop Simulator/Saves/Saved Objects")
    else:
        candidates.append(Path.home() / ".local/share/Tabletop Simulator/Saves/Saved Objects")
    for path in candidates:
        if path.parent.exists():   # the Saves folder exists once TTS has run
            return path
    return None


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def commit_and_push(message: str) -> None:
    if not (ROOT / ".git").exists():
        sys.exit("This folder isn't a git repo yet. See 'First-time setup' in README.md.")
    git("add", "-A")
    if git("status", "--porcelain").stdout.strip():
        result = git("commit", "-m", message)
        if result.returncode:
            sys.exit("git commit failed:\n" + (result.stderr or result.stdout))
        print(f"Committed: {message}")
    else:
        print("Nothing new to commit.")
    result = git("push")
    if result.returncode:
        sys.exit("git push failed, so TTS can't see the new images yet:\n" + result.stderr)
    print("Pushed to GitHub.")


# ---------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser(description="Build Monument's cards for Tabletop Simulator.")
    parser.add_argument("--push", action="store_true", help="commit and push after building")
    parser.add_argument("--local", action="store_true", help="use image paths on this PC instead of GitHub")
    parser.add_argument("--no-tts", action="store_true", help="don't copy into TTS's Saved Objects folder")
    args = parser.parse_args()
    if args.push and args.local:
        sys.exit("Use either --push or --local, not both.")

    cfg, style = load_toml(ROOT / "config.toml"), load_toml(ROOT / "style.toml")
    game = cfg["game"]
    if not args.local and cfg["github"]["user"] in ("", PLACEHOLDER_USER):
        sys.exit("Set your GitHub username in config.toml, or build with --local for solo testing.")

    cards = load_cards(CARDS_CSV, style, game.get("version", ""))
    fonts = Fonts(style)
    for folder in (IMAGES, PREVIEW, TTS_OUT):
        folder.mkdir(parents=True, exist_ok=True)

    faces = {card.id: render_card(card, style, fonts) for card in cards}
    for card in cards:
        slug = re.sub(r"[^a-z0-9]+", "-", card.name.lower()).strip("-")
        faces[card.id].save(PREVIEW / f"{card.id}-{slug}.png")

    written: set[str] = set()
    custom_decks: dict[int, dict] = {}
    refs: dict[str, tuple[int, int]] = {}
    by_group = {g: [c for c in cards if c.deck == g] for g in GROUPS}
    key = 1
    sheet_count = 0
    for group, group_cards in by_group.items():
        if not group_cards:
            continue
        back = render_back(group, game["name"], style, fonts)
        back.save(PREVIEW / f"back-{group.lower()}.png")
        back_url = image_url(write_hashed(back, f"back-{group.lower()}", written), cfg, args.local)
        for n, (sheet, cols, rows, chunk) in enumerate(build_sheets(group_cards, faces, style), start=1):
            face_url = image_url(write_hashed(sheet, f"{group.lower()}-{n}", written), cfg, args.local)
            custom_decks[key] = {"FaceURL": face_url, "BackURL": back_url, "NumWidth": cols,
                                 "NumHeight": rows, "BackIsHidden": True, "UniqueBack": False, "Type": 0}
            for index, card in enumerate(chunk):
                refs[card.id] = (key, index)
            key += 1
            sheet_count += 1

    # One bag: shared cards first, then each player's starting deck and draft pool.
    label = f"{game['name']} {game.get('version', '')}".strip()
    contents = []
    shared = deck_object("Shared cards", by_group["Shared"], refs, custom_decks, "shared", face_down=False)
    if shared:
        contents.append(shared)
    for p in range(1, int(game.get("players", 2)) + 1):
        for group in ("Starting", "Draft"):
            obj = deck_object(f"Player {p} {DECK_NAMES[group].lower()}", by_group[group],
                              refs, custom_decks, f"p{p}|{group}")
            if obj:
                contents.append(obj)
    built = datetime.now().strftime("%Y-%m-%d %H:%M")
    bag = {"GUID": guid("bag", label), "Name": "Bag",
           "Transform": {"posX": 0, "posY": 1, "posZ": 0, "rotX": 0, "rotY": 0, "rotZ": 0,
                         "scaleX": 1, "scaleY": 1, "scaleZ": 1},
           "Nickname": label, "Description": f"Built {built}. Take out each player's starting deck and draft pool.",
           "ColorDiffuse": {"r": 0.61, "g": 0.48, "b": 0.12}, "ContainedObjects": contents, **COMMON}

    json_path = TTS_OUT / f"{game['name']}.json"
    json_path.write_text(json.dumps(saved_object(bag), indent=2), encoding="utf-8")
    thumb = render_back("Shared" if by_group["Shared"] else "Draft", game["name"], style, fonts)
    thumb.thumbnail((256, 256))
    thumb.save(TTS_OUT / f"{game['name']}.png")

    for old in IMAGES.glob("*.png"):          # drop images from earlier builds
        if old.name not in written:
            old.unlink()

    total = sum(c.copies for c in cards if c.deck in PER_PLAYER) * int(game.get("players", 2)) + \
        sum(c.copies for c in by_group["Shared"])
    print(f"Rendered {len(cards)} card designs onto {sheet_count} sheets ({total} cards in the bag).")
    print(f"Previews: {PREVIEW}")

    if args.push:
        commit_and_push(f"Build cards ({len(cards)} designs, {built})")

    if not args.no_tts:
        target = tts_saved_objects_dir(cfg)
        if target is None:
            print("Couldn't find TTS's Saved Objects folder. Set saved_objects_dir in config.toml,\n"
                  f"or copy {json_path.name} and {json_path.stem}.png there yourself.")
        else:
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(json_path, target / json_path.name)
            shutil.copy2(TTS_OUT / f"{game['name']}.png", target / f"{game['name']}.png")
            print(f"Copied to TTS: {target / json_path.name}")
    if not args.push and not args.local:
        print("Reminder: push to GitHub before loading in TTS, or the new images won't load.")
    print(f"In TTS: Objects > Saved Objects > {game['name']}")


if __name__ == "__main__":
    main()
