"""Heal manifest artist/title metadata — offline, no lyric fetches.

Prepared libraries processed before the metadata parser matured carry
video-upload metadata: the artist field holds the uploader channel
("RHINO", "Whitesnake TV", "Leadhead444") and the title field holds
the raw video title ("Skid Row - 18 And Life (Official Music Video)").
The catalog, the artist/title folder names and phone search all read
those fields, so they deserve real values.

Rules, in order:

1. explicit repairs for records that lyric providers got wrong
   (swapped artist/title, mojibake, filename-style track names)
2. "Artist - Title" still sitting in the title field gets parsed
   out — works whether artist is the channel ("RHINO - Skid Row - X")
   or a duplicated prefix ("Iron Maiden - Iron Maiden - X")
3. channel decorations on the artist itself (VEVO / - Topic /
   " Official...") get stripped
4. track-number prefixes ("23.The Promise") get stripped

    python -m station.fixmeta <root> [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from ultrapistar.lyrics import _clean_title, _chan_clean

# (bad artist, bad title) → (artist, title): records lyric providers
# returned malformed — swapped fields, mojibake, filename titles
_REPAIRS = {
    ("Is This Love", "Whitesnake"):
        ("Whitesnake", "Is This Love"),
    ("Hold On My Heart", "Genesis"):
        ("Genesis", "Hold On My Heart"),
    ("Desire", "Gene Loves Jezebel"):
        ("Gene Loves Jezebel", "Desire"),
    ("P", "Both Sides of the Story"):
        ("Phil Collins", "Both Sides of the Story"),
    ("Phil Collins",
     "Phil_Collins_-_01_-_Youll_Be_In_My_Heart_Phil_Version"):
        ("Phil Collins", "You'll Be In My Heart"),
    ("Van Halen", "Runnin� with the devil"):
        ("Van Halen", "Runnin' with the Devil"),
    ("Poison", "Nothin� but a Good Time"):
        ("Poison", "Nothin' but a Good Time"),
    ("The Triffids", "Wide Open Roa�"):
        ("The Triffids", "Wide Open Road"),
    ("The Replacements", "I�ll Be You"):
        ("The Replacements", "I'll Be You"),
    ("Rage Against The Machine",
     "01_-__-_Rage_Against_the_Machine_-__Killing_in_the_name"):
        ("Rage Against the Machine", "Killing in the Name"),
    ("Thomas Dolby", "She Blinded Me With Sience"):
        ("Thomas Dolby", "She Blinded Me with Science"),
    ("Ministry", 'Over The Shoulder (12" Version'):
        ("Ministry", "Over the Shoulder"),
    ("Information Society", "What's On Your Mind (pure Ener"):
        ("Information Society", "What's On Your Mind (Pure Energy)"),
    ("Paramore", "Paramore: Misery Business"):
        ("Paramore", "Misery Business"),
    ("Paramore", "Paramore-Crushcrushcrush"):
        ("Paramore", "Crushcrushcrush"),
    ("Nickelback", "Rockstar- Nickelback"):
        ("Nickelback", "Rockstar"),
    ("Green Day", "Wake Me Up When September Ends-Osc"):
        ("Green Day", "Wake Me Up When September Ends"),
    ("New Order", "Blue Monday �88"):
        ("New Order", "Blue Monday-88"),
    ("TOPPOP: It's Immaterial", "Driving Away From Home (Jim's Tune)"):
        ("It's Immaterial", "Driving Away From Home (Jim's Tune)"),
    ("MisterBouncyBounce", "Pulling Mussels (From the Shell) Squeeze"):
        ("Squeeze", "Pulling Mussels (From the Shell)"),
    ("Mike + The Mechanics", "Taken In (Remastered 2005)"):
        ("Mike + The Mechanics", "Taken In"),
    ("Mike + The Mechanics", "Over My Shoulder (Remastered 2005)"):
        ("Mike + The Mechanics", "Over My Shoulder"),
    ("Mike + The Mechanics", "Nobody's Perfect (Remastered 2005)"):
        ("Mike + The Mechanics", "Nobody's Perfect"),
    ("Mike + The Mechanics", "Nobody Knows (Remastered 2005)"):
        ("Mike + The Mechanics", "Nobody Knows"),
    ("Genesis", "No Son Of Mine"):
        ("Genesis", "No Son of Mine"),
}

_SEPS = (" - ", " – ", " — ", " | ", " ~ ")
_TRACKNUM = re.compile(r"^\d+\s*[\._\-:]\s*")
_CHAN_PREFIX = re.compile(r"(?i)^[A-Z0-9 ]{2,12}:\s+")
_BRACKETED = re.compile(r"[\(\[][^\)\]]{0,90}[\)\]]")
_MV_PHRASE = re.compile(
    r"(?i)\s+(official\s+)?(music\s+video|video|audio|mv|m/v)\b.*$")
_TAG = re.compile(r"\s*#\w[\w\d]*\.?\s*$")
_QSUFFIX = re.compile(r"(?i)\s*-?\s*(hq|hd|4k|8k|remaster(?:ed)?)\s*\.?$")


def _norm(s: str) -> str:
    return re.sub(r"\W+", "", (s or "").lower())


def _tidy(t: str) -> str:
    """Video-title tail cleanup applied to parsed parts: drop the
    '(Official) Music Video' tail phrase, trailing #tags and quality
    suffixes before the shared bracket/junk clean. The MV phrase must
    not cut inside a bracketed group — '(Official Music Video)' is
    removed whole by _clean_title, and '(I'm) Stranded' is real."""
    spans = [m.span() for m in _BRACKETED.finditer(t)]
    cut = len(t)
    for m in _MV_PHRASE.finditer(t):
        if not any(s <= m.start() < e for s, e in spans):
            cut = m.start()
            break
    t = _TAG.sub("", t[:cut])
    t = _QSUFFIX.sub("", t)
    return _clean_title(t)


