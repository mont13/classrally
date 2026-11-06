"""Windows launcher; stdlib only. Data migration is also tested on Linux."""
from __future__ import annotations

import json
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import socket
import sys
import threading
import time
from urllib.request import ProxyHandler, build_opener
import uuid
import webbrowser

PORT = 48217  # matches the narrowly scoped firewall rule


class InstanceBusy(RuntimeError):
    pass


@contextmanager
def instance_lock(data_dir: Path):
    """OS lock held before migration until server exit; crash releases it too."""
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / ".instance.lock").open("a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # Linux unit tests exercise the same lifetime/ownership contract
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise InstanceBusy("ClassRally už používá tuto datovou složku. Zavřete předchozí okno.") from None
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def copy_missing(source: Path, target: Path) -> None:
    """Copy regular files only; preserve every existing destination and source."""
    if not source.is_dir():
        return
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.is_symlink():
            continue
        dest = target / item.name
        if item.is_dir():
            copy_missing(item, dest)
        elif item.is_file():
            if dest.exists():
                continue
            temporary = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.copy")
            try:
                with temporary.open("xb") as output, item.open("rb") as source_file:
                    shutil.copyfileobj(source_file, output)
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    if os.name == "nt":
                        os.rename(temporary, dest)  # Windows refuses an existing destination
                    else:
                        os.link(temporary, dest)  # Linux tests: atomic, refuses overwrite too
                except FileExistsError:
                    pass
            finally:
                # Only our own unfinished staging file; never a source/user file.
                temporary.unlink(missing_ok=True)


def prepare_data(app_dir: Path, data_dir: Path) -> None:
    """One-time copy from the old portable layout; nothing is moved or deleted.

    Close the old application before migrating. The marker is written last so
    a failed copy can be resumed. Later launches never resurrect legacy data.
    Newly shipped public question sets are added without overwriting edits.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    marker = data_dir / ".windows-migrated-v1"
    if not marker.exists():
        copy_missing(app_dir / "data", data_dir)
        copy_missing(app_dir / "history", data_dir / "history")
        copy_missing(app_dir / "static" / "uploads", data_dir / "uploads")
        marker.write_text("Legacy files copied; originals preserved.\n", encoding="utf-8")
    copy_missing(app_dir / "questions", data_dir / "questions")
    (data_dir / "history").mkdir(exist_ok=True)
    (data_dir / "uploads").mkdir(exist_ok=True)


def open_when_ready(instance: str, stopped: threading.Event) -> None:
    # Bypass system HTTP proxies for localhost (common in managed schools).
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + 45
    while not stopped.is_set() and time.monotonic() < deadline:
        try:
            with opener.open(f"http://localhost:{PORT}/api/health", timeout=1) as response:
                health = json.load(response)
            if health.get("ok") and health.get("service") == "classrally" and health.get("instance") == instance:
                print(f"\nUčitel: http://localhost:{PORT}/host", flush=True)
                with opener.open(f"http://localhost:{PORT}/api/network", timeout=2) as response:
                    info = json.load(response)
                print("Adresy pro žáky (vyberte na /host síť, ke které se připojili):", flush=True)
                for url in info.get("play_urls", []):
                    print(f"  {url}", flush=True)
                if info.get("loopback_only"):
                    print("Zatím není dostupná Wi-Fi adresa. Zapněte hotspot a na /host obnovte adresy.", flush=True)
                if not webbrowser.open(f"http://localhost:{PORT}/host"):
                    print("Prohlížeč se neotevřel; otevřete adresu pro učitele ručně.", flush=True)
                return
        except (OSError, ValueError):
            pass
        stopped.wait(0.25)
    if not stopped.is_set():
        print("Server se včas neohlásil. Zkontrolujte chybu v tomto okně; pak otevřete localhost ručně.", flush=True)


def main() -> int:
    app_dir = Path(__file__).resolve().parent
    if not (app_dir / "server.py").is_file():  # also runnable from the source tree
        app_dir = app_dir.parent
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        print("Chybí LOCALAPPDATA. Spouštěč je určen pro Windows.")
        return 1
    data_dir = Path(local) / "ClassRally"
    try:
        with instance_lock(data_dir):
            return run_server(app_dir, data_dir)
    except InstanceBusy as exc:
        print(str(exc))
        return 1


def run_server(app_dir: Path, data_dir: Path) -> int:
    # Avoid migrating/opening a browser against an unrelated existing service.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("0.0.0.0", PORT))
    except OSError:
        print(f"Port {PORT} je obsazený. Zavřete předchozí ClassRally nebo požádejte IT.")
        return 1
    prepare_data(app_dir, data_dir)
    instance = uuid.uuid4().hex
    os.environ.update({
        "QUIZ_DATA_DIR": str(data_dir), "QUIZ_HISTORY_DIR": str(data_dir / "history"),
        "QUIZ_QUESTIONS_DIR": str(data_dir / "questions"), "QUIZ_UPLOADS_DIR": str(data_dir / "uploads"),
        "QUIZ_LAPTOP": "1", "QUIZ_ROOMS": "single", "QUIZ_BASE_URL": "",
        "QUIZ_PORT": str(PORT), "QUIZ_INSTANCE_ID": instance, "MAIL_MODE": "off",
    })
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(app_dir))
    os.chdir(app_dir)
    print(f"Data jsou v: {data_dir}")
    print("Toto okno nechte otevřené. Server ukončíte Ctrl+C nebo zavřením okna.")
    import server  # environment must be set before db/server are imported
    stopped = threading.Event()
    readiness = threading.Thread(target=open_when_ready, args=(instance, stopped), daemon=True)
    readiness.start()
    sys.argv = [str(app_dir / "server.py"), "--host", "0.0.0.0", "--port", str(PORT)]
    try:
        server.main()
    finally:
        stopped.set()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError:
        # Avoid dumping file contents/credentials in a traceback.
        print("ClassRally nelze spustit nebo zapsat data. Zkontrolujte práva a volné místo; požádejte IT.")
        raise SystemExit(1)
