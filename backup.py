#!/usr/bin/env python3
"""ClassRally backups: create, check, list and restore (standard library only).

Run it on the server that holds the data (cron or a systemd timer there, not
on a laptop that may be closed at night). Guide: docs/NASAZENI.md.

  python3 backup.py create  --data-root /srv/classrally --dest /var/backups/classrally
  python3 backup.py check   --dest /var/backups/classrally [--push-url-file FILE]
  python3 backup.py list    --dest /var/backups/classrally
  python3 backup.py restore /var/backups/classrally/daily/<file>.tar.gz --into /srv/classrally-restored

A backup holds the SQLite database (copied with SQLite's online backup API, so
it is consistent while the server runs), the game history, all question banks
(shared and the teachers' own) and uploaded images. Not included: the e-mail
outbox (one-time links) and the music in audio/, which comes with the
installation (--with-audio adds it).

Rotation: the newest backup of each of the last 7 days and the first backup of
each of the last 12 months (days and months in UTC, names carry UTC time).
`check` fails when the newest backup is older than --max-age-hours (default
26), its checksum or database does not verify, or the last run failed. With a
push URL (e.g. an Uptime Kuma push monitor) it reports up/down there as well,
so a check that stops running is noticed too.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path, PurePosixPath

try:
    import fcntl
except ImportError:  # Windows: no lock, backups are meant for Linux servers
    fcntl = None

DB_NAME = "classrally.db"
ARCHIVE_RE = re.compile(r"^classrally-(\d{8})-(\d{6})Z\.tar\.gz$")
KEEP_DAILY = 7
KEEP_MONTHLY = 12
MAX_AGE_HOURS = 26.0
MAX_MONTHLY_AGE_DAYS = 32
FUTURE_TOLERANCE_HOURS = 0.1  # clock skew allowed before an archive counts as "from the future"
MAX_MANIFEST_BYTES = 64 << 20   # a manifest only lists files; anything bigger is not ours
MAX_SKIPPED_LISTED = 50
PUSH_URL_ENV = "CLASSRALLY_BACKUP_PUSH_URL"
FOLDERS = ("data", "history", "questions", "uploads", "audio")

# Where the folders are, relative to --data-root or --app-dir. Archives always
# use the data-root names, so one archive restores into either layout.
LAYOUTS = {
    # Docker / server: one data root mounted into the container
    "data-root": {"data": "data", "history": "history", "questions": "questions",
                  "uploads": "uploads", "audio": "audio"},
    # server.py running straight from the source folder
    "app-dir": {"data": "data", "history": "history", "questions": "questions",
                "uploads": "static/uploads", "audio": "static/audio"},
}


class BackupError(Exception):
    pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _write_sidecar(archive: Path, digest: str) -> None:
    # same format as `sha256sum`, so `sha256sum -c` works in that folder
    _write_atomic(archive.with_name(archive.name + ".sha256"),
                  f"{digest}  {archive.name}\n".encode())


def _archive_name(now: float) -> str:
    # UTC: names sort in time order even across daylight-saving changes
    return time.strftime("classrally-%Y%m%d-%H%M%SZ.tar.gz", time.gmtime(now))


def _archives(folder: Path) -> list[str]:
    if not folder.is_dir():
        return []
    return sorted(n for n in os.listdir(folder) if ARCHIVE_RE.match(n))


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def _kb(size: int) -> str:
    return f"{max(1, round(size / 1024))} kB"


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("musi byt aspon 1")
    return number


def _shown(name: str) -> str:
    """A file name safe to print on one line and to store in JSON.

    Undecodable bytes and control characters (a newline in a name) become \\xNN.
    """
    text = name.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")
    return "".join(c if 32 <= ord(c) != 127 else f"\\x{ord(c):02x}" for c in text)


def _unusable_name(rel: str) -> str | None:
    """Why a file name cannot go into an archive that restores anywhere (None = fine)."""
    try:
        rel.encode("utf-8")
    except UnicodeEncodeError:
        return "jmeno neni platne UTF-8"
    if "\\" in rel or any(ord(c) < 32 or ord(c) == 127 for c in rel):
        return "jmeno obsahuje zpetne lomitko nebo ridici znak"
    return None


def _iter_files(root: Path, skipped: list[str], leave_out: frozenset = frozenset()):
    """Regular files under root as (posix relative path, path); symlinks skipped.

    `leave_out`: folder names directly under root that are not walked at all.
    A folder or file that cannot be read is reported in `skipped`, never
    silently left out (whatever the Python version does with stat errors).
    """
    def unreadable(error: OSError) -> None:
        skipped.append(f"{_shown(str(error.filename))}: {error.strerror}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=unreadable):
        dirnames[:] = sorted(d for d in dirnames
                             if d != "__pycache__" and not os.path.islink(os.path.join(dirpath, d))
                             and not (dirpath == str(root) and d in leave_out))
        for name in sorted(filenames):
            path = Path(dirpath) / name
            try:
                mode = os.lstat(path).st_mode
            except FileNotFoundError:
                continue  # deleted while we were walking
            except OSError as e:  # e.g. a folder that can be listed but not entered
                skipped.append(f"{_shown(str(path))}: {e.strerror}")
                continue
            if stat.S_ISREG(mode):  # not symlinks, sockets, devices
                yield path.relative_to(root).as_posix(), path


def _skip_data_file(rel: str) -> bool:
    # the database goes in from its consistent copy, the outbox holds one-time links
    return rel.split("/")[0] == "outbox" or rel.startswith(DB_NAME)


def _snapshot_db(src_path: Path, dst_path: Path) -> dict[str, int]:
    """Consistent copy of a live SQLite database (WAL included); row counts per table."""
    src = sqlite3.connect(str(src_path), timeout=60)
    try:
        dst = sqlite3.connect(str(dst_path))
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")  # one self-contained file
            return _db_health(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _db_health(conn: sqlite3.Connection, quick: bool = False) -> dict[str, int]:
    pragma = "quick_check" if quick else "integrity_check"
    result = conn.execute(f"PRAGMA {pragma}").fetchall()
    if result != [("ok",)]:
        raise BackupError(f"databaze neprosla {pragma}: {result[:3]}")
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {n: conn.execute('SELECT COUNT(*) FROM "%s"' % n.replace('"', '""')).fetchone()[0]
            for n in names}


def _db_file_health(path: Path, expected_tables: dict | None) -> dict[str, int]:
    try:
        conn = sqlite3.connect(str(path))
        try:
            tables = _db_health(conn, quick=True)
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise BackupError(f"databaze v archivu je poskozena: {e}") from None
    if expected_tables is not None and tables != expected_tables:
        raise BackupError("pocty radku v databazi nesedi s MANIFEST.json")
    return tables


def _check_member(member: tarfile.TarInfo) -> None:
    name = member.name
    parts = PurePosixPath(name).parts
    if (not member.isfile() or PurePosixPath(name).as_posix() != name or name.startswith("/")
            or "\\" in name or ".." in parts or not parts
            or parts[0] not in ("MANIFEST.json",) + FOLDERS
            or (parts[0] == "MANIFEST.json") != (len(parts) == 1)):
        raise BackupError(f"neocekavana polozka v archivu: {name!r}")


class _WriteError(Exception):
    """Writing a restored file failed (disk full, permissions): not the archive's fault."""

    def __init__(self, error: OSError):
        super().__init__(str(error))
        self.error = error