def _split_video_title(title: str) -> tuple[str, str] | None:
    """'Skid Row - 18 And Life (Official Music Video)' →
    ('Skid Row', '18 And Life'). Splits on a bracket-stripped copy so
    '(2007 - Remaster)' can't masquerade as a separator."""
    t = title.strip()
    bare = _BRACKETED.sub("()", t)           # keep parens, blank inside
    for sep in _SEPS:
        if sep in bare:
            # split the ORIGINAL at the same position
            idx = bare.index(sep)
            a, b = t[:idx], t[idx + len(sep):]
            a, b = _tidy(a), _tidy(b)
            # a preposition-starting tail is video-junk, not a title:
            # '... - from THE COMPLETE ALBUMS COLLECTION'
            if re.match(r"(?i)^(from|the|of|at|on|in)\b", b):
                return None
            if len(_norm(a)) >= 2 and len(_norm(b)) >= 2:
                b = _TRACKNUM.sub("", b).strip() or b
                # 'Title - Album' tails: 'Put Down That Weapon -
                # Diesel And Dust' → keep the song part
                for sep2 in _SEPS:
                    if sep2 in b:
                        head = _tidy(b.split(sep2, 1)[0])
                        if len(_norm(head)) >= 2:
                            b = head
                        break
                return a, b
            return None
    m = re.match(r"^([A-Za-z][A-Za-z0-9 '&\.]{1,40})-([^-]{3,80})$", t)
    if m:                                   # 'ABC-How To Be A Millionaire'
        a, b = _tidy(m.group(1)), _tidy(m.group(2))
        if (len(_norm(a)) >= 2 and len(_norm(b)) >= 2
                and not _norm(b).isdigit()):
            return a, b
    return None


def heal(song: dict) -> tuple[str, str] | None:
    """Compute repaired (artist, title) for one manifest record —
    or None when the metadata is already sane."""
    artist, title = song.get("artist") or "", song.get("title") or ""
    if not title:
        return None
    if (artist, title) in _REPAIRS:
        return _REPAIRS[(artist, title)]

    a2, t2 = artist, title
    if t2.startswith("yt-") or t2.startswith("upload-"):
        return None                     # opaque id — nothing to parse

    # '23.The Promise' / '2.Planet Earth' / '01 Papercut'
    t2 = _TRACKNUM.sub("", t2).strip() or t2
    # residue from stripped bracketed junk: 'Monkey Business (' →
    # 'Monkey Business'
    t2 = t2.rstrip(" ([{").rstrip() or t2

    # video title still holding 'Artist - Title'
    split = _split_video_title(t2)
    if split:
        pa, pt = split
        if _norm(pt) and pt != t2:
            if _norm(pa) == _norm(a2):
                # 'Artist - Artist - Title' → drop duplicated prefix
                t2 = pt
            elif _norm(pt) == _norm(a2):
                # 'Channel - Song - Artist' reprise — parsed tail is
                # the artist name, so the song is the left part
                t2 = pa
            else:
                a2, t2 = pa, pt
    else:
        # only bother cleaning channel junk off the artist when the
        # record is actually messy — 'Roxy Music' is a real name and
        # must survive a clean 'Roxy Music - Avalon'
        a2 = _CHAN_PREFIX.sub("", a2).strip() or a2   # 'TOPPOP: X'
        if (a2, t2) != (artist, title):
            return a2 or artist, t2
        return None

    # trailing ' - <Artist>' reprise ('Rockstar- Nickelback')
    m = re.match(r"^(.*?)\s*-\s*" + re.escape(a2) + r"\s*$", t2,
                 re.IGNORECASE)
    if m and len(_norm(m.group(1))) >= 2:
        t2 = m.group(1).strip()

    if (a2, t2) != (artist, title) and _norm(t2):
        return a2 or artist, t2
    return None


def fix_meta(root: Path, dry: bool = False) -> dict:
    root = Path(root)
    doc = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    songs = doc.get("songs") if isinstance(doc, dict) else doc
    changed = 0
    for s in songs or []:
        fixed = heal(s)
        if not fixed:
            continue
        if dry:
            print(f'  {s.get("artist")} - {s.get("title")}'
                  f'  ->  {fixed[0]} - {fixed[1]}')
        else:
            s["artist"], s["title"] = fixed
        changed += 1
    if changed and not dry:
        (root / "manifest.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=1),
            encoding="utf-8")
    return {"fixed": changed}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m station.fixmeta")
    ap.add_argument("root", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    out = fix_meta(args.root, dry=args.dry_run)
    print(("would fix" if args.dry_run else "fixed"), out["fixed"],
          "records")


if __name__ == "__main__":
    main()
