#!/usr/bin/env python3
"""Run mypy on the test cases for the stdlib and third-party stubs."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from itertools import product
from pathlib import Path
from typing_extensions import TypeAlias

from utils import (
    PackageInfo,
    colored,
    get_all_testcase_directories,
    get_recursive_requirements,
    print_error,
    print_success_msg,
    testcase_dir_from_package_name,
)

ReturnCode: TypeAlias = int

SUPPORTED_PLATFORMS = ["linux", "darwin", "win32"]
SUPPORTED_VERSIONS = ["3.11", "3.10", "3.9", "3.8", "3.7"]


def package_with_test_cases(package_name: str) -> PackageInfo:
    """Helper function for argument-parsing"""

    if package_name == "stdlib":
        return PackageInfo("stdlib", Path("test_cases"))
    test_case_dir = testcase_dir_from_package_name(package_name)
    if test_case_dir.is_dir():
        if not os.listdir(test_case_dir):
            raise argparse.ArgumentTypeError(f"{package_name!r} has a 'test_cases' directory but it is empty!")
        return PackageInfo(package_name, test_case_dir)
    raise argparse.ArgumentTypeError(f"No test cases found for {package_name!r}!")


parser = argparse.ArgumentParser(description="Script to run mypy against various test cases for typeshed's stubs")
parser.add_argument(
    "packages_to_test",
    type=package_with_test_cases,
    nargs="*",
    action="extend",
    help="Test only these packages (defaults to all typeshed stubs that have test cases)",
)
parser.add_argument(
    "--all",
    action="store_true",
    help=(
        'Run tests on all available platforms and versions (defaults to "False"). '
        "Note that this cannot be specified if --platform and/or --python-version are specified."
    ),
)
parser.add_argument("--quiet", action="store_true", help="Print less output to the terminal")
parser.add_argument(
    "--platform",
    dest="platforms_to_test",
    choices=SUPPORTED_PLATFORMS,
    nargs="*",
    action="extend",
    help=(
        "Run mypy for certain OS platforms (defaults to sys.platform). "
        "Note that this cannot be specified if --all is also specified."
    ),
)
parser.add_argument(
    "-p",
    "--python-version",
    dest="versions_to_test",
    choices=SUPPORTED_VERSIONS,
    nargs="*",
    action="extend",
    help=(
        "Run mypy for certain Python versions (defaults to sys.version_info[:2]). "
        "Note that this cannot be specified if --all is also specified."
    ),
)


def test_testcase_directory(package: PackageInfo, version: str, platform: str, quiet: bool) -> ReturnCode:
    package_name, test_case_directory = package
    is_stdlib = package_name == "stdlib"

    if not quiet:
        msg = f"Running mypy --platform {platform} --python-version {version} on the "
        msg += "standard library test cases..." if is_stdlib else f"test cases for {package_name!r}..."
        print(msg, end=" ")

    flags = [
        "--python-version",
        version,
        "--show-traceback",
        "--show-error-codes",
        "--no-error-summary",
        "--platform",
        platform,
        "--no-site-packages",
        "--strict",
        "--pretty",
    ]

    # --warn-unused-ignores doesn't work for files inside typeshed.
    # SO, to work around this, we copy the test_cases directory into a TemporaryDirectory.
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        new_test_case_dir = td_path / "test_cases"
        shutil.copytree(test_case_directory, new_test_case_dir)
        env_vars = dict(os.environ)
        if is_stdlib:
            flags.extend(["--custom-typeshed-dir", str(Path(__file__).parent.parent)])
        else:
            # HACK: we want to run these test cases in an isolated environment --
            # we want mypy to see all stub packages listed in the "requires" field of METADATA.toml
            # (and all stub packages required by those stub packages, etc. etc.),
            # but none of the other stubs in typeshed.
            #
            # The best way of doing that without stopping --warn-unused-ignore from working
            # seems to be to create a "new typeshed" directory in a tempdir
            # that has only the required stubs copied over.
            new_typeshed = td_path / "typeshed"
            os.mkdir(new_typeshed)
            shutil.copytree(Path("stdlib"), new_typeshed / "stdlib")
            requirements = get_recursive_requirements(package_name)
            # mypy refuses to consider a directory a "valid typeshed directory"
            # unless there's a stubs/mypy-extensions path inside it,
            # so add that to the list of stubs to copy over to the new directory
            for requirement in requirements + ["mypy-extensions"]:
                shutil.copytree(Path("stubs", requirement), new_typeshed / "stubs" / requirement)
            env_vars["MYPYPATH"] = os.pathsep.join(map(str, new_typeshed.glob("stubs/*")))
            flags.extend(["--custom-typeshed-dir", str(td_path / "typeshed")])

        # If the test-case filename ends with -py39,
        # only run the test if --python-version was set to 3.9 or higher (for example)
        for path in new_test_case_dir.rglob("*.py"):
            if match := re.fullmatch(r".*-py3(\d{1,2})", path.stem):
                minor_version_required = int(match[1])
                assert f"3.{minor_version_required}" in SUPPORTED_VERSIONS
                if minor_version_required <= int(version.split(".")[1]):
                    flags.append(str(path))
            else:
                flags.append(str(path))

        result = subprocess.run([sys.executable, "-m", "mypy", *flags], capture_output=True, env=env_vars)

    if result.returncode:
        print_error("failure\n")
        replacements = (str(new_test_case_dir), str(test_case_directory))
        if result.stderr:
            print_error(result.stderr.decode(), fix_path=replacements)
        if result.stdout:
            print_error(result.stdout.decode(), fix_path=replacements)
    elif not quiet:
        print_success_msg()
    return result.returncode


def main() -> ReturnCode:
    args = parser.parse_args()

    testcase_directories = args.packages_to_test or get_all_testcase_directories()
    if args.all:
        if args.platforms_to_test:
            parser.error("Cannot specify both --platform and --all")
        if args.versions_to_test:
            parser.error("Cannot specify both --python-version and --all")
        platforms_to_test, versions_to_test = SUPPORTED_PLATFORMS, SUPPORTED_VERSIONS
    else:
        platforms_to_test = args.platforms_to_test or [sys.platform]
        versions_to_test = args.versions_to_test or [f"3.{sys.version_info[1]}"]

    code = 0
    for platform, version, directory in product(platforms_to_test, versions_to_test, testcase_directories):
        code = max(code, test_testcase_directory(directory, version, platform, args.quiet))
    if code:
        print_error("\nTest completed with errors")
    else:
        print(colored("\nTest completed successfully!", "green"))

    return code


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        print_error("Test aborted due to KeyboardInterrupt!")
        code = 1
    raise SystemExit(code)