def _copy_member(src, target: Path | None, mtime: float,
                 written: list[Path] | None, made: list[Path] | None) -> str:
    """SHA-256 of a member, streamed in chunks; with a target also written there.

    The target goes into `written` before it is opened, so a file cut short by
    a full disk is rolled back too.
    """
    digest = hashlib.sha256()
    out = None
    if target is not None:
        try:
            _make_dirs(target.parent, made if made is not None else [])
            if written is not None:
                written.append(target)
            out = open(target, "wb")
        except OSError as e:
            raise _WriteError(e) from None
    try:
        for chunk in iter(lambda: src.read(1 << 20), b""):
            digest.update(chunk)
            if out is not None:
                try:
                    out.write(chunk)
                except OSError as e:
                    raise _WriteError(e) from None
    finally:
        if out is not None:
            try:
                out.close()
            except OSError as e:
                raise _WriteError(e) from None
    if target is not None:
        try:
            os.utime(target, (mtime, mtime))
        except OSError as e:
            raise _WriteError(e) from None
    return digest.hexdigest()


def _verify_archive(path: Path, into: Path | None = None, layout: str = "data-root",
                    written: list[Path] | None = None, made: list[Path] | None = None) -> dict:
    """Verify an archive: member names, every file against MANIFEST.json, the database.

    Streams the archive, so memory use does not grow with the data. With `into`
    the files are also written there (restore); paths go to `written`/`made`
    for the caller's rollback. Returns the manifest.
    """
    paths = LAYOUTS[layout]
    hashes: dict[str, str] = {}
    manifest = None
    with tempfile.TemporaryDirectory(prefix="classrally-check-") as tmp:
        db_file = Path(tmp) / DB_NAME
        try:
            with tarfile.open(path, "r:gz") as tar:
                for member in tar:
                    _check_member(member)
                    src = tar.extractfile(member)
                    if member.name == "MANIFEST.json":
                        manifest = json.loads(src.read(MAX_MANIFEST_BYTES))
                        continue
                    target = None
                    if into is not None:
                        top, rest = member.name.split("/", 1)
                        target = into / paths[top] / rest
                        if member.name == f"data/{DB_NAME}":
                            db_file = target
                    elif member.name == f"data/{DB_NAME}":
                        target = db_file
                    hashes[member.name] = _copy_member(src, target, member.mtime, written, made)
        except (BackupError, FileNotFoundError, _WriteError):
            raise
        except (tarfile.TarError, OSError, EOFError, ValueError, zlib.error) as e:
            raise BackupError(f"archiv nejde precist ({type(e).__name__}: {e})") from None
        if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
            raise BackupError("v archivu chybi MANIFEST.json")
        expected = {k: v.get("sha256") if isinstance(v, dict) else None for k, v in manifest["files"].items()}
        if hashes != expected:
            bad = sorted(set(hashes) ^ set(expected) | {k for k in hashes if hashes[k] != expected.get(k)})
            raise BackupError(f"obsah nesedi s MANIFEST.json ({len(bad)} rozdilu, napr. {_shown(bad[0])})")
        if f"data/{DB_NAME}" not in hashes:
            raise BackupError("v archivu chybi databaze")
        database = manifest.get("database")
        _db_file_health(db_file, database.get("tables") if isinstance(database, dict) else None)
    return manifest


