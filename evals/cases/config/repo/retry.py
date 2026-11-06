"""Retrying flaky calls."""

import time


def retry(fn, attempts=3, delay=0.0):
    """Call fn until it succeeds; re-raise the last error after `attempts` tries."""
    last_error = None
    for _ in range(attempts):
        try:
            return fn()
        except Exception as err:
            last_error = err
            time.sleep(delay)
    raise last_error
