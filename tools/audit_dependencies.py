"""Check each exact pin against the public version-specific PyPI advisory feed."""
import json
from pathlib import Path
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def check(pin):
    package, version = pin
    url = f"https://pypi.org/pypi/{package}/{version}/json"
    with urllib.request.urlopen(url, timeout=20) as response:
        record = json.load(response)
    return package, version, [item["id"] for item in record["vulnerabilities"] if not item.get("withdrawn")]


def main():
    pins = set()
    for lock in ("bootstrap.lock", "build.lock", "requirements.lock", "requirements-dev.lock",
                 "requirements-portable.lock"):
        for line in Path(lock).read_text().splitlines():
            match = re.match(r"^([\w.-]+)==(\S+) --hash=sha256:[a-f0-9]{64}(?: |$)", line)
            if match:
                pins.add((match[1].lower(), match[2]))
            elif line.strip() and not line.startswith(("#", "-r ")):
                raise ValueError(f"Malformed or unhashed pin in {lock}")
    with ThreadPoolExecutor(max_workers=6) as workers:
        results = list(workers.map(check, sorted(pins)))
    findings = [f"{name} {version}: {', '.join(ids)}" for name, version, ids in results if ids]
    print(f"Checked {len(pins)} pinned releases.")
    for finding in findings:
        print(finding)
    return bool(findings)


if __name__ == "__main__":
    raise SystemExit(main())