def _verify_sidecar(archive: Path) -> None:
    sidecar = archive.with_name(archive.name + ".sha256")
    try:
        expected = sidecar.read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError, UnicodeDecodeError):
        raise BackupError("chybi kontrolni soucet .sha256") from None
    if _sha256_file(archive) != expected:
        raise BackupError("kontrolni soucet nesedi (archiv je poskozeny nebo zmeneny)")


def _prune(folder: Path, keep: int, group_len: int, newest_in_group: bool, protect: str = "") -> list[str]:
    """Keep one archive per day (group_len 8) or month (6) for the newest `keep` groups.

    `protect` (the archive just made) is never deleted, even if the clock went
    back and its name sorts before older archives.
    """
    groups: dict[str, list[str]] = {}
    for name in _archives(folder):
        groups.setdefault(ARCHIVE_RE.match(name).group(1)[:group_len], []).append(name)
    kept = {protect}
    for key in sorted(groups)[-keep:]:
        names = sorted(groups[key])
        kept.add(names[-1] if newest_in_group else names[0])
    removed = []
    for names in groups.values():
        for name in names:
            if name not in kept:
                for victim in (folder / name, folder / (name + ".sha256")):
                    if victim.exists():
                        victim.unlink()
                removed.append(name)
    # checksums left behind by an archive deleted by hand
    for name in os.listdir(folder):
        if name.endswith(".sha256") and ARCHIVE_RE.match(name[:-7]) and not (folder / name[:-7]).exists():
            (folder / name).unlink()
    return removed


