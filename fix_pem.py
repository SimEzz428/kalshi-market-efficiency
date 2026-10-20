
from __future__ import annotations

import argparse
import base64
import pathlib
import re
import sys

TYPES = ["RSA PRIVATE KEY", "PRIVATE KEY", "ENCRYPTED PRIVATE KEY"]
B64 = re.compile(rb"^[A-Za-z0-9+/=]+$")


def diagnose(raw: bytes) -> None:
    print(f"  size                {len(raw):,} bytes")
    if not raw:
        print("  FILE IS EMPTY — nothing was pasted, or the editor did not save")
        return

    has_crlf = b"\r\n" in raw
    has_tab = b"\t" in raw
    print(f"  CRLF line endings   {has_crlf}")
    print(f"  trailing newline    {raw.endswith(bytes([10]))}")
    print(f"  tabs present        {has_tab}")
    nonascii = [b for b in raw if b > 127]
    print(f"  non-ASCII bytes     {len(nonascii)}"
          + ("   <- smart quotes / nbsp from a rich-text paste" if nonascii else ""))

    text = raw.decode("utf-8", "replace")
    lines = text.splitlines()
    print(f"  lines               {len(lines)}")

    has_begin = any("-----BEGIN" in ln for ln in lines)
    has_end = any("-----END" in ln for ln in lines)
    print(f"  BEGIN line          {has_begin}")
    print(f"  END line            {has_end}")
    if has_begin:
        b = next(ln for ln in lines if "-----BEGIN" in ln)
        print(f"    -> {b.strip()}")
        if b != b.strip():
            print("       !! has surrounding whitespace — this alone breaks it")
    if not has_begin or not has_end:
        print("\n  DIAGNOSIS: framing lines missing. This is the usual cause.")

    body = [ln for ln in lines if "-----" not in ln and ln.strip()]
    if body:
        lens = sorted({len(ln) for ln in body})
        print(f"  body lines          {len(body)}")
        print(f"  body line lengths   {lens[:8]}{' ...' if len(lens) > 8 else ''}")
        indented = [ln for ln in body if ln != ln.lstrip()]
        if indented:
            print(f"  !! {len(indented)} indented line(s) — editor added leading space")
        bad = [ln for ln in body if not B64.match(ln.strip().encode())]
        if bad:
            print(f"  !! {len(bad)} line(s) contain non-base64 characters")


def extract_b64(raw: bytes) -> bytes:
    text = raw.decode("utf-8", "replace")
    text = re.sub(r"-----[A-Z ]+-----", "", text)
    return "".join(re.findall(r"[A-Za-z0-9+/=]+", text)).encode()


def rebuild(b64: bytes, keytype: str) -> bytes:
    wrapped = b"\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
    return (f"-----BEGIN {keytype}-----\n".encode() + wrapped
            + f"\n-----END {keytype}-----\n".encode())


def try_load(pem: bytes):
    from cryptography.hazmat.primitives import serialization
    return serialization.load_pem_private_key(pem, password=None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--repair", action="store_true")
    a = ap.parse_args()

    p = pathlib.Path(a.path)
    if not p.exists():
        print(f"no such file: {p}")
        return 1
    raw = p.read_bytes()

    print("=" * 66)
    print(f"DIAGNOSIS  {p}")
    print("=" * 66)
    diagnose(raw)

    print("\n" + "=" * 66)
    print("LOAD TEST")
    print("=" * 66)
    try:
        k = try_load(raw)
        print(f"  ALREADY VALID — RSA {k.key_size} bit. Nothing to repair.")
        return 0
    except Exception as e:
        print(f"  fails as-is: {type(e).__name__}: {str(e)[:90]}")

    b64 = extract_b64(raw)
    print(f"\n  recovered {len(b64):,} base64 characters")
    if len(b64) < 400:
        print("  TOO SHORT for an RSA private key (expect 1,600+ chars).")
        print("  The file does not contain a complete key — re-copy it from")
        print("  Kalshi. If you already closed that dialog, generate a new key.")
        return 1

    print("\n  trying each PEM type:")
    for t in TYPES:
        pem = rebuild(b64, t)
        try:
            k = try_load(pem)
        except Exception as e:
            print(f"    {t:<26} no  ({type(e).__name__})")
            continue
        print(f"    {t:<26} YES — RSA {k.key_size} bit")
        if a.repair:
            backup = p.with_suffix(p.suffix + ".broken")
            backup.write_bytes(raw)
            p.write_bytes(pem)
            p.chmod(0o600)
            print(f"\n  REPAIRED. Original saved as {backup.name}")
            print(f"  {p} rewritten, mode 600")
        else:
            print("\n  Repair is possible. Re-run with --repair to apply.")
        return 0

    print("\n  Could not rebuild a valid key from this file.")
    print("  The base64 itself is damaged — characters were dropped or")
    print("  altered in transit. Generate a fresh key on Kalshi; a partial")
    print("  key cannot be recovered, only replaced.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
