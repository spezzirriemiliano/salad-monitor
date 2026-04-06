"""
update_credentials.py — Extracts Salad cookies from a cURL command copied from DevTools.
No external dependencies.
"""

import re
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "config.json"


def print_instructions():
    print()
    print("=" * 60)
    print("  Salad Cookie Extractor")
    print("=" * 60)
    print()
    print("\033[91m  WARNING: Do not share these values with anyone.")
    print("           The credentials will be saved in the config file — do not share that file either.\033[0m")
    print()
    print("  1. Make sure you are logged in to Salad in Chrome first.")
    print("  2. Navigate to  https://app-api.salad.com/api/v2/machines")
    print("  3. Press F12 to open DevTools, go to the Network tab,")
    print("     and reload the page (F5).")
    print("  4. Right-click the request → Copy → Copy as cURL (bash)")
    print("  5. Paste the result below and press Enter:")
    print()
    print("-" * 60)


def read_multiline_input() -> str:
    lines = []
    while True:
        try:
            line = input()
            lines.append(line)
            # The last line of a cURL command does not end with "\" — stop automatically
            if lines[0].strip().startswith("curl") and not line.rstrip().endswith("\\"):
                break
        except EOFError:
            break
    return "\n".join(lines)


def parse_curl(curl_text: str) -> dict:
    # Match -H 'cookie: ...' (Network tab) or -b '...' (address bar)
    match = re.search(r"-H\s+['\"]cookie:\s*([^'\"]+)['\"]", curl_text, re.IGNORECASE)
    if not match:
        match = re.search(r"-b\s+'([^']+)'", curl_text)
    if not match:
        match = re.search(r'-b\s+"([^"]+)"', curl_text)
    if not match:
        return {}

    cookie_str = match.group(1).strip()
    cookies = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            cookies[name.strip()] = value.strip()

    return {k: v for k, v in cookies.items() if k in ("auth", "cf_clearance")}


def update_config(auth: str, cf: str):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("salad_api", {})["auth_cookie"] = auth
    cfg["salad_api"]["cf_clearance"] = cf
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def main():
    print_instructions()

    curl_text = read_multiline_input()

    print()
    if not curl_text.strip():
        print("[ERROR] Nothing was pasted.")
        input("\nPress Enter to exit...")
        raise SystemExit(1)

    found = parse_curl(curl_text)
    auth  = found.get("auth")
    cf    = found.get("cf_clearance")

    if not auth and not cf:
        print("[ERROR] No cookies found in the pasted text.")
        print("        Make sure you copied the correct request")
        print("        (it should point to app-api.salad.com).")
        input("\nPress Enter to exit...")
        raise SystemExit(1)

    print("-" * 60)
    if auth:
        print(f"[OK] auth         = {auth[:60]}...")
    else:
        print("[WARN] Cookie 'auth' not found.")

    if cf:
        print(f"[OK] cf_clearance = {cf}")
    else:
        print("[WARN] Cookie 'cf_clearance' not found.")

    if not auth or not cf:
        print("\n[ERROR] Both cookies are required to continue.")
        input("\nPress Enter to exit...")
        raise SystemExit(1)

    print()
    resp = input("Update config.json with these values? (y/n): ").strip().lower()
    if resp == "y":
        update_config(auth, cf)
        print("[OK] config.json updated.")
        print("\033[92m     Restart the server for the changes to take effect.\033[0m")
    else:
        print("[INFO] config.json was not modified.")

    input("\nPress Enter to exit...")


if __name__ == "__main__":
    main()