class _Lock:
    def __init__(self, path: Path):
        self.path = path
        self.fd = None

    def __enter__(self) -> bool:
        if fcntl is None:
            return True
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            return False
        return True

    def __exit__(self, *exc) -> None:
        if self.fd is not None:
            os.close(self.fd)  # closing releases the lock


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def create(source: Path, dest: Path, layout: str = "data-root", with_audio: bool = False,
           keep_daily: int = KEEP_DAILY, keep_monthly: int = KEEP_MONTHLY,
           now: float | None = None) -> dict:
    """Make one backup in dest/daily, keep the month's first in dest/monthly, rotate.

    Returns a summary dict; raises BackupError. Another running backup -> {"busy": True}.
    """
    now = time.time() if now is None else now
    if keep_daily < 1 or keep_monthly < 1:
        raise BackupError("--keep-daily i --keep-monthly musi byt aspon 1")
    paths = LAYOUTS[layout]
    db_path = source / paths["data"] / DB_NAME
    if not db_path.is_file():
        raise BackupError(f"databaze {db_path} neexistuje (spravne --data-root / --app-dir?)")
    owner = db_path.stat().st_uid
    if hasattr(os, "geteuid") and os.geteuid() == 0 and owner != 0:
        # SQLite may create the -wal/-shm files of the live database; owned by
        # root, they would stop the server (running as its own user) from writing.
        raise BackupError(f"spustte zalohu pod uzivatelem, ktery vlastni databazi (uid {owner}), ne jako root")
    dest_real = dest.resolve()
    for key in FOLDERS:
        folder = (source / paths[key]).resolve()
        if dest_real == folder or folder in dest_real.parents:
            # every backup would swallow the previous ones
            raise BackupError(f"cil zaloh {dest} lezi ve zalohovane slozce {folder}")
    daily, monthly = dest / "daily", dest / "monthly"
    for folder in (dest, daily, monthly):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)

    with _Lock(dest / ".lock") as locked:
        if not locked:
            return {"busy": True}
        for old in dest.glob(".tmp-*"):  # left over by a run that crashed
            if old.is_dir() and now - old.stat().st_mtime > 86400:
                shutil.rmtree(old, ignore_errors=True)

        name = _archive_name(now)
        while (daily / name).exists():  # another run within the same second
            now += 1
            name = _archive_name(now)
        final = daily / name
        work = Path(tempfile.mkdtemp(prefix=".tmp-", dir=dest))
        try:
            db_copy = work / DB_NAME
            tables = _snapshot_db(db_path, db_copy)
            files: dict[str, dict] = {}
            skipped: list[str] = []
            staged = work / "archive.tar.gz"
            with tarfile.open(staged, "w:gz", compresslevel=6) as tar:
                def add(arcname: str, data: bytes, mtime: float, listed: bool = True) -> None:
                    info = tarfile.TarInfo(arcname)
                    info.size, info.mtime, info.mode = len(data), int(mtime), 0o600
                    tar.addfile(info, io.BytesIO(data))
                    if listed:
                        files[arcname] = {"size": len(data), "sha256": _sha256(data)}

                add(f"data/{DB_NAME}", db_copy.read_bytes(), now)
                for key in FOLDERS:
                    if key == "audio" and not with_audio:
                        continue
                    folder = source / paths[key]
                    try:
                        is_folder = stat.S_ISDIR(os.stat(folder).st_mode)
                    except FileNotFoundError:
                        continue  # not there (yet): nothing to back up
                    except OSError as e:  # there, but cannot be entered
                        skipped.append(f"{key}: {e.strerror}")
                        continue
                    if not is_folder:
                        continue
                    leave_out = frozenset({"outbox"}) if key == "data" else frozenset()
                    for rel, path in _iter_files(folder, skipped, leave_out):
                        if key == "data" and _skip_data_file(rel):
                            continue
                        why = _unusable_name(rel)
                        if why:
                            # one odd name must not stop everyone's backup: leave the
                            # file out, report it (check then alarms until renamed)
                            skipped.append(f"{key}/{_shown(rel)}: {why}")
                            continue
                        try:
                            add(f"{key}/{rel}", path.read_bytes(), path.stat().st_mtime)
                        except FileNotFoundError:
                            continue  # deleted while we were walking
                        except OSError as e:
                            skipped.append(f"{key}/{_shown(rel)}: {e.strerror}")
                manifest = {
                    "format": 1,
                    "app": "classrally",
                    "created": _iso(now),
                    "with_audio": with_audio,
                    "database": {"file": f"data/{DB_NAME}", "integrity": "ok", "tables": tables},
                    "files": files,
                }
                add("MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=1).encode(), now,
                    listed=False)
            _verify_archive(staged)  # what was written must read back and verify

            digest = _sha256_file(staged)
            os.chmod(staged, 0o600)
            os.utime(staged, (now, now))
            _write_sidecar(final, digest)  # checksum first: a check never sees the archive without it
            os.replace(staged, final)
        finally:
            shutil.rmtree(work, ignore_errors=True)

        month = ARCHIVE_RE.match(name).group(1)[:6]
        made_monthly = False
        if not any(ARCHIVE_RE.match(n).group(1)[:6] == month for n in _archives(monthly)):
            _write_sidecar(monthly / name, digest)
            try:
                os.link(final, monthly / name)
            except OSError:  # no hard links here: copy under a temporary name, then rename
                partial = monthly / f".{name}.tmp"
                try:
                    shutil.copy2(final, partial)
                    os.replace(partial, monthly / name)
                except BaseException:
                    partial.unlink(missing_ok=True)  # full disk: leave no half copy behind
                    raise
            made_monthly = True
        removed = _prune(daily, keep_daily, 8, newest_in_group=True, protect=name)
        removed += [f"monthly/{n}" for n in _prune(monthly, keep_monthly, 6, newest_in_group=False,
                                                    protect=name if made_monthly else "")]

    summary = {
        "time": _iso(now), "ok": True, "archive": f"daily/{name}", "size": final.stat().st_size,
        "files": len(files), "tables": tables, "monthly_created": made_monthly, "removed": removed,
        "daily": len(_archives(daily)), "monthly": len(_archives(monthly)),
        "skipped_count": len(skipped), "skipped": skipped[:MAX_SKIPPED_LISTED],
    }
    _write_atomic(dest / "last-run.json", json.dumps(summary, indent=1).encode())
    return summary


