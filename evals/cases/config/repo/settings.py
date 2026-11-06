"""Parsing small configuration values."""


def parse_pairs(text):
    """Parse 'key=value' lines; blank lines and '#' comments are skipped."""
    result = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"line {number}: expected key=value")
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def load_port(text, default=8080):
    try:
        port = int(text)
    except ValueError:
        return default
    if not 1 <= port <= 65535:
        raise ValueError("port out of range")
    return port
