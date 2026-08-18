import io
def sub(path, old, new):
    raw = io.open(path, encoding="utf-8", newline="").read()
    crlf = "\r\n" in raw
    s = raw.replace("\r\n", "\n")
    n = s.count(old)
    if n != 1:
        raise SystemExit(f"expected 1 match, got {n} in {path}:\n{old[:250]}")
    s = s.replace(old, new, 1)
    io.open(path, "w", encoding="utf-8", newline="").write(s.replace("\n", "\r\n") if crlf else s)
    print(f"  patched {path}")
