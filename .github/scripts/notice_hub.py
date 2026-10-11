#!/usr/bin/env python3
"""
NoticeHub publisher — HPRC Scanner  (GitHub Action theke chole)

Layout (HPRC_Scanner/NoticeHub/):
  NoticeTexts/YYYY-MM.json         ← notice gulo (bot / admin edit kore)
  NoticeDocuments/YYYY-MM/<file>   ← attachment — notice-er date-er month folder-e
  NoticeDocumentsBin/YYYY-MM/      ← bot-e delete kora document
  index.json, registry.json        ← ei script banay

Commands:
  normalize   validate + date ISO + faka id-e auto ID + addedAt/updatedAt stamp + registry update
  check       normalize-er dry run (kichu lekhe na) — push-er age local-e chalate paro
  index       index.json (app ei file prothome pore)

Rules (bhul thakle FAIL → index publish HOY NA → app-e ager thik data-i thake):
  • file naam YYYY-MM.json, bhetore JSON array
  • protiti notice-e title ar date thakte hobe; date-er month == file-er month
  • id faka rakho → "YYYYMMDD-NN" auto। Ekbar dewa id bodlay na, delete kora id-o reuse hoy na
  • mediaName-e shudhu file-er naam (folder = notice-er month)
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

HUB_DIR = Path(os.environ.get("NOTICE_HUB_DIR", "HPRC_Scanner/NoticeHub"))
TEXTS_DIR = HUB_DIR / "NoticeTexts"
DOCS_DIR = HUB_DIR / "NoticeDocuments"
REGISTRY = HUB_DIR / "registry.json"
INDEX = HUB_DIR / "index.json"

SCHEMA = 1
RECENT_LIMIT = 40

MONTH_FILE_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])\.json$")
ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
KNOWN_FIELDS = ["id", "date", "title", "descriptionENG", "descriptionBNG",
                "category", "subCategory", "mediaName", "addedAt", "updatedAt"]
CONTENT_FIELDS = ["date", "title", "descriptionENG", "descriptionBNG",
                  "category", "subCategory", "mediaName"]

_errors: list[str] = []
_warnings: list[str] = []


# ───────────────────────── helpers ─────────────────────────
def _annotate(kind: str, msg: str, file: Path | None) -> None:
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{kind}{f' file={file}' if file else ''}::{msg}")
    else:
        print(f"[{kind.upper()}] {file or ''} {msg}")


def error(msg: str, file: Path | None = None) -> None:
    _errors.append(msg)
    _annotate("error", msg, file)


def warn(msg: str, file: Path | None = None) -> None:
    _warnings.append(msg)
    _annotate("warning", msg, file)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_date(raw) -> str | None:
    """'2026-10-11' / '11/10/2026' / '11-10-2026' → '2026-10-11'"""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = m.groups()
    else:
        m = re.match(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})$", s)
        if not m:
            return None
        d, mo, y = m.groups()
    try:
        return dt.date(int(y), int(mo), int(d)).isoformat()
    except ValueError:
        return None


def parse_ts(v) -> dt.datetime | None:
    if isinstance(v, (int, float)) and v > 0:
        return dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v, dt.timezone.utc)
    if isinstance(v, str) and v.strip():
        try:
            t = dt.datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
            return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            return None
    return None


def ts_iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_ms(v) -> int:
    t = parse_ts(v)
    return int(t.timestamp() * 1000) if t else 0


def split_csv(s) -> list[str]:
    return [p.strip() for p in s.split(",") if p.strip()] if isinstance(s, str) else []


def is_link(s: str) -> bool:
    return s.lower().startswith(("http://", "https://"))


def content_hash(n: dict) -> str:
    payload = json.dumps({k: n.get(k, "") for k in CONTENT_FIELDS}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def dump_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def month_files() -> list[Path]:
    if not TEXTS_DIR.exists():
        return []
    return sorted(p for p in TEXTS_DIR.iterdir() if p.is_file() and p.suffix == ".json")


def read_month(path: Path) -> list | None:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        error(f"Cannot read file: {e}", path)
        return None
    if not text.strip():
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        error(f"Invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}", path)
        return None
    if not isinstance(data, list):
        error("File must be a JSON array of notices", path)
        return None
    return data


def load_registry() -> dict:
    if not REGISTRY.exists():
        return {"notices": {}}
    try:
        reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
        reg.setdefault("notices", {})
        return reg
    except (OSError, json.JSONDecodeError):
        warn("registry.json unreadable — rebuilding (addedAt in files is kept)")
        return {"notices": {}}


# ───────────────────────── normalize ─────────────────────────
def cmd_normalize(args) -> int:
    dry = args.dry_run
    files = month_files()
    if not files:
        error(f"No notice files found in {TEXTS_DIR}")
        return finish()

    registry = load_registry()
    reg_notices: dict = registry["notices"]
    now = now_iso()
    parsed: dict[Path, list[dict]] = {}
    seen_ids: dict[str, Path] = {}

    # ── 1. validate ──
    for path in files:
        m = MONTH_FILE_RE.match(path.name)
        if not m:
            error("File name must be YYYY-MM.json (e.g. 2026-10.json)", path)
            continue
        file_month = f"{m.group(1)}-{m.group(2)}"
        data = read_month(path)
        if data is None:
            continue
        out: list[dict] = []
        for i, raw in enumerate(data):
            if not isinstance(raw, dict):
                error(f"notice #{i + 1}: must be a JSON object", path)
                continue
            n = dict(raw)
            for k in ("id", "title", "date", "category", "subCategory", "mediaName"):
                if n.get(k) is not None and not isinstance(n[k], str):
                    n[k] = str(n[k])
                if isinstance(n.get(k), str):
                    n[k] = n[k].strip()
            nid = n.get("id") or ""
            where = f"notice #{i + 1}" + (f" (id '{nid}')" if nid else "")

            if nid and not ID_RE.match(nid):
                error(f"{where}: id may contain only letters, digits, '-', '_', '.'", path)
            elif nid in seen_ids:
                error(f"Duplicate id '{nid}' (also in {seen_ids[nid].name})", path)
            elif nid:
                seen_ids[nid] = path
            if not n.get("title"):
                error(f"{where}: 'title' is required", path)

            iso = normalize_date(n.get("date"))
            if not iso:
                error(f"{where}: invalid date '{n.get('date')}' (use YYYY-MM-DD)", path)
            elif iso[:7] != file_month:
                error(f"{where}: date {iso} belongs to {iso[:7]}.json, not {path.name}", path)
            else:
                n["date"] = iso

            n["descriptionENG"] = n.get("descriptionENG") or ""
            n["descriptionBNG"] = n.get("descriptionBNG") or ""
            n["category"] = n.get("category") or "General"
            n["subCategory"] = n.get("subCategory") or "General"
            n["mediaName"] = n.get("mediaName") or ""
            if not n["descriptionENG"] and not n["descriptionBNG"]:
                warn(f"{where}: has no description", path)

            for media in split_csv(n["mediaName"]):
                if is_link(media):
                    continue
                if "/" in media or "\\" in media:
                    error(f"{where}: mediaName '{media}' must be a plain file name (folder is the notice month)", path)
                elif iso and not (DOCS_DIR / iso[:7] / media).exists():
                    warn(f"{where}: document '{media}' not found in NoticeDocuments/{iso[:7]}/", path)
            out.append(n)
        parsed[path] = out

    if _errors:
        return finish()

    # ── 2. faka id → "YYYYMMDD-NN" (registry-r deleted id-o bad, tai kokhono reuse hoy na) ──
    taken = set(seen_ids) | set(reg_notices)
    for path, notices in parsed.items():
        for n in notices:
            if n.get("id"):
                continue
            base = n["date"].replace("-", "")
            k = 1
            while f"{base}-{k:02d}" in taken:
                k += 1
            n["id"] = f"{base}-{k:02d}"
            taken.add(n["id"])
            print(f"assigned id {n['id']} → '{n.get('title', '')[:50]}' ({path.name})")

    # ── 3. addedAt / updatedAt ──
    present: set[str] = set()
    for path, notices in parsed.items():
        for n in notices:
            nid = n["id"]
            present.add(nid)
            h = content_hash(n)
            reg = reg_notices.get(nid)
            if reg and reg.get("deleted"):
                warn(f"id '{nid}' was deleted on {reg['deleted']} and is used again", path)
                reg = {k: v for k, v in reg.items() if k != "deleted"}

            added = parse_ts(n.get("addedAt")) or (parse_ts(reg.get("addedAt")) if reg else None) or parse_ts(now)
            added_iso = ts_iso(added)
            if reg and reg.get("hash") and reg["hash"] != h:
                updated_iso = now
            else:
                upd = parse_ts(n.get("updatedAt")) or (parse_ts(reg.get("updatedAt")) if reg else None)
                updated_iso = ts_iso(upd) if upd else added_iso

            n["addedAt"] = added_iso
            n["updatedAt"] = updated_iso
            reg_notices[nid] = {"month": path.stem, "hash": h, "addedAt": added_iso, "updatedAt": updated_iso}

    for nid, reg in reg_notices.items():
        if nid not in present and not reg.get("deleted"):
            reg["deleted"] = now

    # ── 4. write back (field order fixed, extra fields kept) ──
    changed = 0
    for path, notices in parsed.items():
        ordered = []
        for n in notices:
            o = {k: n[k] for k in KNOWN_FIELDS if k in n}
            o.update({k: v for k, v in n.items() if k not in o})
            ordered.append(o)
        text = dump_json(ordered)
        if path.read_text(encoding="utf-8-sig") != text:
            changed += 1
            if not dry:
                path.write_text(text, encoding="utf-8")
            print(f"{'would update' if dry else 'updated'}: {path}")

    registry["notices"] = dict(sorted(reg_notices.items()))
    if not dry:
        REGISTRY.write_text(dump_json(registry), encoding="utf-8")

    total = sum(len(v) for v in parsed.values())
    print(f"OK: {len(parsed)} month files, {total} notices, {changed} file(s) normalized")
    return finish()


# ───────────────────────── index ─────────────────────────
def cmd_index(args) -> int:
    months = []
    cat_count: dict[str, int] = {}
    sub_count: dict[str, dict[str, int]] = {}
    everything: list[tuple[str, dict]] = []

    for path in month_files():
        if not MONTH_FILE_RE.match(path.name):
            continue
        data = read_month(path)
        if not data:
            continue  # faka month publish hoy na
        month_cats: dict[str, set] = {}
        for n in data:
            cats = split_csv(n.get("category")) or ["General"]
            subs = split_csv(n.get("subCategory")) or ["General"]
            for c in cats:
                cat_count[c] = cat_count.get(c, 0) + 1
                month_cats.setdefault(c, set()).update(subs)
                for s in subs:
                    sub_count.setdefault(c, {})[s] = sub_count.get(c, {}).get(s, 0) + 1
            everything.append((path.stem, n))
        months.append({
            "month": path.stem,
            "path": f"NoticeTexts/{path.name}",
            "version": hashlib.sha256(path.read_bytes()).hexdigest()[:16],
            "count": len(data),
            "categories": {c: sorted(s) for c, s in sorted(month_cats.items())},
        })

    if _errors:
        return finish()

    months.sort(key=lambda m: m["month"], reverse=True)
    by_count = lambda x: (-x[1], x[0].lower())  # noqa: E731
    categories = [
        {"name": c, "count": cnt,
         "sub": [{"name": s, "count": sc} for s, sc in sorted(sub_count[c].items(), key=by_count)]}
        for c, cnt in sorted(cat_count.items(), key=by_count)
    ]
    recent = sorted(
        ({"id": n["id"], "month": mo, "addedAt": ts_ms(n.get("addedAt"))} for mo, n in everything if n.get("id")),
        key=lambda r: r["addedAt"], reverse=True,
    )[:RECENT_LIMIT]

    min_app = 0
    if INDEX.exists():
        try:
            min_app = int(json.loads(INDEX.read_text(encoding="utf-8")).get("minAppVersion", 0))
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    env_min = (args.min_app_version or os.environ.get("MIN_APP_VERSION") or "").strip()
    if env_min:
        min_app = int(env_min)

    INDEX.write_text(dump_json({
        "schema": SCHEMA,
        "generatedAt": now_iso(),
        "ref": args.ref or "",
        "minAppVersion": min_app,
        "totalNotices": len(everything),
        "categories": categories,
        "months": months,
        "recent": recent,
    }), encoding="utf-8")
    print(f"index.json: {len(months)} months, {len(everything)} notices, ref={args.ref or '(main)'}")
    return finish()


# ───────────────────────── main ─────────────────────────
def finish() -> int:
    if _warnings:
        print(f"{len(_warnings)} warning(s)")
    if _errors:
        print(f"FAILED with {len(_errors)} error(s) — nothing was published.")
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("normalize")
    n.add_argument("--dry-run", action="store_true")
    sub.add_parser("check")
    i = sub.add_parser("index")
    i.add_argument("--ref", default="", help="commit SHA that contains these files")
    i.add_argument("--min-app-version", default="")

    args = p.parse_args()
    if args.cmd == "check":
        args.dry_run = True
        return cmd_normalize(args)
    return {"normalize": cmd_normalize, "index": cmd_index}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
