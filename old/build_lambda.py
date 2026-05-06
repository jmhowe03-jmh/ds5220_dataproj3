"""
build_lambda.py — cross-platform Lambda zip builder (Windows/Mac/Linux)

Run with:
    python build_lambda.py

Output: lambda.zip  (upload this to your Lambda function in the AWS console)

Size strategy:
  - boto3 is excluded (pre-installed in every Lambda Python runtime)
  - dist-info, tests, pyi stubs, and docs are stripped after install
  - These two steps typically cut the unzipped size by 60-70%
"""

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PACKAGE_DIR = Path("package")
ZIP_NAME    = Path("lambda.zip")
HANDLER     = Path("lambda_function.py")

# boto3 is already in the Lambda runtime — do NOT bundle it
DEPENDENCIES = [
    "openmeteo-requests",
    "requests-cache",
    "retry-requests",
    "pandas",
    "matplotlib",
    "seaborn",
]

# Directory/file patterns to delete after install to slim the package
STRIP_PATTERNS = [
    "*.dist-info",
    "*.egg-info",
    "tests",
    "test",
    "testing",
    "*.pyi",
    "*.pyx",
    "*.pxd",
    "docs",
    "doc",
    "examples",
    "benchmarks",
]


def clean():
    print("==> Cleaning previous build...")
    if PACKAGE_DIR.exists():
        shutil.rmtree(PACKAGE_DIR)
    if ZIP_NAME.exists():
        ZIP_NAME.unlink()
    PACKAGE_DIR.mkdir()


def install_deps():
    print(f"==> Installing dependencies into ./{PACKAGE_DIR}/ ...")
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--isolated",
        "--platform", "manylinux2014_x86_64",
        "--target", str(PACKAGE_DIR),
        "--implementation", "cp",
        "--python-version", "3.12",
        "--only-binary=:all:",
        "--upgrade",
        "--quiet",
    ] + DEPENDENCIES

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print("    Platform-specific install failed, falling back to simple install...")
        cmd_simple = [
            sys.executable, "-m", "pip", "install",
            "--isolated",
            "--target", str(PACKAGE_DIR),
            "--upgrade",
            "--quiet",
        ] + DEPENDENCIES
        result2 = subprocess.run(cmd_simple, capture_output=True, text=True)
        if result2.returncode != 0:
            print("ERROR during pip install:")
            print(result2.stderr)
            sys.exit(1)

    print("    Dependencies installed.")


def strip_package():
    """Remove files that are not needed at runtime to reduce zip size."""
    print("==> Stripping unnecessary files...")
    removed_bytes = 0

    for pattern in STRIP_PATTERNS:
        for path in PACKAGE_DIR.rglob(pattern):
            if path.is_dir():
                size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
                shutil.rmtree(path)
            elif path.is_file():
                size = path.stat().st_size
                path.unlink()
            else:
                continue
            removed_bytes += size

    # Also remove __pycache__ dirs and .pyc files
    for path in list(PACKAGE_DIR.rglob("__pycache__")):
        if path.is_dir():
            shutil.rmtree(path)
    for path in list(PACKAGE_DIR.rglob("*.pyc")):
        path.unlink()

    removed_mb = removed_bytes / (1024 * 1024)
    print(f"    Stripped {removed_mb:.1f} MB of unnecessary files.")


def copy_handler():
    print(f"==> Copying {HANDLER} into package...")
    if not HANDLER.exists():
        print(f"ERROR: {HANDLER} not found. Run this script from the dataproj3 folder.")
        sys.exit(1)
    shutil.copy(HANDLER, PACKAGE_DIR / HANDLER.name)


def build_zip():
    print(f"==> Creating {ZIP_NAME} ...")
    with zipfile.ZipFile(ZIP_NAME, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for filepath in sorted(PACKAGE_DIR.rglob("*")):
            if not filepath.is_file():
                continue
            arcname = filepath.relative_to(PACKAGE_DIR)
            zf.write(filepath, arcname)

    zip_mb     = ZIP_NAME.stat().st_size / (1024 * 1024)
    unzip_mb   = sum(f.stat().st_size for f in PACKAGE_DIR.rglob("*") if f.is_file()) / (1024 * 1024)

    print(f"\n✅  Done!")
    print(f"   Zipped size  : {zip_mb:.1f} MB")
    print(f"   Unzipped size: {unzip_mb:.1f} MB  (Lambda limit: 250 MB)")

    if unzip_mb > 250:
        print("\n⚠️  Unzipped size still exceeds 250 MB.")
        print("   Use the Lambda Layer approach instead — ask Claude to set that up.")
    elif zip_mb > 50:
        print("\n   Zip is over 50 MB — upload via S3, not directly in the browser.")
        print("   S3 → Upload lambda.zip → copy the HTTPS URL → Lambda → Code → Upload from S3.")
    else:
        print("\n   Upload directly: Lambda → Code → Upload from → .zip file")


if __name__ == "__main__":
    if not HANDLER.exists():
        print(f"ERROR: Run this script from the same folder as {HANDLER}")
        sys.exit(1)

    clean()
    install_deps()
    strip_package()
    copy_handler()
    build_zip()