def check(dest: Path, max_age_hours: float = MAX_AGE_HOURS, verify_all: bool = False,
          now: float | None = None) -> tuple[bool, str]:
    """(ok, one-line summary) about the backups in dest."""
    now = time.time() if now is None else now
    problems: list[str] = []
    daily, monthly = _archives(dest / "daily"), _archives(dest / "monthly")
    newest_info = ""
    if not daily:
        problems.append(f"v {dest / 'daily'} neni zadna zaloha")
    else:
        newest = dest / "daily" / daily[-1]
        targets = [newest]
        if verify_all:
            targets += [dest / "daily" / n for n in daily[:-1]] + [dest / "monthly" / n for n in monthly]
        for path in targets:
            try:
                st = path.stat()
                _verify_sidecar(path)
                manifest = _verify_archive(path)
            except FileNotFoundError:
                if path != newest:
                    continue  # rotated away by a backup running at the same time
                problems.append(f"posledni zaloha {path.name} zmizela")
                continue
            except (BackupError, OSError) as e:
                problems.append(f"{path.parent.name}/{path.name}: {e}")
                continue
            if path != newest:
                continue
            age_h = (now - st.st_mtime) / 3600
            if age_h > max_age_hours:
                problems.append(f"posledni zaloha je stara {age_h:.1f} h (limit {max_age_hours:g} h)")
            elif age_h < -FUTURE_TOLERANCE_HOURS:
                problems.append(f"posledni zaloha ma cas {-age_h:.1f} h v budoucnosti (hodiny serveru?)")
            database = manifest.get("database")
            tables = database.get("tables") if isinstance(database, dict) else None
            users = tables.get("users") if isinstance(tables, dict) else None
            newest_info = (f"posledni zaloha pred {max(age_h, 0):.1f} h ({_kb(st.st_size)}"
                           + (f", uctu {users}" if users is not None else "") + ", databaze v poradku)")
    if not monthly:
        problems.append("neni zadna mesicni zaloha")
    else:
        try:
            age_d = (now - (dest / "monthly" / monthly[-1]).stat().st_mtime) / 86400
        except OSError:
            age_d = None
        if age_d is not None and age_d > MAX_MONTHLY_AGE_DAYS:
            problems.append(f"posledni mesicni zaloha je stara {age_d:.0f} dni")
    last_run = dest / "last-run.json"
    last = _read_json(last_run)
    if last is None and last_run.exists():
        problems.append("stav posledniho behu (last-run.json) nejde precist")
    elif last is not None and last.get("ok") is False:
        problems.append(f"posledni beh {last.get('time', '?')} selhal: {last.get('error', '?')}")
    elif last is not None and last.get("skipped_count"):
        examples = last.get("skipped") or ["?"]
        problems.append(f"posledni zaloha vynechala {last['skipped_count']} polozek, napr. {examples[0]} "
                        "(prejmenujte soubor nebo opravte prava)")
    if problems:
        return False, "CHYBA: " + "; ".join(problems)
    checked = f"overeno vsech {len(daily) + len(monthly)}" if verify_all else "overena posledni"
    return True, f"OK: {newest_info}; dennich {len(daily)}, mesicnich {len(monthly)}, {checked}"


