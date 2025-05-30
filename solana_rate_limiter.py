# solana_rate_limiter.py
import threading, time

_lock = threading.Lock()
_last_call = 0.0

def throttle():
    """
    Blocks until at least 1 second has elapsed since the
    last time this was called. Safe to call from any thread.
    """
    global _last_call
    with _lock:
        now = time.time()
        since = now - _last_call
        if since < 1.0:
            time.sleep(1.0 - since)
        _last_call = time.time()
