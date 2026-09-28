import zipfile
from pathlib import Path

base = Path(r"d:\python_project\Bee\backend\data\marketplace")

stored = base / "bundles" / "bee" / "open-code-review-delegate" / "1.0.0.zip"
print("=== STORED BUNDLE (single source of truth) ===")
with zipfile.ZipFile(stored) as z:
    for n in z.namelist():
        print(" ", n)

snap = base / "snapshots" / "snapshot.zip"
print("\n=== SNAPSHOT.ZIP (what the client installs) ===")
if snap.exists():
    with zipfile.ZipFile(snap) as z:
        names = [n for n in z.namelist() if "open-code-review-delegate" in n]
        for n in names:
            print(" ", n)
        if not names:
            print("  (no open-code-review-delegate entries)")
else:
    print("  snapshot.zip not found")
