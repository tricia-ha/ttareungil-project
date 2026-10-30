"""Local cache identity; content validation still occurs on initial model load."""

from pathlib import Path


def file_signature(paths):
    rows = []
    for value in paths:
        path = Path(value).resolve()
        try:
            stat = path.stat()
            rows.append((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        except FileNotFoundError:
            rows.append((str(path), None, None, None))
    return tuple(rows)