def _push_host(url: str) -> str:
    """Host[:port] of a push URL for messages (never the path or user info: tokens)."""
    try:
        parts = urllib.parse.urlsplit(url)
        return (parts.hostname or "?") + (f":{parts.port}" if parts.port else "")
    except ValueError:
        return "?"


def push(url: str, ok: bool, message: str, timeout: float = 15) -> None:
    """Report to a push monitor (Uptime Kuma: ?status=up|down&msg=...).

    Error messages never contain the URL: it holds the monitor's token.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        valid = parts.scheme in ("http", "https") and bool(parts.hostname)
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        valid = False
    if not valid:
        raise BackupError("push URL musi byt platna adresa http(s)://...")
    # Kuma's "copy" button gives .../api/push/<token>?status=up&msg=OK&ping= :
    # ours must replace those, a doubled "status" counts as down in Kuma.
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if k not in ("status", "msg", "ping")]
    query += [("status", "up" if ok else "down"), ("msg", message[:250]), ("ping", "")]
    full = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
    try:
        with urllib.request.urlopen(full, timeout=timeout) as resp:
            body = resp.read(4096)
    except urllib.error.HTTPError as e:
        raise BackupError(f"push se nepovedl: HTTP {e.code}") from None
    except urllib.error.URLError as e:
        reason = getattr(e.reason, "strerror", None) or type(e.reason).__name__
        raise BackupError(f"push se nepovedl: {reason}") from None
    except Exception as e:  # InvalidURL, ValueError ... their text would contain the URL
        raise BackupError(f"push se nepovedl ({type(e).__name__})") from None
    try:
        answer = json.loads(body)
    except ValueError:
        return
    if isinstance(answer, dict) and answer.get("ok") is False:
        raise BackupError("push odmitnut monitorem (je monitor aktivni?)")


def _make_dirs(path: Path, made: list[Path]) -> None:
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for folder in reversed(missing):
        folder.mkdir(mode=0o700)
        made.append(folder)


def _rollback(written: list[Path], made: list[Path]) -> None:
    for path in reversed(written):
        with contextlib.suppress(OSError):
            path.unlink()
    for folder in reversed(made):
        with contextlib.suppress(OSError):
            folder.rmdir()


def restore(archive: Path, into: Path, layout: str = "data-root") -> dict:
    """Unpack a verified archive into a new or empty folder (never over existing data).

    All or nothing: when the archive turns out bad or writing fails (full disk),
    everything written so far is removed again.
    """
    if into.exists() and (not into.is_dir() or any(into.iterdir())):
        raise BackupError(f"{into} neni prazdna slozka — obnova nikdy neprepisuje existujici data")
    if archive.with_name(archive.name + ".sha256").exists():
        _verify_sidecar(archive)
    paths = LAYOUTS[layout]
    made: list[Path] = []
    written: list[Path] = []
    try:
        _make_dirs(into, made)
        manifest = _verify_archive(archive, into, layout, written, made)
        target = into / "MANIFEST.json"
        written.append(target)
        target.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        for key in ("data", "history", "questions", "uploads"):
            _make_dirs(into / paths[key], made)
    except _WriteError as e:
        _rollback(written, made)
        raise e.error from None
    except BaseException:
        _rollback(written, made)
        raise
    database = manifest.get("database")
    tables = database.get("tables", {}) if isinstance(database, dict) else {}
    return {"files": len(manifest.get("files", {})), "tables": tables, "created": manifest.get("created")}


def list_backups(dest: Path, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    lines = []
    for sub in ("daily", "monthly"):
        for name in _archives(dest / sub):
            path = dest / sub / name
            try:
                st = path.stat()
            except OSError:
                continue
            age_h = (now - st.st_mtime) / 3600
            age = f"{age_h:.1f} h" if age_h < 48 else f"{age_h / 24:.0f} dni"
            lines.append(f"{sub + '/' + name:49} {_kb(st.st_size):>9}   pred {age}")
    last = _read_json(dest / "last-run.json")
    if last:
        lines.append(f"posledni beh: {last.get('time')} "
                     + ("v poradku" if last.get("ok") else f"SELHAL: {last.get('error')}"))
    return lines


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------

def _push_url(args) -> str:
    if args.push_url_file:
        try:
            lines = [ln.strip() for ln in Path(args.push_url_file).read_text(encoding="utf-8").splitlines()]
        except (OSError, UnicodeDecodeError) as e:
            raise BackupError(f"soubor s push URL nejde precist ({type(e).__name__})") from None
        return next((ln for ln in lines if ln and not ln.startswith("#")), "")
    return os.environ.get(PUSH_URL_ENV, "").strip()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ClassRally backups (7 daily + 12 monthly), checks and restore.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="make a backup and rotate the old ones")
    src = p_create.add_mutually_exclusive_group(required=True)
    src.add_argument("--data-root", type=Path,
                     help="server layout: DIR/data, DIR/history, DIR/questions, DIR/uploads (Docker)")
    src.add_argument("--app-dir", type=Path,
                     help="source folder when server.py runs from it (uploads in static/uploads)")
    p_create.add_argument("--dest", type=Path, required=True,
                          help="backup folder, ideally on another disk than the data")
    p_create.add_argument("--with-audio", action="store_true", help="include the music in audio/ too")
    p_create.add_argument("--keep-daily", type=_positive_int, default=KEEP_DAILY)
    p_create.add_argument("--keep-monthly", type=_positive_int, default=KEEP_MONTHLY)

    p_check = sub.add_parser("check", help="is the newest backup fresh and readable? (exit 1 if not)")
    p_check.add_argument("--dest", type=Path, required=True)
    p_check.add_argument("--max-age-hours", type=float, default=MAX_AGE_HOURS)
    p_check.add_argument("--all", action="store_true", help="verify every kept backup, not just the newest")
    p_check.add_argument("--push-url-file", type=Path,
                         help=f"file with a push monitor URL (Uptime Kuma); or env {PUSH_URL_ENV}")

    p_list = sub.add_parser("list", help="list kept backups")
    p_list.add_argument("--dest", type=Path, required=True)

    p_restore = sub.add_parser("restore", help="unpack a backup into a new, empty folder")
    p_restore.add_argument("archive", type=Path)
    p_restore.add_argument("--into", type=Path, required=True)
    p_restore.add_argument("--layout", choices=sorted(LAYOUTS), default="data-root",
                           help="data-root (Docker, default) or app-dir (uploads go to static/uploads)")
    return parser


def _run(args) -> int:
    if args.cmd == "create":
        source, layout = (args.data_root, "data-root") if args.data_root else (args.app_dir, "app-dir")
        try:
            result = create(source, args.dest, layout=layout, with_audio=args.with_audio,
                            keep_daily=args.keep_daily, keep_monthly=args.keep_monthly)
        except Exception as e:  # any failure must end up in last-run.json for `check`
            error = str(e) or type(e).__name__
            print(f"CHYBA: zaloha se nepovedla: {error}", file=sys.stderr)
            try:
                _write_atomic(args.dest / "last-run.json",
                              json.dumps({"time": _iso(time.time()), "ok": False, "error": error}).encode())
            except OSError:
                pass
            return 1
        if result.get("busy"):
            print("Jina zaloha prave bezi, tento beh nic nedela.")
            return 0
        print(f"OK: {result['archive']} ({_kb(result['size'])}, souboru {result['files']}, "
              f"uctu {result['tables'].get('users', '?')}); dennich {result['daily']}, "
              f"mesicnich {result['monthly']}"
              + (", zalozena mesicni" if result["monthly_created"] else "")
              + (f", smazano {len(result['removed'])}" if result["removed"] else ""))
        if result["skipped_count"]:
            print(f"POZOR: vynechano {result['skipped_count']} polozek (kontrola hlasi chybu, dokud se to "
                  "neopravi): " + "; ".join(result["skipped"][:5]), file=sys.stderr)
        return 0

    if args.cmd == "check":
        try:
            ok, message = check(args.dest, max_age_hours=args.max_age_hours, verify_all=args.all)
        except Exception as e:  # still report "down" below instead of dying silently
            ok, message = False, f"CHYBA: kontrola selhala ({type(e).__name__}: {e})"
        print(message)
        try:
            url = _push_url(args)
            if url:
                push(url, ok, message)
                print(f"Hlaseni odeslano na {_push_host(url)}.")
        except BackupError as e:
            print(f"POZOR: {e}", file=sys.stderr)
            return 1 if not ok else 2
        return 0 if ok else 1

    if args.cmd == "list":
        lines = list_backups(args.dest)
        print("\n".join(lines) if lines else f"V {args.dest} zadne zalohy nejsou.")
        return 0

    try:
        result = restore(args.archive, args.into, layout=args.layout)
    except (BackupError, OSError, sqlite3.Error) as e:
        print(f"CHYBA: obnova se nepovedla: {e}", file=sys.stderr)
        return 1
    uploads = LAYOUTS[args.layout]["uploads"]
    print(f"Obnoveno do {args.into}: souboru {result['files']}, zaloha z {result['created']}, "
          f"uctu {result['tables'].get('users', '?')}, databaze v poradku.")
    print(f"Do provozu: zastavte server, soucasne slozky data/ history/ questions/ {uploads}/ "
          "odsunte stranou (neprepisujte je) a nahradte je temi obnovenymi; pak server spustte.")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("Pozor: obnoveno jako root — pred spustenim nastavte vlastnika na uzivatele, "
              "pod kterym server bezi (chown -R).")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    old_umask = os.umask(0o077)  # backups hold personal data: owner only
    try:
        return _run(args)
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    sys.exit(main())
