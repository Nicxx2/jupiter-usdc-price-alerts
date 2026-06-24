# solana_rate_limiter.py
import os
import threading
import time

_lock = threading.Lock()
_last_call = 0.0
_min_interval_seconds = 1.0
_enabled = True
_lock_path = os.getenv("SOLANATRACKER_RATE_LIMIT_FILE", "/shared/solanatracker-rate-limit.lock")


def configure_rate_limit(requests_per_second=None, enabled=True):
    """
    Configure the SolanaTracker limiter.
    enabled=False bypasses throttling for paid/private setups.
    """
    global _min_interval_seconds, _enabled
    _enabled = bool(enabled)
    try:
        rps = float(requests_per_second)
        if rps <= 0:
            raise ValueError
        _min_interval_seconds = 1.0 / min(rps, 50.0)
    except (TypeError, ValueError):
        _min_interval_seconds = 1.0


def is_rate_limit_enabled():
    return _enabled


_initial_mode = str(os.getenv("SOLANATRACKER_RATE_LIMIT_MODE", "safe")).strip().lower()
configure_rate_limit(
    os.getenv("SOLANATRACKER_REQUESTS_PER_SECOND", "1"),
    enabled=_initial_mode not in {"off", "disabled", "none"},
)


def _local_throttle():
    global _last_call
    now = time.time()
    since = now - _last_call
    if since < _min_interval_seconds:
        time.sleep(_min_interval_seconds - since)
    _last_call = time.time()


def _shared_throttle():
    """
    Coordinate rate limiting across monitor/backend processes in Docker.
    Falls back to the process-local limiter on platforms without fcntl.
    """
    if os.name != "posix" or not _lock_path:
        return False

    try:
        import fcntl

        directory = os.path.dirname(_lock_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        with open(_lock_path, "a+", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.seek(0)
            try:
                last_call = float(f.read().strip() or "0")
            except ValueError:
                last_call = 0.0

            now = time.time()
            since = now - last_call
            if since < _min_interval_seconds:
                time.sleep(_min_interval_seconds - since)

            f.seek(0)
            f.truncate()
            f.write(str(time.time()))
            f.flush()
            os.fsync(f.fileno())
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return True
    except Exception:
        return False


def throttle():
    """
    Blocks until the configured minimum interval has elapsed since the
    last SolanaTracker API call. Safe to call from any thread; on Docker/Linux
    it also coordinates across the monitor and backend processes.
    """
    if not _enabled:
        return
    with _lock:
        if not _shared_throttle():
            _local_throttle()