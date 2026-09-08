import ctypes
import hashlib
import os
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from threading import Lock


PATH_LOCKS: dict[str, Lock] = {}
PATH_LOCKS_GUARD = Lock()


def process_path_lock(path: Path) -> Lock:
    key = str(path.resolve()).lower()
    with PATH_LOCKS_GUARD:
        lock = PATH_LOCKS.get(key)
        if lock is None:
            lock = Lock()
            PATH_LOCKS[key] = lock
        return lock


def _path_mutex_name(path: Path) -> str:
    digest = hashlib.sha256(str(path.resolve()).casefold().encode("utf-8")).hexdigest()
    return f"Local\\CodexHeCrawler-{digest}"


@contextmanager
def _windows_named_mutex(path: Path, timeout: float | None):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel32.ReleaseMutex.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool

    handle = kernel32.CreateMutexW(None, False, _path_mutex_name(path))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        wait_ms = 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
        result = kernel32.WaitForSingleObject(handle, wait_ms)
        if result == 0x102:
            raise TimeoutError(f"等待文件锁超时: {path}")
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        if result not in (0, 0x80):
            raise RuntimeError(f"等待文件锁失败: {path}")
        acquired = True
        yield
    finally:
        if acquired:
            kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)


@contextmanager
def _portable_process_lock(path: Path, timeout: float | None):
    import fcntl

    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
    lock_path = Path(tempfile.gettempdir()) / f"codex-he-{digest}.sync"
    with lock_path.open("a+b") as lock_file:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(f"等待文件锁超时: {path}") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_path_lock(path: Path, timeout: float | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    thread_lock = process_path_lock(path)
    if deadline is None:
        thread_lock.acquire()
    elif not thread_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise TimeoutError(f"等待文件锁超时: {path}")
    try:
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        process_lock = _windows_named_mutex(path, remaining) if os.name == "nt" else _portable_process_lock(path, remaining)
        with process_lock:
            yield
    finally:
        thread_lock.release()


def write_text_atomic_unlocked(path: Path, text: str, encoding: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="") as temp_file:
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def write_text_atomic(path: Path, text: str, encoding: str = "utf-8-sig") -> None:
    with exclusive_path_lock(path):
        write_text_atomic_unlocked(path, text, encoding)


def commit_text_transaction_unlocked(
    changes: dict[Path, tuple[str, str] | None],
) -> None:
    originals: dict[Path, bytes | None] = {}
    staged: dict[Path, Path] = {}
    committed: list[Path] = []
    try:
        for path, change in changes.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            originals[path] = path.read_bytes() if path.exists() else None
            if change is None:
                continue
            text, encoding = change
            descriptor, temp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
            temp_path = Path(temp_name)
            with os.fdopen(descriptor, "w", encoding=encoding, newline="") as temp_file:
                temp_file.write(text)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            staged[path] = temp_path

        for path, change in changes.items():
            if change is None:
                if path.exists():
                    path.unlink()
            else:
                staged[path].replace(path)
            committed.append(path)
    except Exception:
        for path in reversed(committed):
            original = originals[path]
            if original is None:
                if path.exists():
                    path.unlink()
                continue
            descriptor, temp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".rollback.tmp", dir=path.parent)
            restore_path = Path(temp_name)
            try:
                with os.fdopen(descriptor, "wb") as restore_file:
                    restore_file.write(original)
                    restore_file.flush()
                    os.fsync(restore_file.fileno())
                restore_path.replace(path)
            finally:
                if restore_path.exists():
                    restore_path.unlink()
        raise
    finally:
        for temp_path in staged.values():
            if temp_path.exists():
                temp_path.unlink()


def commit_text_transaction(
    changes: dict[Path, tuple[str, str] | None],
    timeout: float | None = None,
) -> None:
    paths = sorted(changes, key=lambda path: str(path.resolve()).casefold())
    with ExitStack() as locks:
        for path in paths:
            locks.enter_context(exclusive_path_lock(path, timeout=timeout))
        commit_text_transaction_unlocked(changes)
