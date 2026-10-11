#!/usr/bin/env python3
"""
NoticeHub publisher — HPRC Scanner

Layout (HPRC_Scanner/NoticeHub/):
  NoticeTexts/YYYY-MM.json         ← notice gulo (admin / bot edit kore)
  NoticeDocuments/YYYY-MM/<file>   ← attachment — notice-er date-er month folder-e
  NoticeDocumentsBin/YYYY-MM/      ← bot-e delete kora document
  index.json, registry.json        ← ei script banay

Commands (GitHub Action theke chole):

  normalize   validate + date ISO kora + addedAt/updatedAt stamp + registry update
  index       index.json (manifest) toiri, + purono app-er jonno NoticeHub.json (legacy export)
  check       normalize-er dry run (kichu lekhe na) — local-e push-er age chalate paro
  split       ekbar-i: purono NoticeHub.json ke month file-e bhag kora
  migrate-media  ekbar-i: purono Media/ folder theke notice-er document NoticeDocuments/YYYY-MM/-e COPY

Rules (validation fail hole index publish HOY NA, tai app-e bhanga data jay na):
  • file naam YYYY-MM.json, bhetore JSON array (ba {"notices": [...]})
  • protiti notice-er title, date thakte hobe
  • id FAKA rakho — Action date dekhe nijei "YYYYMMDD-NN" debe (20261011-01, -02 ...)।
    Ekbar dewa id kokhono bodlay na (starred eta diye chole)। Haat-e dile shob month mile unique hote hobe.
  • date-er month == file-er month
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
MONTHS_DIR = HUB_DIR / "NoticeTexts"
REGISTRY = HUB_DIR / "registry.json"
INDEX = HUB_DIR / "index.json"
DOCS_DIR = HUB_DIR / "NoticeDocuments"
LEGACY_FILE = Path(os.environ.get("NOTICE_LEGACY_FILE", "HPRC_Scanner/NoticeHub.json"))

SCHEMA = 1
RECENT_LIMIT = 40
LEGACY_EXPORT_COUNT = int(os.environ.get("LEGACY_EXPORT_COUNT", "80"))

MONTH_FILE_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])\.json$")
KNOWN_FIELDS = ["id", "date", "title", "descriptionENG", "descriptionBNG",
                "category", "subCategory", "mediaName", "addedAt", "updatedAt"]
CONTENT_FIELDS = ["date", "title", "descriptionENG", "descriptionBNG",
                  "category", "subCategory", "mediaName"]

_errors: list[str] = []
_warnings: list[str] = []


# ───────────────────────── helpers ─────────────────────────
def gh_annotation(kind: str, msg: str, file: Path | None = None) -> None:
    loc = f" file={file}" if file else ""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{kind}{loc}::{msg}")
    else:
        print(f"[{kind.upper()}] {file or ''} {msg}")


def error(msg: str, file: Path | None = None) -> None:
    _errors.append(msg)
    gh_annotation("error", msg, file)


def warn(msg: str, file: Path | None = None) -> None:
    _warnings.append(msg)
    gh_annotation("warning", msg, file)


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


def iso_to_dmy(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}/{m}/{y}"


def parse_ts(v) -> dt.datetime | None:
    if isinstance(v, (int, float)) and v > 0:
        return dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v, dt.timezone.utc)
    if isinstance(v, str) and v.strip():
        s = v.strip().replace("Z", "+00:00")
        try:
            t = dt.datetime.fromisoformat(s)
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
    if not isinstance(s, str):
        return []
    return [p.strip() for p in s.split(",") if p.strip()]


def is_link(s: str) -> bool:
    return s.lower().startswith(("http://", "https://"))


def content_hash(n: dict) -> str:
    payload = json.dumps({k: n.get(k, "") for k in CONTENT_FIELDS}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def dump_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def month_files() -> list[Path]:
    if not MONTHS_DIR.exists():
        return []
    return sorted(p for p in MONTHS_DIR.iterdir() if p.is_file() and p.suffix == ".json")


def read_month(path: Path) -> list | None:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except Exception as e:  # noqa: BLE001
        error(f"Cannot read file: {e}", path)
        return None
    if not text.strip():
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        error(f"Invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}", path)
        return None
    if isinstance(data, dict) and isinstance(data.get("notices"), list):
        data = data["notices"]
    if not isinstance(data, list):
        error("Month file must be a JSON array of notices", path)
        return None
    return data


def load_registry() -> tuple[dict, bool]:
    if not REGISTRY.exists():
        return {"notices": {}}, True
    try:
        reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
        reg.setdefault("notices", {})
        return reg, False
    except Exception:  # noqa: BLE001
        warn("registry.json unreadable — rebuilding (addedAt of existing notices kept from files)")
        return {"notices": {}}, False


# ───────────────────────── normalize ─────────────────────────
def cmd_normalize(args) -> int:
    dry = args.dry_run
    files = month_files()
    if not files:
        error(f"No month files found in {MONTHS_DIR}")
        return finish()

    registry, bootstrap = load_registry()
    if args.bootstrap:
        bootstrap = True
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
            where = f"notice #{i + 1}"
            if not isinstance(raw, dict):
                error(f"{where}: must be a JSON object", path)
                continue
            n = dict(raw)
            for k in ("id", "title", "date", "descriptionENG", "descriptionBNG", "category", "subCategory", "mediaName"):
                if k in n and n[k] is not None and not isinstance(n[k], str):
                    n[k] = str(n[k])
                if isinstance(n.get(k), str):
                    n[k] = n[k].strip() if k not in ("descriptionENG", "descriptionBNG") else n[k]
            if "description" in n and not n.get("descriptionENG"):
                n["descriptionENG"] = n.pop("description")
            nid = n.get("id") or ""
            where = f"notice #{i + 1} (id '{nid}')" if nid else where
            if not nid:
                pass  # auto-generate hobe (date valid hole), niche
            elif not re.match(r"^[A-Za-z0-9_.\-]+$", nid):
                error(f"{where}: id may contain only letters, digits, '-', '_', '.'", path)
            elif nid in seen_ids:
                error(f"Duplicate id '{nid}' (also in {seen_ids[nid].name})", path)
            else:
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

    # ── 1b. faka id → "YYYYMMDD-NN" (registry-r purono/deleted id-o bad diye, tai kokhono reuse hoy na) ──
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

    # ── 2. stamp addedAt / updatedAt using registry ──
    present: set[str] = set()
    for path, notices in parsed.items():
        month = path.stem
        for n in notices:
            nid = n["id"]
            present.add(nid)
            h = content_hash(n)
            reg = reg_notices.get(nid)
            if reg and reg.get("deleted"):
                warn(f"id '{nid}' was deleted on {reg['deleted']} and is used again", path)
                reg = {k: v for k, v in reg.items() if k != "deleted"}

            added = parse_ts(n.get("addedAt"))
            if added is None and reg and reg.get("addedAt"):
                added = parse_ts(reg["addedAt"])
            if added is None:
                if bootstrap:
                    added = dt.datetime.fromisoformat(n["date"]).replace(tzinfo=dt.timezone.utc)
                else:
                    added = parse_ts(now)
            added_iso = ts_iso(added)

            if reg and reg.get("hash") and reg["hash"] != h:
                updated_iso = now
            else:
                upd = parse_ts(n.get("updatedAt")) or (parse_ts(reg.get("updatedAt")) if reg else None)
                updated_iso = ts_iso(upd) if upd else added_iso

            n["addedAt"] = added_iso
            n["updatedAt"] = updated_iso
            reg_notices[nid] = {"month": month, "hash": h, "addedAt": added_iso, "updatedAt": updated_iso}

    for nid, reg in reg_notices.items():
        if nid not in present and not reg.get("deleted"):
            reg["deleted"] = now

    # ── 3. write back (field order fixed, extra fields kept) ──
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
    print(f"OK: {len(parsed)} month files, {total} notices, {changed} file(s) normalized"
          f"{' (bootstrap)' if bootstrap else ''}")
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
        if data is None:
            continue
        if not data:
            continue  # empty month = not published
        raw = path.read_bytes()
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
            "version": hashlib.sha256(raw).hexdigest()[:16],
            "count": len(data),
            "categories": {c: sorted(s) for c, s in sorted(month_cats.items())},
        })

    if _errors:
        return finish()

    months.sort(key=lambda m: m["month"], reverse=True)
    categories = [
        {"name": c, "count": cnt,
         "sub": [{"name": s, "count": sc} for s, sc in sorted(sub_count[c].items(), key=lambda x: (-x[1], x[0].lower()))]}
        for c, cnt in sorted(cat_count.items(), key=lambda x: (-x[1], x[0].lower()))
    ]
    recent = sorted(
        ({"id": n["id"], "month": mo, "addedAt": ts_ms(n.get("addedAt"))} for mo, n in everything if n.get("id")),
        key=lambda r: r["addedAt"], reverse=True,
    )[:RECENT_LIMIT]

    min_app = 0
    if INDEX.exists():
        try:
            min_app = int(json.loads(INDEX.read_text(encoding="utf-8")).get("minAppVersion", 0))
        except Exception:  # noqa: BLE001
            pass
    env_min = (args.min_app_version or os.environ.get("MIN_APP_VERSION") or "").strip()
    if env_min:
        min_app = int(env_min)

    index = {
        "schema": SCHEMA,
        "generatedAt": now_iso(),
        "ref": args.ref or "",
        "minAppVersion": min_app,
        "totalNotices": len(everything),
        "categories": categories,
        "months": months,
        "recent": recent,
    }
    INDEX.write_text(dump_json(index), encoding="utf-8")
    print(f"index.json: {len(months)} months, {len(everything)} notices, ref={args.ref or '(main)'}")

    if not args.no_legacy:
        export_legacy(everything)
    return finish()


def export_legacy(everything: list[tuple[str, dict]]) -> None:
    """Purono app version (single NoticeHub.json fetch kore) jate latest notice gulo pay."""
    items = sorted(everything, key=lambda x: (x[1].get("date", ""), x[1].get("addedAt", "")), reverse=True)
    out = []
    for _, n in items[:LEGACY_EXPORT_COUNT]:
        iso = normalize_date(n.get("date"))
        out.append({
            "id": n.get("id", ""),
            "date": iso_to_dmy(iso) if iso else n.get("date", ""),
            "title": n.get("title", ""),
            "descriptionENG": n.get("descriptionENG", ""),
            "descriptionBNG": n.get("descriptionBNG", ""),
            "category": n.get("category", "General"),
            "subCategory": n.get("subCategory", "General"),
            "mediaName": n.get("mediaName", ""),
        })
    LEGACY_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEGACY_FILE.write_text(dump_json(out), encoding="utf-8")
    print(f"legacy export: {LEGACY_FILE} ({len(out)} latest notices for old app versions)")


# ───────────────────────── split (one-time migration) ─────────────────────────
def tolerant_parse(text: str) -> list[dict]:
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else data.get("notices", [])
    except json.JSONDecodeError:
        warn("Legacy file is not valid JSON — parsing block by block")
    clean = text.strip()
    if clean.startswith("["):
        clean = clean[1:]
    if clean.endswith("]"):
        clean = clean[:-1]
    out = []
    for i, block in enumerate(re.split(r"\}\s*,?\s*\{", clean)):
        b = block.strip()
        b = b if b.startswith("{") else "{" + b
        b = b if b.endswith("}") else b + "}"
        try:
            out.append(json.loads(b))
        except json.JSONDecodeError as e:
            error(f"Legacy block #{i + 1} unreadable ({e.msg}) — fix it by hand")
    return out


def cmd_split(args) -> int:
    src = Path(args.source)
    notices = tolerant_parse(src.read_text(encoding="utf-8-sig"))
    by_month: dict[str, list] = {}
    unplaced = []
    seen = set()
    for i, n in enumerate(notices):
        if not isinstance(n, dict):
            continue
        n = dict(n)
        if "description" in n and not n.get("descriptionENG"):
            n["descriptionENG"] = n.pop("description")
        nid = str(n.get("id") or "").strip()
        if not nid:
            nid = "legacy-" + hashlib.sha1(f"{n.get('date')}|{n.get('title')}".encode()).hexdigest()[:10]
            warn(f"notice #{i + 1} '{n.get('title', '')[:40]}' had no id → '{nid}'")
        if nid in seen:
            error(f"Duplicate id '{nid}' in legacy file (notice #{i + 1}) — fix before splitting")
        seen.add(nid)
        n["id"] = nid
        iso = normalize_date(n.get("date"))
        if not iso:
            unplaced.append(n)
            continue
        n["date"] = iso
        by_month.setdefault(iso[:7], []).append(n)

    MONTHS_DIR.mkdir(parents=True, exist_ok=True)
    for month, items in sorted(by_month.items()):
        dest = MONTHS_DIR / f"{month}.json"
        if dest.exists() and not args.force:
            error(f"{dest} already exists (use --force to overwrite)")
            continue
        items.sort(key=lambda x: x["date"], reverse=True)
        ordered = [{**{k: x[k] for k in KNOWN_FIELDS if k in x}, **{k: v for k, v in x.items() if k not in KNOWN_FIELDS}} for x in items]
        dest.write_text(dump_json(ordered), encoding="utf-8")
        print(f"wrote {dest} ({len(items)} notices)")
    if unplaced:
        dest = HUB_DIR / "legacy_unplaced.json"
        dest.write_text(dump_json(unplaced), encoding="utf-8")
        warn(f"{len(unplaced)} notice(s) had no valid date → {dest}. Fix the date and move them into a month file.")
    return finish()


# ───────────────────────── migrate-media (one-time) ─────────────────────────
def cmd_migrate_media(args) -> int:
    """Protiti notice-er document purono flat folder theke NoticeDocuments/<notice month>/-e COPY kore।
    Purono folder chhoy na (purono app version ekhono oita theke download kore)।"""
    import shutil
    src_dir = Path(args.source)
    if not src_dir.is_dir():
        error(f"Source folder not found: {src_dir}")
        return finish()
    copied = skipped = 0
    missing: list[str] = []
    for path in month_files():
        data = read_month(path)
        if not data:
            continue
        for n in data:
            iso = normalize_date(n.get("date"))
            if not iso:
                continue
            for media in split_csv(n.get("mediaName")):
                if is_link(media):
                    continue
                dst = DOCS_DIR / iso[:7] / Path(media).name
                if dst.exists():
                    skipped += 1
                    continue
                src = src_dir / media
                if not src.is_file():
                    missing.append(f"{n.get('id')}: {media}")
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied += 1
                print(f"copied {src} → {dst}")
    for m in missing:
        warn(f"document not found in {src_dir}: {m}")
    print(f"migrate-media: {copied} copied, {skipped} already there, {len(missing)} missing")
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
    n.add_argument("--bootstrap", action="store_true",
                   help="new notices get addedAt = their date (use for the first migration run)")
    sub.add_parser("check")

    i = sub.add_parser("index")
    i.add_argument("--ref", default="", help="commit SHA that contains these month files")
    i.add_argument("--min-app-version", default="")
    i.add_argument("--no-legacy", action="store_true")

    mm = sub.add_parser("migrate-media")
    mm.add_argument("--source", default="HPRC_Scanner/Media", help="old flat media folder")

    s = sub.add_parser("split")
    s.add_argument("source", help="path to old NoticeHub.json")
    s.add_argument("--force", action="store_true")

    args = p.parse_args()
    if args.cmd == "check":
        args.dry_run, args.bootstrap = True, False
        return cmd_normalize(args)
    return {"normalize": cmd_normalize, "index": cmd_index, "split": cmd_split,
            "migrate-media": cmd_migrate_media}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
