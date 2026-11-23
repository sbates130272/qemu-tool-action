#!/usr/bin/env python3
"""The logic of qemu-tool-action.

Run by main.js and post.js as `qemu_tool_action.py main|post`. Standard
library only: it has to run before anything has been installed, and inside a
`container:` job whose image this action knows nothing about.

Inputs arrive the way the runner hands them to a JavaScript action, as
INPUT_<NAME> environment variables with the hyphens kept.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import urllib.request
import uuid
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import escape

OPERATIONS = ("up", "install", "exec", "command", "down")
NS_FLEET = "http://qemu-tool/fleet/1.0"
DEFAULT_BASE = Path("/var/lib/qemu-tool")
MIN_ORAS = (1, 3, 4)
STACK_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# Under <work-dir>/action each stack has a directory of its own, by name.
# What the action keeps for itself there has names no stack can have.
ACTION_OWN = Path("action") / ".tool"
# Keepalives are explicit so a wedged guest is dropped after three minutes
# on any OpenSSH, not only on Debian's, which turns them on under BatchMode.
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "BatchMode=yes",
    "-o", "IdentitiesOnly=yes",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=6",
]
# What qemu-tool needs on PATH to pull and boot a published guest, and the
# apt package carrying each. zstd is not in quick-start's own preflight, and
# the pull dies without it after that preflight has passed.
APT_FOR_TOOL = {
    "qemu-img": "qemu-utils",
    "zstd": "zstd",
    "ansible-playbook": "ansible-core",
    "git": "git",
    "ssh": "openssh-client",
}


class Failure(Exception):
    """A failure with a message fit for an ::error:: line."""


# ── Runner plumbing ──────────────────────────────────────────────────────────

def inp(name: str, default: str = "") -> str:
    value = os.environ.get(f"INPUT_{name.upper().replace(' ', '_')}", "")
    return value.strip() or default


def flag(name: str, default: bool = False) -> bool:
    value = inp(name).lower()
    if not value:
        return default
    if value in ("true", "yes", "1", "on"):
        return True
    if value in ("false", "no", "0", "off"):
        return False
    raise Failure(f"input {name} must be true or false, not {value!r}")


def lines_of(name: str) -> list[str]:
    return [line.strip() for line in inp(name).splitlines()
            if line.strip() and not line.strip().startswith("#")]


def whole_number(name: str, default: str) -> int:
    value = inp(name, default)
    # isdigit() alone takes '²', which int() then refuses.
    if not (value.isascii() and value.isdigit()):
        raise Failure(f"input {name} must be a whole number, not {value!r}")
    return int(value)


def _append(env_var: str, text: str) -> None:
    path = os.environ.get(env_var)
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)


def _keyed(key: str, value: str) -> str:
    delimiter = f"qta_{uuid.uuid4().hex}"
    return f"{key}<<{delimiter}\n{value}\n{delimiter}\n"


def set_output(key: str, value: str) -> None:
    _append("GITHUB_OUTPUT", _keyed(key, value))


def export_env(key: str, value: str) -> None:
    os.environ[key] = value
    _append("GITHUB_ENV", _keyed(key, value))


def save_state(key: str, value: str) -> None:
    _append("GITHUB_STATE", _keyed(key, value))


def add_path(directory: Path) -> None:
    os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ.get('PATH', '')}"
    _append("GITHUB_PATH", f"{directory}\n")


def command_text(text: str) -> str:
    """A message as a workflow command carries it: one line."""
    return (str(text).replace("%", "%25").replace("\r", "%0D")
            .replace("\n", "%0A"))


def say(message: str) -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print(f"::warning::{command_text(message)}", flush=True)


class group:
    """A folded section of the log. Groups do not nest."""

    def __init__(self, title: str) -> None:
        self.title = title

    def __enter__(self) -> None:
        print(f"::group::{command_text(self.title)}", flush=True)

    def __exit__(self, *_exc: object) -> None:
        print("::endgroup::", flush=True)


def run(cmd: list, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    argv = [str(c) for c in cmd]
    say(f"$ {shlex.join(argv)}")
    try:
        result = subprocess.run(argv, **kwargs)
    except FileNotFoundError:
        raise Failure(f"{argv[0]} is not on PATH") from None
    if check and result.returncode != 0:
        raise Failure(f"`{shlex.join(argv[:3])} ...` exited "
                      f"{result.returncode}")
    return result


def run_within(cmd: list, seconds: int, what: str, **kwargs) -> None:
    """run(), given up on after `seconds`, with everything it started.

    The command gets a process group of its own so that what it spawned,
    `docker compose up` under quick-start for one, is stopped with it.
    """
    argv = [str(c) for c in cmd]
    say(f"$ {shlex.join(argv)}")
    try:
        child = subprocess.Popen(argv, start_new_session=True, **kwargs)
    except FileNotFoundError:
        raise Failure(f"{argv[0]} is not on PATH") from None
    try:
        code = child.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        _stop_group(child)
        raise Failure(f"{what} did not finish within {seconds} s") from None
    except BaseException:
        # An interrupted step: subprocess.run() kills its child on the way
        # out, and a child in a session of its own gets no signal otherwise.
        _stop_group(child)
        raise
    if code != 0:
        raise Failure(f"`{shlex.join(argv[:3])} ...` exited {code}")


def _stop_group(child: subprocess.Popen, grace: int = 15) -> None:
    """End a run_within() command's whole process group.

    SIGTERM, then SIGKILL to the group whether or not its leader went
    quietly: something it started may have ignored the first.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, sig)
        except OSError:
            pass
        try:
            child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass


def quiet(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    """Run and capture. A missing program is exit 127, not an exception."""
    argv = [str(c) for c in cmd]
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              errors="replace", **kwargs)
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, 127, "",
                                           f"{argv[0]} is not on PATH")


def bounded(cmd: list, seconds: int, **kwargs) -> subprocess.CompletedProcess:
    """quiet(), with a hung command counted as a failed one."""
    try:
        return quiet(cmd, timeout=seconds, **kwargs)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timed out")


def sudo() -> list[str] | None:
    """The prefix that makes a command root, or None when there is none."""
    if os.geteuid() == 0:
        return []
    if shutil.which("sudo") and quiet(["sudo", "-n", "true"]).returncode == 0:
        return ["sudo", "-n"]
    return None


def in_container() -> bool:
    return Path("/.dockerenv").exists()


# ── Where things go ──────────────────────────────────────────────────────────

def _usable(directory: Path) -> bool:
    return directory.is_dir() and os.access(directory, os.W_OK | os.X_OK)


def mount_point(directory: Path, mountinfo: str) -> str:
    """The mount point, from /proc/self/mountinfo text, a path lives on."""
    target = str(directory)
    best = "/"
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        point = fields[4].replace("\\040", " ")
        if (target == point or target.startswith(point.rstrip("/") + "/")) \
                and len(point) > len(best):
            best = point
    return best


def _shared_with_host(directory: Path) -> bool:
    """In a container: is this on a mount, rather than the image's own disk?"""
    try:
        info = Path("/proc/self/mountinfo").read_text()
    except OSError:
        return False
    return mount_point(directory.resolve(), info) != "/"


def base_dir(operation: str) -> Path:
    """The root of everything this action writes.

    /var/lib/qemu-tool when it can be had, because a `container:` job has to
    bind-mount the stack and the images at one path on both sides and that is
    the path qemu-tool's own defaults name. A directory that already exists
    and belongs to someone else is left alone.
    """
    explicit = inp("work-dir")
    if explicit:
        path = Path(explicit).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        if in_container() and (not _shared_with_host(path)
                               or str(path).startswith(("/__w/", "/__t/"))):
            warn(f"work-dir {path} may not be where the host has it. The "
                 "stack's containers are started by the host's Docker "
                 "daemon and only see it under a mount the host shares at "
                 "the same path; the runner's workspace and temp "
                 "directories are not at the same path.")
        return path
    if in_container() and not _shared_with_host(DEFAULT_BASE):
        message = (f"{DEFAULT_BASE} is not bind-mounted into this job "
                   "container. Add `-v /var/lib/qemu-tool:/var/lib/qemu-tool` "
                   "to container.options: the Docker daemon resolves the "
                   "stack's bind mounts on the host.")
        if operation == "up":
            raise Failure(message)
        warn(message)
    if _usable(DEFAULT_BASE):
        return DEFAULT_BASE
    if not DEFAULT_BASE.exists():
        root = sudo()
        if root is not None:
            owner = f"{os.getuid()}:{os.getgid()}"
            if (quiet(root + ["mkdir", "-p", DEFAULT_BASE]).returncode == 0 and
                    quiet(root + ["chown", owner, DEFAULT_BASE]).returncode == 0):
                return DEFAULT_BASE
    fallback = Path(os.environ.get("RUNNER_TEMP") or "/tmp") / "qemu-tool"
    fallback.mkdir(parents=True, exist_ok=True)
    say(f"{DEFAULT_BASE} is not writable, using {fallback}")
    return fallback


# ── Installing qemu-tool and what it needs ───────────────────────────────────

def clean_copy(source: Path, target: Path) -> None:
    """Copy a source tree without what a build or a checkout leaves in it."""
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(
        "build", "*.egg-info", "__pycache__", "*.pyc", ".git"))


def repository_url(name: str) -> str:
    if "://" in name or name.startswith("git@"):
        return name
    return f"https://github.com/{name}.git"


def fetch_ref(base: Path, ref: str) -> tuple[Path, str]:
    """A shallow checkout of qemu-minimal at a tag, a branch or a commit."""
    url = repository_url(inp("qemu-tool-repository",
                             "sbates130272/qemu-minimal"))
    if shutil.which("git") is None:
        raise Failure("qemu-tool-ref needs git on PATH")
    clone = base / ACTION_OWN / "qemu-minimal"
    shutil.rmtree(clone, ignore_errors=True)
    run(["git", "init", "-q", clone])
    if run(["git", "-C", clone, "fetch", "-q", "--depth", "1", url, ref],
           check=False).returncode != 0:
        raise Failure(f"cannot fetch {ref!r} from {url}. qemu-tool-ref takes "
                      "a tag, a branch or a full commit SHA.")
    run(["git", "-C", clone, "checkout", "-q", "--detach", "FETCH_HEAD"])
    sha = quiet(["git", "-C", clone, "rev-parse", "HEAD"]).stdout.strip()
    if not (clone / "qemu" / "pyproject.toml").is_file():
        raise Failure(f"{url} at {ref} has no qemu/pyproject.toml; it is not "
                      "a qemu-minimal tree")
    return clone, sha


def check_install_inputs() -> None:
    """qemu-tool-ref excludes the other two ways of saying which qemu-tool."""
    others = [name for name in ("qemu-tool-version", "qemu-tool-source")
              if inp(name)]
    if inp("qemu-tool-ref") and others:
        raise Failure(f"qemu-tool-ref and {' and '.join(others)} each say "
                      "which qemu-tool to install; give one")


def install_source(base: Path) -> dict:
    """What to install, from the three inputs that can say."""
    check_install_inputs()
    version = inp("qemu-tool-version")
    source = inp("qemu-tool-source")
    ref = inp("qemu-tool-ref")
    if source and version:
        # What 2.0.0 documented, kept: a source directory outranks a version.
        warn("qemu-tool-source overrides qemu-tool-version; give one of them")
    staged = base / ACTION_OWN / "src"
    if ref:
        clone, sha = fetch_ref(base, ref)
        # Installed from a copy, so the checkout stays as fetched.
        clean_copy(clone / "qemu", staged)
        return {"kind": "ref", "spec": [str(staged)], "sha": sha,
                "checkout": str(clone), "label": f"ref {sha}"}
    if source:
        path = Path(source).expanduser().resolve()
        if not (path / "pyproject.toml").is_file():
            raise Failure(f"qemu-tool-source {path} has no pyproject.toml; "
                          "pass the qemu/ directory of a qemu-minimal checkout")
        # Not editable, and from a copy: nothing is written into the
        # caller's checkout, and the install does not change under a fleet
        # when the checkout does.
        clean_copy(path, staged)
        return {"kind": "source", "spec": [str(staged)], "sha": "",
                "checkout": "", "label": f"source {path}"}
    version = version or ">=2.1,<4"
    if version == "latest":
        spec = "qemu-tool"
    elif version[0].isdigit():
        spec = f"qemu-tool=={version}"
    else:
        spec = f"qemu-tool{version}"
    return {"kind": "pypi", "spec": [spec], "sha": "", "checkout": "",
            "label": f"pypi {spec}"}


def tool_python(tool: str) -> str:
    """The interpreter a qemu-tool entry point runs under."""
    try:
        first = Path(tool).read_text(errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return sys.executable
    if first.startswith("#!"):
        candidate = first[2:].split()[0]
        if Path(candidate).is_file():
            return candidate
    return sys.executable


def job_stamp() -> str:
    return ":".join(os.environ.get(key, "") for key in
                    ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB"))


def reusable(recorded: dict, job: str, asked: bool, label: str) -> bool:
    """May this step use the install an earlier step of the job made?

    Yes when it is the same job and the step either named no qemu-tool of
    its own or named the one already there. A later step must not reinstall
    under a running fleet for want of saying which version it meant.
    """
    if not job.strip(":") or recorded.get("job") != job:
        return False
    return not asked or recorded.get("label") == label


def install_qemu_tool(base: Path) -> dict[str, str]:
    source = {"kind": "preinstalled", "sha": "", "checkout": ""}
    if not flag("install", True):
        tool = shutil.which("qemu-tool")
        if tool is None:
            raise Failure("install is false and no qemu-tool is on PATH")
    else:
        if sys.version_info < (3, 10):
            raise Failure("qemu-tool needs Python 3.10 or newer; this runner's "
                          f"python3 is {platform.python_version()}")
        asked = bool(inp("qemu-tool-version") or inp("qemu-tool-source")
                     or inp("qemu-tool-ref"))
        venv = base / ACTION_OWN / "venv"
        job = job_stamp()
        marker = venv / "qemu-tool-action.json"
        entry: Path | None = venv / "bin" / "qemu-tool"
        try:
            recorded = json.loads(marker.read_text())
        except (OSError, ValueError):
            recorded = {}
        if (recorded and entry.is_file() and not asked
                and reusable(recorded, job, False, "")):
            say("reusing the install an earlier step of this job made")
            source = recorded
        else:
            source = install_source(base)
            if recorded and entry.is_file() and reusable(
                    recorded, job, True, source["label"]):
                say("reusing the install an earlier step of this job made")
            elif quiet([sys.executable, "-m", "venv", "--clear",
                        venv]).returncode == 0:
                if recorded.get("job") == job and job.strip(":"):
                    warn("this step asks for a different qemu-tool than the "
                         "one an earlier step of this job installed; "
                         "reinstalling")
                run([venv / "bin" / "python", "-m", "pip", "install", "-q",
                     "--disable-pip-version-check", *source["spec"]])
                marker.write_text(json.dumps(dict(source, job=job)))
            else:
                entry = None
        if entry is not None:
            # Only the entry point goes on PATH. The venv's own bin holds a
            # python3 and a pip that must not shadow the runner's.
            bin_dir = base / ACTION_OWN / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            link = bin_dir / "qemu-tool"
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(entry)
        else:
            # No venv module (python3-venv missing). A user install still
            # lands ahead of an image's own copy once its bin is on PATH.
            say("python3 -m venv is unavailable; installing with pip --user")
            pip = [sys.executable, "-m", "pip", "install", "-q", "--user"]
            if run(pip + ["--break-system-packages", *source["spec"]],
                   check=False).returncode != 0:
                # pip before 23.0.1 does not know the flag, and needs none.
                run(pip + source["spec"])
            bin_dir = Path.home() / ".local" / "bin"
        add_path(bin_dir)
        tool = str(bin_dir / "qemu-tool")
        if not Path(tool).is_file():
            raise Failure(f"the install left no {tool}")
    version = quiet([tool, "--version"]).stdout.strip().split()[-1:]
    version_text = version[0] if version else "unknown"
    where = f" ({source['kind']} {source['sha'][:12]})" if source["sha"] else ""
    say(f"qemu-tool {version_text}{where} at {tool}")
    if version_text[0].isdigit() and int(version_text.split(".")[0]) < 2:
        raise Failure(f"qemu-tool {version_text} is a 1.x release; this "
                      "version of the action drives 2.x and 3.x. Use "
                      "sbates130272/qemu-tool-action@v1 for 1.x.")
    set_output("qemu-tool-version", version_text)
    set_output("qemu-tool-path", tool)
    set_output("qemu-tool-sha", source["sha"])
    set_output("qemu-tool-checkout", source["checkout"])
    return {"tool": tool, "python": tool_python(tool), "version": version_text,
            "sha": source["sha"]}


# What the stack's own containers run. Every qemu container starts by
# installing qemu-tool: from a source tree at /qemu-tool-src/qemu when the
# stack directory, which quick-start mounts there, holds one, else the
# latest release from PyPI, whatever the runner has. A compose file rendered
# by one version and executed by another is how a fleet breaks on the day a
# release changes run-vm's command line, so the runner's own installed
# package is put there, as a tree the container's install accepts.

_PACKAGE_INFO = """import json
from importlib import metadata
from pathlib import Path
import qemu_tool
dist = metadata.distribution("qemu-tool")
print(json.dumps({
    "package": str(Path(qemu_tool.__file__).resolve().parent),
    "version": dist.version,
    "requires": [r for r in (dist.requires or []) if "extra ==" not in r],
}))
"""

_PYPROJECT = """[build-system]
requires = ["setuptools>=70"]
build-backend = "setuptools.build_meta"

[project]
name = "qemu-tool"
version = "{version}"
requires-python = ">=3.10"
dependencies = {requires}

[project.scripts]
qemu-tool = "qemu_tool.__main__:main"

[tool.setuptools.packages.find]
include = ["qemu_tool*"]
"""


def checkout_above(*starts: Path) -> Path | None:
    """The qemu-minimal checkout quick-start would find from these places.

    Its own test, on its own two starting points: the installed module and
    the working directory. Finding one, it copies that tree's qemu/ into the
    stack directory for the containers, over anything already there.
    """
    for start in starts:
        start = Path(start).resolve()
        for candidate in (start, *start.parents):
            if ((candidate / "ansible" / "playbooks" / "vm-quickstart.yml")
                    .is_file() and (candidate / "qemu" / "pyproject.toml")
                    .is_file()):
                return candidate
    return None


def container_source(python: str, stack_dir: Path) -> tuple[str, Path]:
    """Put the runner's qemu-tool where the stack's containers install from.

    Returns the version placed and the directory of the installed package.
    """
    found = quiet([python, "-c", _PACKAGE_INFO])
    if found.returncode != 0:
        raise Failure("cannot locate the installed qemu_tool package: "
                      f"{found.stderr.strip()[-300:]}")
    info = json.loads(found.stdout)
    package = Path(info["package"])
    target = stack_dir / "qemu"
    stack_dir.mkdir(parents=True, exist_ok=True)
    if ((package.parent / "pyproject.toml").is_file()
            and not (package / "share").is_dir()):
        # An editable install of a checkout: its qemu/ directory is the tree.
        clean_copy(package.parent, target)
    else:
        # An installed package: its directory is all the containers need.
        # A wheel carries the fleets and stacks inside it; a .deb keeps
        # them under /usr/share, and run-vm and ernic-stats, which are what
        # the containers run, read neither.
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir()
        clean_copy(package, target / "qemu_tool")
        (target / "pyproject.toml").write_text(_PYPROJECT.format(
            version=info["version"], requires=json.dumps(info["requires"])))
    return info["version"], package


def setup_kvm() -> None:
    kvm = Path("/dev/kvm")
    if not kvm.exists():
        warn("/dev/kvm does not exist on this runner. Guests need KVM: use a "
             "runner with nested virtualisation, and in a container job pass "
             "`--device /dev/kvm`.")
        return
    if os.access(kvm, os.R_OK | os.W_OK):
        return
    root = sudo()
    if root is None:
        warn("/dev/kvm is not accessible to this user and there is no sudo "
             "to change that")
        return
    rule = ('KERNEL=="kvm", GROUP="kvm", MODE="0666", '
            'OPTIONS+="static_node=kvm"\n')
    if not in_container() and shutil.which("udevadm"):
        subprocess.run(root + ["tee", "/etc/udev/rules.d/99-kvm4all.rules"],
                       input=rule, text=True, stdout=subprocess.DEVNULL,
                       check=False)
        quiet(root + ["udevadm", "control", "--reload-rules"])
        quiet(root + ["udevadm", "trigger", "--name-match=kvm"])
        time.sleep(1)
    if not os.access(kvm, os.R_OK | os.W_OK):
        quiet(root + ["chmod", "666", str(kvm)])
    say(f"/dev/kvm: {'accessible' if os.access(kvm, os.R_OK | os.W_OK) else 'STILL NOT accessible'}")


def oras_version() -> tuple[int, ...] | None:
    if shutil.which("oras") is None:
        return None
    match = re.search(r"Version:\s*v?(\d+)\.(\d+)\.(\d+)",
                      quiet(["oras", "version"]).stdout)
    return tuple(int(part) for part in match.groups()) if match else None


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
        return response.read()


def setup_oras(base: Path) -> None:
    have = oras_version()
    if have is not None and have >= MIN_ORAS:
        return
    version = inp("oras-version", "1.3.4").lstrip("v")
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if arch is None:
        raise Failure(f"no oras build is known for {platform.machine()}; "
                      "install oras yourself and set setup-host: false")
    say(f"oras {'.'.join(map(str, have)) if have else 'absent'}; "
        f"installing {version}")
    release = ("https://github.com/oras-project/oras/releases/download/"
               f"v{version}")
    name = f"oras_{version}_linux_{arch}.tar.gz"
    try:
        archive = _fetch(f"{release}/{name}")
        sums = _fetch(f"{release}/oras_{version}_checksums.txt").decode()
    except OSError as exc:  # URLError, HTTPError and timeouts are all OSError
        raise Failure(f"cannot download oras {version} ({exc}); install "
                      "oras 1.3.4 or newer yourself and set setup-host: "
                      "false") from None
    wanted = next((line.split()[0] for line in sums.splitlines()
                   if line.strip().endswith(name)), None)
    if wanted != hashlib.sha256(archive).hexdigest():
        raise Failure(f"{name} does not match its published sha256")
    bin_dir = base / ACTION_OWN / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    packed = bin_dir / name
    packed.write_bytes(archive)
    try:
        with tarfile.open(packed) as tar:
            source = tar.extractfile(tar.getmember("oras"))
            if source is None:
                raise KeyError("oras")
            (bin_dir / "oras").write_bytes(source.read())
    except (KeyError, tarfile.TarError):
        raise Failure(f"{name} carries no oras binary") from None
    finally:
        packed.unlink()
    (bin_dir / "oras").chmod(0o755)
    add_path(bin_dir)


def setup_host(base: Path) -> None:
    setup_kvm()
    missing = sorted({pkg for tool, pkg in APT_FOR_TOOL.items()
                      if shutil.which(tool) is None})
    if missing:
        root = sudo()
        if root is None or shutil.which("apt-get") is None:
            warn(f"missing and not installable here: {', '.join(missing)}")
        else:
            apt = root + ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get"]
            run(apt + ["update", "-q"], check=False)
            run(apt + ["install", "-y", "-q", "--no-install-recommends",
                       *missing])
    setup_oras(base)
    if shutil.which("docker") is None:
        warn("docker is not on PATH; the stacks need Docker with the "
             "compose plugin")


# ── Fleet settings ───────────────────────────────────────────────────────────

_ELEMENT = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_SIZES = ("vm-vcpus", "vm-vmem")


def overlay_elements() -> dict[str, str]:
    """The fleet XML elements the inputs ask for, in the order given."""
    elements: dict[str, str] = {}
    for name, element in (("vm-count", "vm-count"), ("vcpus", "vm-vcpus"),
                          ("vmem", "vm-vmem")):
        value = inp(name)
        if value:
            if not value.isdigit() or int(value) < 1:
                raise Failure(f"input {name} must be a positive whole "
                              f"number, not {value!r}")
            elements[element] = value
    for line in lines_of("fleet"):
        match = re.match(r"^([^:=\s]+)\s*[:=]\s*(.*)$", line)
        if not match or not _ELEMENT.match(match.group(1)):
            raise Failure(f"fleet: cannot read {line!r}; write one "
                          "`element: value` per line, e.g. `vm-nvme: 2`. "
                          "Anything with structure, such as a <devices> "
                          "block, goes in a file named by xml or xml-overlay.")
        elements[match.group(1)] = match.group(2).strip()
    if elements.get("vm-vmem", "").isdigit() and "vm-shm-size" not in elements:
        # Guest RAM is a shared memfd and must fit the container's shm.
        elements["vm-shm-size"] = f"{int(elements['vm-vmem']) + 1024}m"
    return elements


def sized_by_caller(elements: dict[str, str]) -> bool:
    return bool(set(_SIZES) & set(elements))


def files_set_sizes(files: list[str]) -> bool:
    """Does a fleet XML file of the caller's own size the guests?"""
    for name in files:
        try:
            root = ElementTree.parse(name).getroot()
        except (OSError, ElementTree.ParseError):
            continue  # qemu-tool reports an unreadable file better
        if any(child.tag.rsplit("}", 1)[-1] in _SIZES for child in root):
            return True
    return False


def render_overlay(elements: dict[str, str]) -> str:
    body = "".join(f"  <{name}>{escape(value)}</{name}>\n"
                   for name, value in elements.items())
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            "<!-- Written by qemu-tool-action from its inputs. -->\n"
            f'<fleet xmlns="{NS_FLEET}">\n{body}</fleet>\n')


_FLAVORS = """import json
from qemu_tool.quick_start import VARIANTS
from qemu_tool.resources import STACKS, shipped_path
print(json.dumps({name: str(shipped_path("stacks", STACKS, spec[0]))
                  for name, spec in VARIANTS.items()}))
"""


def shipped_fleets(python: str) -> dict[str, str]:
    """Every flavor the installed qemu-tool ships, and its fleet XML."""
    found = quiet([python, "-c", _FLAVORS])
    if found.returncode != 0:
        raise Failure("cannot list the fleets this qemu-tool ships: "
                      f"{found.stderr.strip()[-300:]}")
    return json.loads(found.stdout)


def packaged_fleet(python: str, flavor: str) -> Path:
    fleets = shipped_fleets(python)
    if flavor not in fleets:
        raise Failure(f"this qemu-tool ships no flavor {flavor!r}; it has "
                      f"{', '.join(sorted(fleets))}. A newer flavor needs a "
                      "newer qemu-tool (qemu-tool-version or qemu-tool-ref).")
    path = Path(fleets[flavor])
    if not path.is_file():
        raise Failure(f"the fleet XML of flavor {flavor!r} is missing from "
                      f"this qemu-tool install ({path})")
    return path


def existing(name: str) -> list[str]:
    files = []
    for entry in lines_of(name):
        path = Path(entry).expanduser().resolve()
        if not path.is_file():
            raise Failure(f"{name}: {path} does not exist")
        files.append(str(path))
    return files


def fleet_layers(python: str, flavor: str, action_dir: Path,
                 elements: dict[str, str]) -> tuple[list[str], list[str]]:
    """The fleet XML layers, lowest first, and which are the caller's own.

    A fleet of the caller's (xml) or the flavor's shipped file; then the
    caller's overlays (xml-overlay); then what the other inputs ask for.
    """
    own = existing("xml")
    overlays = existing("xml-overlay")
    if own and flavor != "default":
        warn(f"flavor {flavor!r} is not used: xml names the fleet")
    layers = own or [str(packaged_fleet(python, flavor))]
    layers += overlays
    if elements:
        overlay = action_dir / "overlay.xml"
        overlay.write_text(render_overlay(elements))
        say(f"wrote {overlay}:")
        say(overlay.read_text())
        layers.append(str(overlay))
    return layers, own + overlays


_FLEET_INFO = """import json, sys
from pathlib import Path
from qemu_tool.fleet_xml import FleetXml
cfg = FleetXml.parse_layers([Path(p) for p in sys.argv[1:]])
print(json.dumps({key: getattr(cfg, key, None) for key in
                  ("backing_ref", "count", "name_prefix", "rocjitsu_config")}))
"""


def check_fleet(tool: dict, layers: list[str]) -> dict:
    """Have qemu-tool read and render the fleet before anything is pulled.

    Its own refusals -- an element this version does not know, a guest image
    on a tag that moves, a guest with no NIC -- then arrive in the first
    minute and in its own words, not after a 2 GB download.
    """
    parsed = quiet([tool["python"], "-c", _FLEET_INFO, *layers])
    if parsed.returncode != 0:
        raise Failure(f"qemu-tool {tool['version']} cannot read this fleet:\n"
                      f"{(parsed.stderr or parsed.stdout).strip()[-1500:]}")
    command = [tool["tool"], "gen-compose", "--dry-run"]
    for layer in layers:
        command += ["--xml", layer]
    rendered = quiet(command)
    if rendered.returncode != 0:
        raise Failure(f"qemu-tool {tool['version']} cannot render this "
                      f"fleet:\n{rendered.stderr.strip()[-1500:]}")
    # A dry run that succeeds writes the files to stdout and nothing but
    # its warnings to stderr, and a warning runs to several lines.
    if rendered.stderr.strip():
        say(rendered.stderr.rstrip())
    try:
        return json.loads(parsed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}


def seed_key(state: dict, fleet: dict) -> None:
    """Use the caller's key and login in place of the image's published ones.

    quick-start fetches a guest's keypair from a referrer published beside
    its image, and skips that when the key directory already holds a key
    recorded against the same image. Writing that record is how an image
    that was not made by `qemu-tool publish-qcow` gets in.
    """
    key_file = inp("ssh-key-file")
    user = inp("ssh-user")
    if not key_file:
        if user:
            raise Failure("ssh-user needs ssh-key-file, the private key the "
                          "guest image accepts")
        return
    source = Path(key_file).expanduser().resolve()
    if not source.is_file():
        raise Failure(f"ssh-key-file {source} does not exist")
    key_dir = Path(state["key_dir"])
    key_dir.mkdir(parents=True, exist_ok=True)
    key = key_dir / "id_rsa"
    shutil.copyfile(source, key)
    key.chmod(0o600)
    checked = quiet(["ssh-keygen", "-y", "-P", "", "-f", key])
    if checked.returncode not in (0, 127):  # 127: no ssh-keygen to ask
        raise Failure(f"ssh-key-file {source} is not a private key this "
                      "action can use; it has to be a private key with no "
                      f"passphrase, not a .pub: {checked.stderr.strip()[-300:]}")
    (key_dir / "image-ref").write_text((fleet.get("backing_ref") or "") + "\n")
    info = key_dir / "vm-info.json"
    if user:
        info.write_text(json.dumps({"username": user}) + "\n")
    else:
        info.unlink(missing_ok=True)
    # quick-start republishes the key under the images directory for
    # `qemu-tool ssh`, and keeps a copy it finds there already.
    for stale in ("quickstart_id_rsa", "quickstart_id_rsa.pub"):
        (Path(state["images_dir"]) / "keys" / stale).unlink(missing_ok=True)
    say(f"guest login: {user or 'the fleet XML account'} with {source}")


# ── The guests ───────────────────────────────────────────────────────────────

_SHARED_KEYS = ("ansible_host", "ansible_user", "fleet_guest_rocjitsu_image")


def parse_inventory(text: str) -> tuple[dict[str, str], list[dict]]:
    """The hosts of a gen-compose fleet-inventory.yml, without PyYAML."""
    shared: dict[str, str] = {}
    hosts: list[dict] = []
    current: dict | None = None
    hosts_indent: int | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        match = re.match(r"^(?:- )?([A-Za-z0-9_.-]+):\s*(.*)$", stripped)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip().strip("\"'")
        if hosts_indent is not None and indent <= hosts_indent:
            hosts_indent, current = None, None
        if key == "hosts" and not value:
            hosts_indent, current = indent, None
            continue
        if hosts_indent is None:
            if key in _SHARED_KEYS and value:
                shared[key] = value
            continue
        if indent == hosts_indent + 2:
            current = {"name": key, "nics": [], "nic_ips": []}
            hosts.append(current)
        elif current is not None:
            if stripped.startswith("- "):
                current["nics"].append({})
            if indent >= hosts_indent + 6 and current["nics"]:
                # An entry of the guest's NIC list: index, mac, ip.
                current["nics"][-1][key] = value
                if key == "ip" and value:
                    current["nic_ips"].append(value)
            elif value:
                current.setdefault(key, value)
    return shared, hosts


def read_inventory(state: dict) -> tuple[dict[str, str], list[dict]]:
    inventory = Path(state["stack_dir"]) / "fleet-inventory.yml"
    if not inventory.is_file():
        raise Failure(f"{inventory} was not generated; the stack did not "
                      "come up")
    shared, hosts = parse_inventory(inventory.read_text())
    if not hosts:
        raise Failure(f"{inventory} names no guests")
    return shared, hosts


def image_username(key_dir: Path) -> str | None:
    try:
        return (json.loads((key_dir / "vm-info.json").read_text())
                .get("username") or "").strip() or None
    except (OSError, ValueError, AttributeError):
        return None


def container_ip(container: str) -> str | None:
    found = quiet(["docker", "inspect", "-f",
                   "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
                   container])
    addresses = found.stdout.split()
    return addresses[0] if found.returncode == 0 and addresses else None


def build_manifest(state: dict) -> list[dict]:
    key_dir = Path(state["key_dir"])
    shared, hosts = read_inventory(state)
    username = image_username(key_dir) or shared.get("ansible_user", "ubuntu")
    key = key_dir / "id_rsa"
    manifest = []
    for index, host in enumerate(hosts, start=1):
        if state["in_network"]:
            container = f"{state['stack_name']}-qemu-{index}-1"
            address, port = container_ip(container), 2222
            if address is None:
                raise Failure(f"container {container} has no address on the "
                              "stack network")
        else:
            address = host.get("ansible_host") or shared.get(
                "ansible_host", "127.0.0.1")
            port = int(host.get("ansible_port", 2222))
        # fleet_guest_ip is the NIC qemu-tool treats as the guest's own:
        # the first on the mesh, which is the one that reaches other guests.
        own = host.get("fleet_guest_ip")
        nics = host["nics"] or ([{"index": "1", "ip": own, "mac":
                                  host.get("fleet_guest_ernic_mac", "")}]
                                if own else [])
        ips = [nic["ip"] for nic in nics if nic.get("ip")]
        manifest.append({
            "name": host["name"],
            "index": index,
            "ssh_host": address,
            "ssh_port": port,
            "username": username,
            "ssh_key_file": str(key),
            "ssh_cmd": f"ssh -i {key} -p {port} {username}@{address}",
            "ssh_aliases": [],
            "rdma_ip": own or (ips[0] if ips else None),
            "rdma_ips": ips,
            "nics": nics,
            "rdma_device": None,
            "gpu": None,
        })
    return manifest


def ssh_base(vm: dict) -> list[str]:
    return ["ssh", *SSH_OPTS, "-i", vm["ssh_key_file"], "-p",
            str(vm["ssh_port"]), f"{vm['username']}@{vm['ssh_host']}"]


def show(title: str, cmd: list[str], seconds: int = 60) -> None:
    """Print a diagnostic command's output under a heading. Never raises."""
    say(f"--- {title}")
    try:
        subprocess.run(cmd, check=False, timeout=seconds)
    except (subprocess.TimeoutExpired, OSError) as exc:
        say(f"(not available: {exc})")


def wait_for_ssh(state: dict, manifest: list[dict], deadline: float) -> None:
    for vm in manifest:
        start = time.monotonic()
        while True:
            if bounded(ssh_base(vm) + ["true"], 60).returncode == 0:
                say(f"  {vm['name']}: answering SSH at "
                    f"{vm['ssh_host']}:{vm['ssh_port']} "
                    f"({int(time.monotonic() - start)}s)")
                break
            if time.monotonic() > deadline:
                show(f"{vm['name']}: last SSH attempt, verbose",
                     ["ssh", "-v", *ssh_base(vm)[1:], "true"])
                show(f"{vm['name']}: end of its container's log",
                     ["docker", "logs", "--tail", "80",
                      f"{state['stack_name']}-qemu-{vm['index']}-1"])
                raise Failure(f"{vm['name']} never answered SSH at "
                              f"{vm['ssh_host']}:{vm['ssh_port']} as "
                              f"{vm['username']}")
            time.sleep(5)


_RDMA_PROBE = r"""
command -v ibv_devinfo >/dev/null 2>&1 || { echo NOTOOL; exit 0; }
dev=$(ibv_devinfo -l 2>/dev/null | sed -n '2p' | tr -d ' \t\r')
[ -n "$dev" ] || { sudo -n modprobe ionic_rdma 2>/dev/null; exit 1; }
ibv_devinfo -d "$dev" 2>/dev/null | grep -q PORT_ACTIVE || exit 1
echo "DEV=$dev"
echo "ADDRS=$(ip -4 -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | tr '\n' ' ')"
"""


def wait_for_rdma(manifest: list[dict], deadline: float) -> None:
    for vm in manifest:
        while True:
            probe = bounded(ssh_base(vm) + ["bash -s"], 90, input=_RDMA_PROBE)
            if probe.returncode == 0 and "NOTOOL" in probe.stdout:
                raise Failure(f"{vm['name']}: the guest image has no "
                              "ibv_devinfo. wait-for: rdma needs rdma-core "
                              "in the guest; use wait-for: ssh for an image "
                              "without it.")
            if probe.returncode == 0 and "DEV=" in probe.stdout:
                found = dict(line.split("=", 1) for line
                             in probe.stdout.splitlines() if "=" in line)
                vm["rdma_device"] = found.get("DEV") or None
                addresses = found.get("ADDRS", "").split()
                say(f"  {vm['name']}: {vm['rdma_device']} is PORT_ACTIVE")
                if vm["rdma_ip"] and vm["rdma_ip"] not in addresses:
                    warn(f"{vm['name']}: its RDMA NIC should carry "
                         f"{vm['rdma_ip']} and the guest has only "
                         f"{' '.join(addresses) or 'no IPv4 address'}")
                break
            if time.monotonic() > deadline:
                show(f"{vm['name']}: RDMA state", ssh_base(vm) + [
                    "uname -r; lsmod | grep -E '^ionic'; "
                    "lspci -nn | grep -i 1dd8; ibv_devinfo; nproc"])
                raise Failure(f"{vm['name']} has no RDMA device with an "
                              "active port. The emulated NIC's driver "
                              "(ionic_rdma) needs at least 4 vCPUs in the "
                              "guest, and a guest image that carries it.")
            time.sleep(5)


# ── The emulated GPU ─────────────────────────────────────────────────────────
#
# A booted guest has the GPU on its PCI bus and nothing more: amdgpu is
# blacklisted, and the one firmware file it needs to probe, ip_discovery.bin,
# is not in the guest image because it belongs to the rocjitsu build serving
# the device. qemu-tool's own `quick-start test` supplies both from an
# Ansible play that needs a qemu-minimal checkout. What follows is that
# play's firmware and probe steps (roles/rocjitsu_setup/tasks/firmware.yml in
# its "live" form, then vm-quickstart.yml), without the checkout.

_GPU_INSTALL = r"""
set -euo pipefail
stage=$(mktemp -d /tmp/qt-action-fw.XXXXXX)
trap 'rm -rf "$stage" /tmp/qt-action-fw.tar' EXIT
tar -xf /tmp/qt-action-fw.tar -C "$stage"
test -s "$stage/ip_discovery.bin"
base=/lib/firmware/amdgpu
upd=/lib/firmware/updates/amdgpu
sudo -n install -d -m 0755 "$base"
for f in "$stage"/*.bin; do
  n=$(basename "$f")
  if [ "$n" = ip_discovery.bin ] || [ "$fwset" != gap ]; then
    sudo -n install -o root -g root -m 0644 "$f" "$base/$n"
  elif [ ! -e "$upd/$n" ] && [ ! -e "$upd/$n.xz" ]; then
    sudo -n install -D -o root -g root -m 0644 "$f" "$upd/$n"
  fi
done
sudo -n /usr/local/bin/amdgpu-probe || echo "amdgpu-probe exited $?" >&2
for _ in $(seq 40); do
  gpus=$(cat /sys/class/kfd/kfd/topology/nodes/*/name 2>/dev/null | grep -c . || true)
  if [ "${gpus:-0}" -ge 1 ]; then
    echo "${gpus} GPU node(s) in the KFD topology"
    exit 0
  fi
  sleep 3
done
echo "No GPU in the KFD topology after 120 s: amdgpu did not bind one." >&2
echo "amdgpu-probe --status: $(/usr/local/bin/amdgpu-probe --status 2>&1 || true)" >&2
sudo -n dmesg | grep -i amdgpu | tail -n 40 >&2 || true
exit 1
"""


def gpu_status(vm: dict) -> str:
    """amdgpu-probe's one word, or "unknown" for a guest without the script."""
    # A wrapper from before --status takes the argument for a plain run and
    # would load amdgpu with no firmware staged.
    asked = bounded(ssh_base(vm) + [
        "grep -q -- --status /usr/local/bin/amdgpu-probe 2>/dev/null "
        "&& /usr/local/bin/amdgpu-probe --status || echo unknown"], 60)
    words = asked.stdout.split()
    return words[-1] if asked.returncode == 0 and words else "unknown"


# The blobs a guest's driver package ships. A guest that has them needs only
# the gap around them; one that does not needs the full set.
_FIRMWARE_SET = r"""
for n in gc_12_1_0_mec.bin gc_12_1_0_mec_1.bin gc_12_1_0_rlc.bin \
         gc_12_1_0_rlc_1.bin gc_12_1_0_uni_mes.bin sdma_7_1_0.bin; do
  [ -e "/lib/firmware/updates/amdgpu/$n" ] || { echo full; exit 0; }
done
echo gap
"""


def firmware_set(vm: dict) -> str:
    asked = bounded(ssh_base(vm) + ["bash -s"], 60, input=_FIRMWARE_SET)
    return "full" if asked.stdout.strip() == "full" else "gap"


# Limits on the GPU step's own commands; none is an input.
GPU_FIRMWARE_SECONDS = 600   # generating a firmware set, on the runner
GPU_COPY_SECONDS = 300       # copying it into one guest
GPU_INSTALL_SECONDS = 600    # installing it and the 120 s wait for the GPU


def generate_firmware(image: str, generation: str, fwset: str,
                      target: Path) -> None:
    """The firmware set of this stack's rocjitsu, as a tar on the runner."""
    script = ("python3 /usr/local/bin/vfio_guest_firmware.py "
              f"--generation {shlex.quote(generation)} --set {fwset} "
              "--output /out >&2 && tar -cf - -C /out .")
    say(f"generating {generation} firmware ({fwset}) with {image}")
    # Named, so that it can be removed: stopping the docker client, which is
    # all a timeout does, leaves the container running, and it carries no
    # compose label for the teardown to find it by.
    name = f"qta-firmware-{uuid.uuid4().hex[:12]}"
    command = ["docker", "run", "--rm", "--name", name, "--entrypoint",
               "/bin/sh", image, "-c", script]
    try:
        with open(target, "wb") as out:
            made = subprocess.run(command, stdout=out,
                                  timeout=GPU_FIRMWARE_SECONDS)
    except FileNotFoundError:
        raise Failure("docker is not on PATH; it generates the GPU "
                      "firmware") from None
    except subprocess.TimeoutExpired:
        bounded(["docker", "rm", "-f", name], 60)
        raise Failure("generating the GPU firmware from the rocjitsu image "
                      f"{image} did not finish within "
                      f"{GPU_FIRMWARE_SECONDS} s") from None
    if made.returncode != 0 or target.stat().st_size == 0:
        raise Failure("could not generate the GPU firmware from the "
                      f"rocjitsu image {image}")


def bring_up_gpu(vm: dict, firmware: Path, fwset: str) -> None:
    try:
        with open(firmware, "rb") as blob:
            sent = subprocess.run(
                ssh_base(vm) + ["cat > /tmp/qt-action-fw.tar"], stdin=blob,
                capture_output=True, timeout=GPU_COPY_SECONDS)
    except subprocess.TimeoutExpired:
        raise Failure(f"{vm['name']}: copying the GPU firmware in did not "
                      f"finish within {GPU_COPY_SECONDS} s") from None
    if sent.returncode != 0:
        raise Failure(f"{vm['name']}: could not copy the GPU firmware in")
    try:
        done = quiet(ssh_base(vm) + ["bash -s"],
                     input=f"fwset={fwset}\n{_GPU_INSTALL}",
                     timeout=GPU_INSTALL_SECONDS)
    except subprocess.TimeoutExpired:
        raise Failure(f"{vm['name']}: installing the GPU firmware and "
                      "binding the GPU did not finish within "
                      f"{GPU_INSTALL_SECONDS} s") from None
    say(f"  {vm['name']}: {(done.stdout.strip().splitlines() or ['no GPU'])[-1]}")
    if done.returncode != 0:
        say(done.stderr)
        raise Failure(f"{vm['name']}: amdgpu did not bind the emulated GPU")


def gpu_generation(state: dict) -> str:
    """The generation to make firmware for: the input, else the fleet's.

    A fleet names its GPU by rocjitsu's config file, e.g.
    gfx1250_mi455x.json; the generation is what precedes the underscore.
    """
    asked = inp("gpu-generation")
    if asked:
        return asked
    config = (state.get("fleet") or {}).get("rocjitsu_config") or ""
    stem = Path(config).name.split("_")[0]
    return stem if re.match(r"^gfx[0-9]+$", stem) else "gfx1250"


GPU_MODES = ("true", "required", "optional", "false")
# What amdgpu-probe --status means by each word, for a message.
GPU_STATES = {
    "absent": "no AMD GPU on its PCI bus",
    "nofirmware": "a GPU, and no firmware staged for it",
    "ready": "a GPU with firmware staged, and amdgpu not loaded",
    "unbound": "amdgpu loaded, and it bound no GPU",
    "stranded": "amdgpu loaded with parameters other than the emulation's",
    "unknown": "no amdgpu-probe in the guest image, or the guest did not "
               "answer",
}


def gpu_mode() -> str:
    """The gpu input: true, required, optional or false."""
    value = inp("gpu").lower()
    if value in ("required", "optional"):
        return value
    try:
        return "true" if flag("gpu", True) else "false"
    except Failure:
        raise Failure("input gpu must be true, false, required or optional, "
                      f"not {value!r}") from None


def gpu_shortfall(manifest: list[dict]) -> str | None:
    """With gpu: required, what is wrong; None when every GPU is bound."""
    short = [vm for vm in manifest if vm["gpu"] != "loaded"]
    if not short:
        return None
    return ("gpu: required, and " + "; ".join(
        f"{vm['name']} is {vm['gpu']} "
        f"({GPU_STATES.get(vm['gpu'], 'not a state this action knows')})"
        for vm in short) + ". Every guest has to end as loaded.")


def setup_gpus(state: dict, manifest: list[dict], mode: str = "true") -> None:
    """Make each guest's emulated GPU usable, where it has one.

    mode true: a guest with no GPU, or no amdgpu-probe, is left alone; one
    whose GPU cannot be bound fails the step, at the first such problem.
    required: every guest that can be brought up is, and the step then
    fails unless every guest ended as loaded, naming all that did not.
    optional: nothing here fails the step, whatever went wrong.
    Every guest's final status is in its manifest entry whichever it is,
    also when this raises.
    """
    problems: list[str] = []

    def problem(message: str) -> None:
        if mode == "true":
            raise Failure(message)
        problems.append(message)
        if mode == "optional":
            warn(f"gpu: optional, so not a failure: {message}")

    pending = []
    stranded = []
    for vm in manifest:
        vm["gpu"] = gpu_status(vm)
        if vm["gpu"] in ("nofirmware", "ready", "unbound"):
            pending.append(vm)
        elif vm["gpu"] == "stranded":
            stranded.append(vm)
        elif vm["gpu"] == "unknown":
            say(f"  {vm['name']}: no amdgpu-probe in this guest image, so "
                "its GPU is left as booted (gpu: false says so on purpose)")
        else:
            say(f"  {vm['name']}: GPU {vm['gpu']}, nothing to do")
    for vm in stranded:
        problem(f"{vm['name']}: amdgpu is loaded with parameters "
                "other than the emulation's; only a reboot of the "
                "guest clears that")
    if pending:
        shared, _hosts = read_inventory(state)
        image = shared.get("fleet_guest_rocjitsu_image")
        if not image:
            problem("the generated fleet-inventory.yml names no rocjitsu "
                    "image to take the GPU firmware from; set gpu: false "
                    "and stage it yourself")
            pending = []
    if pending:
        try:
            sets = {vm["name"]: firmware_set(vm) for vm in pending}
            tars = {}
            for fwset in sorted(set(sets.values())):
                tars[fwset] = (Path(state["state_file"]).parent
                               / f"gpu-firmware-{fwset}.tar")
                generate_firmware(image, gpu_generation(state), fwset,
                                  tars[fwset])
        except Failure as failure:
            problem(str(failure))
            pending = []
        except Exception as exc:  # noqa: BLE001 - optional means optional
            if mode != "optional":
                raise
            problem(f"{type(exc).__name__}: {exc}")
            pending = []
        failures: list[Exception] = []
        with concurrent.futures.ThreadPoolExecutor(len(pending) or 1) as pool:
            jobs = [(vm, pool.submit(bring_up_gpu, vm, tars[sets[vm["name"]]],
                                     sets[vm["name"]])) for vm in pending]
            for vm, job in jobs:
                try:
                    job.result()
                except Exception as exc:  # noqa: BLE001 - the others still count
                    failures.append(exc)
                vm["gpu"] = gpu_status(vm)
        for failure in failures:
            if mode == "true" or (mode == "required"
                                  and not isinstance(failure, Failure)):
                raise failure
            problem(str(failure) if isinstance(failure, Failure)
                    else f"{type(failure).__name__}: {failure}")
    if mode == "required":
        short = gpu_shortfall(manifest)
        if short:
            raise Failure(" ".join([short, *problems]))
        if problems:
            raise Failure(problems[0])


_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ALIAS_INDEX = "{index}"
DEFAULT_ALIASES = "vm{index}"


def parse_aliases(text: str) -> tuple[list[str], list[list[str]]]:
    """The ssh-aliases input: patterns for every guest, and names per guest.

    A word containing {index} names every guest, with the guest's index in
    its place. Any other word names one guest: the words of the first line
    that has such a word the first guest, of the next such line the second,
    and so on. Blank lines, comments and lines holding only {index} words
    are no guest's line.
    """
    patterns: list[str] = []
    per_guest: list[list[str]] = []
    rows = [line.strip() for line in text.splitlines()]
    rows = [row for row in rows if row and not row.startswith("#")]
    for row in rows:
        own: list[str] = []
        for word in row.replace(",", " ").split():
            probe = word.replace(_ALIAS_INDEX, "1")
            if not _ALIAS.match(probe):
                raise Failure(
                    f"ssh-aliases: {word!r} cannot be an alias. Use letters, "
                    "digits, '.', '_' and '-', starting with a letter or a "
                    "digit, and {index} for the guest's number; ssh reads "
                    "*, ? and ! in a Host line as patterns.")
            if _ALIAS_INDEX in word:
                if word not in patterns:
                    patterns.append(word)
            elif word in own or any(word in other for other in per_guest):
                raise Failure(f"ssh-aliases: {word!r} is given twice; an "
                              "alias names one guest")
            else:
                own.append(word)
        if own:
            per_guest.append(own)
    return patterns, per_guest


def guest_names(prefix: str | None, count: int) -> list[str]:
    """The names qemu-tool gives a fleet's guests: <vm-name-prefix>-<n>.

    Without a prefix, names no alias can be, for a check that only needs
    the guests told apart.
    """
    return [f"{prefix}-{index}" if prefix else f"guest {index}"
            for index in range(1, count + 1)]


def check_aliases(text: str, count: int | None = None,
                  prefix: str | None = None) -> None:
    """Refuse what can be refused of ssh-aliases before a fleet is booted.

    Without a count, the words alone. With the fleet's guest count, also a
    name for a guest it does not have and two guests sharing one alias;
    with its name prefix as well, an alias that is another guest's name.
    """
    _patterns, per_guest = parse_aliases(text or DEFAULT_ALIASES)
    if not count:
        return
    named = len(per_guest)
    if named > count:
        raise Failure(f"ssh-aliases names guest {named} "
                      f"({' '.join(per_guest[named - 1])}) and the fleet has "
                      f"{count}. A line of the input is one guest; write "
                      "{index} in a word to name every guest with it.")
    assign_aliases([{"name": name, "index": index} for index, name
                    in enumerate(guest_names(prefix, count), start=1)], text)


def assign_aliases(manifest: list[dict], text: str) -> None:
    """Set each guest's ssh_aliases: what the input asks for, then vm<n>.

    The guest's own name is always a Host name too and is not listed. vm<n>
    is kept beside whatever was asked for, unless the caller gave that very
    word to another guest.
    """
    patterns, per_guest = parse_aliases(text or DEFAULT_ALIASES)
    names = {vm["name"]: vm for vm in manifest}
    taken: dict[str, dict] = {}
    for position, vm in enumerate(manifest):
        wanted = list(per_guest[position]) if position < len(per_guest) else []
        wanted += [p.replace(_ALIAS_INDEX, str(vm["index"])) for p in patterns]
        aliases: list[str] = []
        for alias in wanted:
            if alias == vm["name"] or alias in aliases:
                continue
            owner = taken.get(alias) or names.get(alias)
            if owner is not None and owner is not vm:
                raise Failure(f"ssh-aliases: {alias!r} would name both "
                              f"{owner['name']} and {vm['name']}")
            taken[alias] = vm
            aliases.append(alias)
        vm["ssh_aliases"] = aliases
    for vm in manifest:
        legacy = f"vm{vm['index']}"
        if legacy not in taken and legacy not in names:
            taken[legacy] = vm
            vm["ssh_aliases"].append(legacy)
    for position, words in enumerate(per_guest):
        if words and position >= len(manifest):
            warn(f"ssh-aliases: {' '.join(words)} names guest {position + 1} "
                 f"and the fleet has {len(manifest)}")


def write_ssh_config(manifest: list[dict], path: Path) -> None:
    entries = []
    for vm in manifest:
        hosts = " ".join([vm["name"], *vm.get("ssh_aliases",
                                               [f"vm{vm['index']}"])])
        entries.append(
            f"Host {hosts}\n"
            f"  HostName {vm['ssh_host']}\n"
            f"  Port {vm['ssh_port']}\n"
            f"  User {vm['username']}\n"
            f"  IdentityFile {vm['ssh_key_file']}\n"
            "  IdentitiesOnly yes\n"
            "  StrictHostKeyChecking no\n"
            "  UserKnownHostsFile /dev/null\n"
            "  LogLevel ERROR\n"
            "  ServerAliveInterval 30\n"
            "  ServerAliveCountMax 6\n")
    path.write_text("\n".join(entries))
    path.chmod(0o600)


def write_inventory(manifest: list[dict], path: Path) -> None:
    out = ["all:", "  vars:",
           "    ansible_ssh_common_args: >-",
           "      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
           "  hosts:"]
    for vm in manifest:
        out += [f"    {vm['name']}:",
                f"      ansible_host: {vm['ssh_host']}",
                f"      ansible_port: {vm['ssh_port']}",
                f"      ansible_user: {vm['username']}",
                f"      ansible_ssh_private_key_file: {vm['ssh_key_file']}"]
        if vm["rdma_ip"]:
            out.append(f"      rdma_ip: {vm['rdma_ip']}")
        if vm["rdma_device"]:
            out.append(f"      rdma_device: {vm['rdma_device']}")
    path.write_text("\n".join(out) + "\n")


def select(manifest: list[dict], spec: str) -> list[dict]:
    spec = spec.strip() or "all"
    if spec == "all":
        return manifest
    if spec == "first":
        return manifest[:1]
    if spec == "last":
        return manifest[-1:]
    chosen: list[dict] = []
    for token in filter(None, re.split(r"[,\s]+", spec)):
        hit = [vm for vm in manifest
               if token in (vm["name"], str(vm["index"]))]
        if not hit:
            raise Failure(f"run-on: no guest {token!r}; there are "
                          f"{', '.join(vm['name'] for vm in manifest)}")
        chosen += [vm for vm in hit if vm not in chosen]
    if not chosen:
        raise Failure("run-on names no guest")
    return chosen


# ── Copying into the guests, running there, copying out ──────────────────────

def remote_path(path: str) -> str:
    """Quoted for the guest's shell; ~/ means its home, which is the cwd."""
    if path in ("~", "~/"):
        return "."
    if path.startswith("~/"):
        path = path[2:]
    return shlex.quote(path)


def parse_copy(entry: str) -> tuple[Path, str]:
    source, _, destination = entry.partition(":")
    path = Path(source).expanduser().resolve()
    if not path.exists():
        raise Failure(f"copy: {path} does not exist")
    if not destination:
        destination = "workspace" if path.is_dir() else "."
    return path, destination


def copy_into(vm: dict, source: Path, destination: str) -> None:
    if source.is_dir():
        pack = ["tar", "-C", str(source), "-cf", "-", "."]
    else:
        pack = ["tar", "-C", str(source.parent), "-cf", "-", source.name]
    where = remote_path(destination)
    unpack = ssh_base(vm) + [f"mkdir -p {where} && tar -C {where} -xf -"]
    packer = subprocess.Popen(pack, stdout=subprocess.PIPE)
    received = subprocess.run(unpack, stdin=packer.stdout,
                              capture_output=True, text=True, errors="replace")
    packer.stdout.close()  # type: ignore[union-attr]
    if packer.wait() != 0 or received.returncode != 0:
        raise Failure(f"copying {source} to {vm['name']}:{destination} "
                      f"failed: {received.stderr.strip()[-400:]}")


def copy_all(manifest: list[dict], entries: list[str]) -> str | None:
    """Copy every entry to every guest; return the first directory's target."""
    pairs = [parse_copy(entry) for entry in entries]
    for source, destination in pairs:
        say(f"copying {source} to {destination} on "
            f"{len(manifest)} guest(s)")
        with concurrent.futures.ThreadPoolExecutor(len(manifest)) as pool:
            for future in [pool.submit(copy_into, vm, source, destination)
                           for vm in manifest]:
                future.result()
    return next((dest for source, dest in pairs if source.is_dir()), None)


def parse_fetch(entry: str, default_dir: Path) -> tuple[str, Path]:
    path, _, local = entry.partition(":")
    if not path.strip():
        raise Failure(f"fetch: cannot read {entry!r}; write "
                      "`guest-path[:local-directory]`")
    return path.strip(), (Path(local.strip()).expanduser().resolve()
                          if local.strip() else default_dir)


def fetch_from(vm: dict, path: str, local: Path) -> bool:
    """Copy a file or directory out of one guest, under <local>/<guest>/."""
    where = remote_path(path)
    pack = ssh_base(vm) + [
        f"p={where}; [ -e \"$p\" ] || exit 44; "
        "tar -C \"$(dirname \"$p\")\" -cf - \"$(basename \"$p\")\""]
    target = local / vm["name"]
    target.mkdir(parents=True, exist_ok=True)
    # ssh's stderr goes to a file: a pipe nobody reads while tar runs would
    # fill, and a remote tar reporting many errors would then never finish.
    with tempfile.TemporaryFile() as errors:
        sender = subprocess.Popen(pack, stdout=subprocess.PIPE, stderr=errors)
        unpacked = subprocess.run(["tar", "-C", str(target), "-xf", "-"],
                                  stdin=sender.stdout, capture_output=True)
        sender.stdout.close()  # type: ignore[union-attr]
        code = sender.wait()
        errors.seek(0)
        stderr = errors.read().decode(errors="replace")
    if code == 44:
        warn(f"fetch: {vm['name']} has no {path}")
        return False
    if code != 0 or unpacked.returncode != 0:
        warn(f"fetch: copying {path} out of {vm['name']} failed: "
             f"{stderr.strip()[-300:]}")
        return False
    say(f"  {vm['name']}:{path} -> {target}")
    return True


def fetch_all(targets: list[dict], default_dir: Path) -> None:
    """Best effort: what a failed run left behind is what is wanted most."""
    for entry in lines_of("fetch"):
        path, local = parse_fetch(entry, default_dir)
        for vm in targets:
            try:
                fetch_from(vm, path, local)
            except OSError as exc:
                warn(f"fetch: {vm['name']}: {exc}")


def parse_env() -> dict[str, str]:
    env = {}
    for line in lines_of("env"):
        key, sep, value = line.partition("=")
        if not sep or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key.strip()):
            raise Failure(f"env: cannot read {line!r}; write KEY=VALUE")
        env[key.strip()] = value
    return env


def guest_env(vm: dict, manifest: list[dict]) -> dict[str, str]:
    env = {
        "QT_VM_NAME": vm["name"],
        "QT_VM_INDEX": str(vm["index"]),
        "QT_VM_COUNT": str(len(manifest)),
        "QT_VM_NAMES": " ".join(peer["name"] for peer in manifest),
        "QT_RDMA_IP": vm["rdma_ip"] or "",
        "QT_RDMA_DEVICE": vm["rdma_device"] or "",
        "QT_NIC_IPS": " ".join(vm.get("rdma_ips") or []),
        "QT_RDMA_IPS": " ".join(peer["rdma_ip"] or "" for peer in manifest),
        "QT_PEER_RDMA_IPS": " ".join(peer["rdma_ip"] or "" for peer in manifest
                                     if peer is not vm),
    }
    for peer in manifest:
        env[f"QT_VM_{peer['index']}_NAME"] = peer["name"]
        env[f"QT_VM_{peer['index']}_RDMA_IP"] = peer["rdma_ip"] or ""
    env.update(parse_env())
    return env


def guest_script(vm: dict, manifest: list[dict], script: str,
                 workdir: str | None) -> str:
    head = [f"export {key}={shlex.quote(value)}"
            for key, value in guest_env(vm, manifest).items()]
    if workdir:
        head.append(f"cd {remote_path(workdir)}")
    return "\n".join(head) + "\n" + script + "\n"


# The script goes to a file rather than to bash's stdin, so a command in it
# that reads stdin does not eat the rest of the script. A login shell, so
# the guest's profile applies.
_REMOTE_RUNNER = ('f=$(mktemp /tmp/qt-action.XXXXXX) && cat > "$f" && '
                  'bash -leo pipefail "$f" < /dev/null; rc=$?; '
                  'rm -f "$f"; exit $rc')


def run_script(manifest: list[dict], targets: list[dict], script: str,
               workdir: str | None) -> None:
    def one(vm: dict, capture: bool) -> subprocess.CompletedProcess:
        return subprocess.run(
            ssh_base(vm) + [_REMOTE_RUNNER],
            input=guest_script(vm, manifest, script, workdir), text=True,
            encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None)

    failed = []
    if len(targets) == 1:
        with group(f"run on {targets[0]['name']}"):
            if one(targets[0], False).returncode != 0:
                failed.append(targets[0]["name"])
    else:
        say(f"running on {len(targets)} guests in parallel; each guest's "
            "output is printed when all have finished")
        with concurrent.futures.ThreadPoolExecutor(len(targets)) as pool:
            jobs = [(vm, pool.submit(one, vm, True)) for vm in targets]
            for vm, job in jobs:
                try:
                    done = job.result()
                except Exception as exc:  # noqa: BLE001 - keep the others
                    warn(f"{vm['name']}: {exc}")
                    failed.append(vm["name"])
                    continue
                with group(f"run on {vm['name']} (exit {done.returncode})"):
                    sys.stdout.write(done.stdout or "")
                    sys.stdout.flush()
                if done.returncode != 0:
                    failed.append(vm["name"])
    if failed:
        raise Failure(f"the run script failed on {', '.join(failed)}")


def validate_guest_inputs() -> str:
    """Refuse what can be refused before a fleet is booted for it."""
    wait_for = inp("wait-for", "ssh").lower()
    if wait_for not in ("ssh", "rdma", "none"):
        raise Failure(f"wait-for must be ssh, rdma or none, not {wait_for!r}")
    whole_number("wait-timeout", "900")
    whole_number("stack-timeout", "0")
    whole_number("log-lines", "40")
    check_aliases(inp("ssh-aliases"))
    if inp("diagnostics"):
        flag("diagnostics")
    if gpu_mode() == "required" and wait_for == "none":
        raise Failure("gpu: required needs the guests reachable; that "
                      "cannot be combined with wait-for: none")
    for entry in lines_of("copy"):
        parse_copy(entry)
    for entry in lines_of("fetch"):
        parse_fetch(entry, Path("."))
    parse_env()
    wants_guest = [name for name in ("copy", "fetch") if lines_of(name)]
    if os.environ.get("INPUT_RUN", "").strip():
        wants_guest.append("run")
    if wants_guest and wait_for == "none":
        raise Failure(f"{' and '.join(wants_guest)} need the guests "
                      "reachable; that cannot be combined with wait-for: none")
    return wait_for


# ── State shared with later steps and with the post step ─────────────────────

def state_path(base: Path, stack: str) -> Path:
    return base / "action" / stack / "state.json"


def write_state(state: dict) -> None:
    path = Path(state["state_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


def load_state() -> dict:
    """The fleet an `exec` or `down` step means.

    The one its stack-dir or stack-name input names; with neither, the one
    the job's most recent `up` booted. A named fleet that is not found is an
    error, never a reason to act on another.
    """
    explicit = inp("stack-dir")
    ambient = os.environ.get("QEMU_TOOL_ACTION_STATE")
    stack_dir = Path(explicit).expanduser().resolve() if explicit else None
    named = stack_dir.name if stack_dir else inp("stack-name")
    if not named:
        if ambient and Path(ambient).is_file():
            return json.loads(Path(ambient).read_text())
        raise Failure("no fleet from an earlier `operation: up` step was "
                      "found in this job")
    roots = [DEFAULT_BASE,
             Path(os.environ.get("RUNNER_TEMP") or "/tmp") / "qemu-tool"]
    if ambient:
        roots.insert(0, Path(ambient).parent.parent.parent)
    if inp("work-dir"):
        roots.insert(0, Path(inp("work-dir")).expanduser().resolve())
    if stack_dir:
        roots.insert(0, stack_dir.parent.parent)
    for root in roots:
        candidate = root / "action" / named / "state.json"
        if not candidate.is_file():
            continue
        state = json.loads(candidate.read_text())
        if state.get("stack_name") != named:
            continue
        if stack_dir and Path(state.get("stack_dir", "")) != stack_dir:
            continue
        return state
    raise Failure(f"no fleet named {named!r} from an earlier `operation: up` "
                  "step was found in this job")


def publish(state: dict, manifest: list[dict]) -> None:
    action_dir = Path(state["state_file"]).parent
    ssh_config, inventory = action_dir / "ssh_config", action_dir / "inventory.yml"
    assign_aliases(manifest, inp("ssh-aliases"))
    write_ssh_config(manifest, ssh_config)
    write_inventory(manifest, inventory)
    state.update(manifest=manifest, ssh_config=str(ssh_config),
                 inventory=str(inventory))
    write_state(state)
    set_output("vm-manifest", json.dumps(manifest))
    set_output("vm-names", " ".join(vm["name"] for vm in manifest))
    set_output("vm-count", str(len(manifest)))
    set_output("ssh-config", str(ssh_config))
    set_output("inventory", str(inventory))
    export_env("QEMU_TOOL_SSH_CONFIG", str(ssh_config))


def summarise(state: dict, manifest: list[dict], seconds: float,
              gpu_failure: str = "") -> None:
    rows = "".join(
        f"| `{vm['name']}` | `{vm['ssh_host']}:{vm['ssh_port']}` | "
        f"`{vm['username']}` | {vm['gpu'] or ''} | "
        f"{vm['rdma_device'] or ''} | {vm['rdma_ip'] or ''} |\n"
        for vm in manifest)
    failed = (f"**The GPU step failed** (`gpu: {state.get('gpu_mode')}`): "
              f"{gpu_failure}\n\n" if gpu_failure else "")
    _append("GITHUB_STEP_SUMMARY",
            f"### qemu-tool fleet `{state['stack_name']}`\n\n"
            f"{len(manifest)} guest(s) up in {int(seconds)}s with qemu-tool "
            f"{state['version']}.\n\n"
            "| Guest | SSH | User | GPU | RDMA device | RDMA address |\n"
            "|---|---|---|---|---|---|\n" + rows + "\n" + failed)


# ── Operations ───────────────────────────────────────────────────────────────

def op_install(base: Path) -> dict[str, str]:
    if flag("install", True):
        check_install_inputs()
    # The host first: fetching qemu-tool-ref needs the git this may install.
    if flag("setup-host", True):
        with group("Prepare the host"):
            setup_host(base)
    with group("Install qemu-tool"):
        return install_qemu_tool(base)


# The flags of `quick-start up` that name a file or a directory.
_PATH_FLAGS = ("--xml", "--report-dir", "--repo-root", "--stack-dir",
               "--key-dir", "--images")


def absolute_paths(arguments: list[str], against: Path) -> list[str]:
    """extra-args, with relative paths made absolute against the workspace.

    quick-start is run from the action's own directory, and resolves a path
    against where it runs; the caller wrote theirs against the workspace.
    """
    out: list[str] = []
    expect_path = False
    for word in arguments:
        flag_name, eq, value = word.partition("=")
        if expect_path:
            out.append(str((against / word).resolve()))
            expect_path = False
        elif eq and flag_name in _PATH_FLAGS:
            out.append(f"{flag_name}={(against / value).resolve()}")
        else:
            out.append(word)
            expect_path = word in _PATH_FLAGS
    return out


def wait_and_publish(state: dict, manifest: list[dict], wait_for: str,
                     gpus: str, started: float) -> None:
    """Wait for the guests, bring up their GPUs, and say what came up.

    A failure of the GPU step is raised last, once the outputs, the state
    and the summary carry the status every guest's GPU was left in.
    """
    deadline = time.monotonic() + whole_number("wait-timeout", "900")
    gpu_failure: Exception | None = None
    if wait_for != "none":
        with group("Wait for the guests"):
            wait_for_ssh(state, manifest, deadline)
            if wait_for == "rdma":
                wait_for_rdma(manifest, deadline)
        if gpus != "false":
            with group("Bring up the emulated GPUs"):
                try:
                    setup_gpus(state, manifest, gpus)
                except Exception as failure:  # noqa: BLE001 - raised below
                    gpu_failure = failure
                for vm in manifest:
                    say(f"  {vm['name']}: GPU {vm['gpu']}")
    elif gpus != "false":
        say("wait-for: none, so the emulated GPUs are left unprobed")
    publish(state, manifest)
    summarise(state, manifest, time.monotonic() - started,
              str(gpu_failure or ""))
    for vm in manifest:
        say(f"{vm['name']}: {vm['ssh_cmd']}")
    if gpu_failure is not None:
        raise gpu_failure


def op_up(base: Path) -> None:
    started = time.monotonic()
    wait_for = validate_guest_inputs()
    gpus = gpu_mode()
    stack_limit = whole_number("stack-timeout", "0")
    stack_dir = Path(inp("stack-dir")
                     or base / "stacks" / inp("stack-name", "ci")
                     ).expanduser().resolve()
    stack = stack_dir.name
    if not STACK_NAME.match(stack):
        raise Failure(f"the stack's name {stack!r} must be lowercase "
                      "letters, digits, - and _ (it is the compose project "
                      "name)")
    elements = overlay_elements()
    containers = inp("container-qemu-tool", "runner")
    if containers not in ("runner", "pypi"):
        raise Failure("container-qemu-tool must be runner or pypi, not "
                      f"{containers!r}")
    extra = absolute_paths(shlex.split(inp("extra-args")), Path.cwd())
    tool = op_install(base)
    state = {
        "state_file": str(state_path(base, stack)),
        "stack_name": stack,
        "stack_dir": str(stack_dir),
        "key_dir": str(Path(inp("key-dir") or base / "keys" / stack)
                       .expanduser().resolve()),
        "images_dir": str(Path(inp("images-dir") or base / "images")
                          .expanduser().resolve()),
        "logs_dir": str(base / "action" / stack / "logs"),
        "fetch_dir": str(base / "action" / stack / "fetched"),
        "tool": tool["tool"],
        "version": tool["version"],
        "sha": tool["sha"],
        "flavor": inp("flavor", "default"),
        "fleet": {},
        "in_network": (in_container() if inp("in-network", "auto") == "auto"
                       else flag("in-network")),
        "layers": [],
        "manifest": [],
        "gpu_mode": gpus,
        "metrics": flag("metrics"),
        "diagnostics": flag("diagnostics"),
    }
    action_dir = Path(state["state_file"]).parent
    action_dir.mkdir(parents=True, exist_ok=True)
    Path(state["images_dir"]).mkdir(parents=True, exist_ok=True)
    with group("Fleet settings"):
        state["layers"], own = fleet_layers(tool["python"], state["flavor"],
                                            action_dir, elements)
        say("layers: " + " + ".join(state["layers"]))
        state["fleet"] = check_fleet(tool, state["layers"])
        check_aliases(inp("ssh-aliases"), state["fleet"].get("count"),
                      state["fleet"].get("name_prefix"))
        seeded = bool(inp("ssh-key-file"))
        if not state["fleet"].get("backing_ref"):
            if not seeded or flag("clean-overlays", True):
                raise Failure(
                    "the fleet names no <backing-ref>, the guest image to "
                    "pull. To boot guest images already in images-dir "
                    "instead, give ssh-key-file and set clean-overlays: "
                    "false.")
            say("no <backing-ref>: booting the guest images already in "
                f"{state['images_dir']}")
        seed_key(state, state["fleet"])
        # Sizes the caller gave, by input or in a file of their own, are
        # the caller's; the runner is only asked when nobody said.
        sizing = inp("size-from-host", "auto").lower()
        if sizing == "auto":
            host_sizing = not (sized_by_caller(elements)
                               or files_set_sizes(own))
        else:
            host_sizing = flag("size-from-host") and not sized_by_caller(elements)
        say("guest size: " + ("derived from this runner" if host_sizing
                              else "as the fleet XML has it"))
        placed, package = container_source(tool["python"], stack_dir)
        if containers == "pypi":
            shutil.rmtree(stack_dir / "qemu", ignore_errors=True)
            say("the stack's containers will install qemu-tool from PyPI")
        else:
            say(f"the stack's containers will run this qemu-tool ({placed})")
        found = checkout_above(package, action_dir)
        if found is not None and found != package.parent.parent:
            # quick-start will find it too and hand the containers its
            # qemu/ instead, whatever was installed or asked for.
            warn(f"work-dir, or the qemu-tool on PATH, is inside the "
                 f"qemu-minimal checkout at {found}. quick-start gives the "
                 f"stack's containers {found}/qemu, not the qemu-tool "
                 "installed. Keep work-dir outside the checkout.")
        # quick-start re-reads every overlay it ever generated here, and
        # only ever rewrites this one: an earlier fleet's login would
        # outlive it. It is written again when the image names an account.
        (stack_dir / "overlay-image-login.xml").unlink(missing_ok=True)
    # Recorded before anything starts, so the post step can clear up a stack
    # that got only part of the way.
    write_state(state)
    save_state("state_file", state["state_file"])
    export_env("QEMU_TOOL_ACTION_STATE", state["state_file"])
    export_env("QEMU_TOOL_STACK", state["stack_dir"])
    for key in ("stack-dir", "stack-name", "key-dir", "images-dir",
                "logs-dir", "fetch-dir"):
        set_output(key, state[key.replace("-", "_")])
    set_output("key-file", str(Path(state["key_dir"]) / "id_rsa"))
    set_output("fleet-xml", "\n".join(state["layers"]))

    command = [tool["tool"], "quick-start", "up", "--flavor", state["flavor"]]
    for layer in state["layers"]:
        command += ["--xml", layer]
    command += ["--stack-dir", state["stack_dir"], "--key-dir",
                state["key_dir"], "--images", state["images_dir"], "--yes"]
    if not flag("metrics"):
        command.append("--no-metrics")
    if not host_sizing:
        command.append("--no-size-from-host")
        # quick-start re-reads every overlay it ever generated in this
        # directory, whatever the flag says; an earlier host-sized run's
        # would outrank the sizes asked for now.
        (stack_dir / "overlay-sized.xml").unlink(missing_ok=True)
    if state["in_network"]:
        command.append("--in-network")
    if flag("clean-overlays", True):
        command.append("--clean-overlays")
    command += extra
    with group("Boot the fleet"):
        subprocess.run(["df", "-h", state["images_dir"]], check=False)
        # Not from the workspace: quick-start looks for a qemu-minimal
        # checkout above its working directory and, finding one, gives the
        # containers that tree in place of the one just placed for them.
        if stack_limit:
            run_within(command, stack_limit,
                       "`qemu-tool quick-start up` (the image pulls and the "
                       "start of the stack's containers; stack-timeout)",
                       cwd=action_dir)
        else:
            run(command, cwd=action_dir)
        if state["in_network"]:
            me = Path("/etc/hostname").read_text().strip()
            joined = quiet(["docker", "network", "connect",
                            f"{stack}_fleet", me])
            if joined.returncode != 0 and "already" not in joined.stderr:
                raise Failure("could not join the stack network: "
                              f"{joined.stderr.strip()}")
            state["joined"] = me
            write_state(state)

    manifest = build_manifest(state)
    # Not yet the manifest of a fleet that is up, which is what `exec` asks
    # for: who the guests are, for diagnostics of a fleet that never got
    # that far.
    state["guests"] = manifest
    write_state(state)
    # Again, on the names the guests really have and before the waits, so
    # that publish() cannot fail on an alias once the fleet is up.
    assign_aliases([dict(vm) for vm in manifest], inp("ssh-aliases"))
    wait_and_publish(state, manifest, wait_for, gpus, started)
    in_guests(state, manifest)


def in_guests(state: dict, manifest: list[dict]) -> None:
    """The copy, run and fetch inputs, for `up` and for `exec` alike."""
    targets = select(manifest, inp("run-on", "all"))
    workdir = inp("working-directory") or state.get("workdir")
    if lines_of("copy"):
        with group("Copy into the guests"):
            copied = copy_all(targets, lines_of("copy"))
        if copied and not inp("working-directory"):
            workdir = copied
            state["workdir"] = copied
            write_state(state)
    script = os.environ.get("INPUT_RUN", "")
    try:
        if script.strip():
            run_script(manifest, targets, script, workdir)
    finally:
        if lines_of("fetch"):
            with group("Copy out of the guests"):
                fetch_all(targets, Path(state["fetch_dir"]))


def op_exec() -> None:
    validate_guest_inputs()
    state = load_state()
    if not state.get("manifest"):
        raise Failure("the fleet of this job never finished coming up")
    if not (lines_of("copy") or lines_of("fetch")
            or os.environ.get("INPUT_RUN", "").strip()):
        raise Failure("operation: exec needs a `run` script, or `copy` or "
                      "`fetch` entries")
    set_output("fetch-dir", state.get("fetch_dir", ""))
    in_guests(state, state["manifest"])


def op_command(base: Path) -> None:
    arguments = shlex.split(inp("args"))
    if not arguments:
        raise Failure("operation: command needs `args`, the qemu-tool command "
                      "line without its first word, e.g. "
                      "`gen-compose --xml fleet.xml --output-dir out`")
    tool = op_install(base)["tool"]
    result = run([tool, *arguments], check=False)
    if result.returncode != 0:
        raise Failure(f"qemu-tool {arguments[0]} exited {result.returncode}")


def labelled(kind: list[str], stack: str) -> list[str]:
    return quiet(["docker", *kind, "--filter",
                  f"label=com.docker.compose.project={stack}"]).stdout.split()


def project_containers(stack: str) -> list[str]:
    return labelled(["ps", "-aq"], stack)


def collect_logs(state: dict, tail: int) -> None:
    logs = Path(state["logs_dir"])
    logs.mkdir(parents=True, exist_ok=True)
    listing = quiet(["docker", "ps", "-a", "--filter",
                     f"label=com.docker.compose.project={state['stack_name']}"])
    (logs / "docker-ps.txt").write_text(listing.stdout)
    for vm in state.get("manifest", []):
        got = bounded(ssh_base(vm) + ["sudo -n dmesg | tail -n 500"], 30)
        if got.returncode == 0:
            (logs / f"guest-{vm['name']}-dmesg.log").write_text(got.stdout)
    for container in project_containers(state["stack_name"]):
        try:
            name = quiet(["docker", "inspect", "-f", "{{.Name}}",
                          container]).stdout.strip().lstrip("/") or container
            text = quiet(["docker", "logs", "--timestamps", container])
            body = text.stdout + text.stderr
            (logs / f"{name}.log").write_text(body)
        except OSError as exc:
            warn(f"log of container {container}: {exc}")
            continue
        if tail > 0:
            with group(f"{name}: last {tail} lines"):
                say("\n".join(body.splitlines()[-tail:]))
    say(f"logs written to {logs}")


# ── Diagnostics (opt-in, at teardown) ────────────────────────────────────────
#
# Best effort throughout: every command has a limit of its own, a guest
# that does not answer costs one connection attempt, and nothing here can
# fail a step or keep the fleet from being torn down.

DIAG_BYTES = 4 * 1024 * 1024   # of each file as written, in bytes
DIAG_SECONDS = 45              # for each command
DIAG_GUEST_SECONDS = 120       # for one guest, after which the rest is skipped
# The drivers of what a fleet can put in a guest: the GPU, the RDMA NIC and
# its P4 engine, NVMe and NVMe over fabrics, SDXI, and rocm-xio.
DIAG_MODULES = ("amdgpu", "ionic", "ionic_rdma", "ernic_p4", "ib_core",
                "ib_uverbs", "rdma_cm", "nvme", "nvme_core", "nvme_fabrics",
                "nvme_rdma", "sdxi_char", "rocm_xio")

_MODULE_PARAMETERS = r"""for m in %s; do
  d=/sys/module/$m
  if [ ! -d "$d" ]; then echo "$m: not loaded"; continue; fi
  if [ ! -d "$d/parameters" ]; then echo "$m: loaded, no parameters"; continue; fi
  for p in "$d"/parameters/*; do
    [ -e "$p" ] || continue
    v=$(cat "$p" 2>/dev/null || sudo -n cat "$p" 2>/dev/null || echo '(unreadable)')
    printf '%%s/%%s=%%s\n' "$m" "${p##*/}" "$(printf '%%s' "$v" | tr '\n' ' ')"
  done
done""" % " ".join(DIAG_MODULES)

GUEST_DIAGNOSTICS = (
    ("dmesg.txt", "sudo -n dmesg || dmesg"),
    ("lspci.txt", "lspci -nnk"),
    ("lsblk.txt", "lsblk -o NAME,MAJ:MIN,SIZE,TYPE,TRAN,MODEL,SERIAL,"
                  "PHY-SEC,LOG-SEC,MOUNTPOINT || lsblk"),
    ("lsmod.txt", "lsmod"),
    ("module-parameters.txt", _MODULE_PARAMETERS),
)

# Redaction goes by NAMES, not by values: a value is taken out when the
# key, variable or flag it belongs to reads like a credential. A secret
# under a name that does not, or with no name at all, is not seen.
# `token`, not `tokenizer`: the settings of a model server are full of the
# second, and a diagnostics reader wants them.
_SECRET_WORD = (r"pass(?:word|wd|phrase)|secret|token(?!iz)|credential"
                r"|api[_-]?key|private[_-]?key")
_NAME = r"[A-Za-z0-9_.-]"
# What a name may end in, or be, to count: KEY, KEY_ID, PASS, AUTH.
_SECRET_TAIL = r"(?:key(?:[_-]?id)?|pass|auth)"
# A count or a limit is not a credential: MAX_TOKENS, --num-tokens.
_NOT_A_COUNT = r"(?!-{0,2}(?:max|min|num)[_.-])"
# A value as it stands in a command line. docker compose writes a string
# holding `: ` single-quoted with the quotes inside it doubled, so a quoted
# value arrives as ''like this''; that form has to be tried before the
# plain one, which would match its first two quotes and stop.
_SECRET_VALUE = r"\"[^\"]*\"|''[^']*''|'[^']*'"
# A whole name that is one: a word above anywhere in it, or the name being
# KEY, KEY_ID, PASS or AUTH or ending in one after a `_`, `.` or `-`
# (SMTP_PASS, not PGPASS), or being AUTHORIZATION or COOKIE.
_SECRET_NAME = (_NOT_A_COUNT
                + r"(?:" + _NAME + r"*(?:" + _SECRET_WORD + r")" + _NAME + r"*"
                r"|(?:" + _NAME + r"*[_.-])?" + _SECRET_TAIL
                + r"|authorization|cookie)")
# The same, for a flag: --db-password, --smtp-pass, --auth.
_SECRET_FLAG_NAME = (_NOT_A_COUNT + r"--?(?:[A-Za-z0-9-]*(?:" + _SECRET_WORD
                     + r")[A-Za-z0-9-]*|(?:[A-Za-z0-9-]*-)?" + _SECRET_TAIL
                     + r")")
# KEY: value, KEY=value and - KEY=value.
_SECRET_PAIR = re.compile(
    r"^(\s*(?:-\s+)?[\"']?" + _SECRET_NAME + r"[\"']?\s*[:=]\s*)(\S.*)$",
    re.IGNORECASE)
# A block scalar's header: the value is the more-indented lines after it.
_BLOCK_SCALAR = re.compile(r"^[|>][0-9+-]*\s*(?:#.*)?$")
# --some-password value and --some-password=value, inside a command line.
# Not from inside a longer flag: the `-tokens` of `--max-tokens` is not one.
_SECRET_FLAG = re.compile(
    r"(?<![A-Za-z0-9-])(" + _SECRET_FLAG_NAME + r"(?:=|\s+))"
    r"(" + _SECRET_VALUE + r"|(?:[^\s\"';&|]|[;&|](?=[^\s\"']))+)",
    re.IGNORECASE)
# An unquoted value stops at a shell separator that ends its word
# (`--token=x; next`), so the command line reads as it was; one inside the
# word (`--token=a;b`) is part of the value.
# name=value inside a longer value: a DSN, a query string, a backend's
# comma-separated options.
_SECRET_INLINE = re.compile(
    r"(?<![A-Za-z0-9_.-])(" + _NOT_A_COUNT + r"(?:" + _NAME + r"*(?:"
    + _SECRET_WORD + r")" + _NAME + r"*|(?:" + _NAME + r"*[_.-])?"
    + _SECRET_TAIL + r")=)(?!\[redacted\])(" + _SECRET_VALUE
    + r"|[^\s\"',;&]+)",
    re.IGNORECASE)
# A list item that is such a flag alone: its value is the next item.
_SECRET_ITEM = re.compile(
    r"^\s*-\s+[\"']?" + _SECRET_FLAG_NAME + r"[\"']?\s*$", re.IGNORECASE)
# An Authorization header's value, wherever it is.
_SECRET_BEARER = re.compile(
    r"\b(Bearer|Basic|Token)\s+[A-Za-z0-9._~+/=-]{8,}")
# A URL carrying user:password@, the user possibly empty (redis://:pw@host).
_SECRET_URL = re.compile(r"(://[^/\s:@]*:)[^@\s/]+@")
# A URL whose whole userinfo is one word (https://TOKEN@host/): a token as
# often as a user name, and nothing tells them apart, so it goes.
_SECRET_URL_USER = re.compile(r"(://)[^/\s:@\[\]]+@(?=[^/\s@])")
# A secret-named key with nothing after it: its value is the mapping or
# the list on the more-indented lines that follow.
_SECRET_PARENT = re.compile(
    r"^(\s*(?:-\s+)?[\"']?" + _SECRET_NAME + r"[\"']?\s*:)\s*(?:#.*)?$",
    re.IGNORECASE)
# Not at this column: in a rendered compose file that is where the names of
# services, networks and volumes are, and a service called `auth` is not a
# credential. The top-level `secrets:` is taken whole all the same.
_NAMES_COLUMN = 2
# Nor where a service refers to one by name: the keys directly under its
# `depends_on:` and `networks:`, which are at this column.
_NAME_REFS = re.compile(r"^ {4}(?:depends_on|networks):\s*(?:#.*)?$")


def redact(text: str) -> str:
    """A rendered compose file without the values named like credentials.

    By name only; see the note above _SECRET_WORD.
    """
    out = []
    next_item = False
    block: int | None = None   # column of the key whose block is being cut
    parent: int | None = None  # index in out of a key waiting for children
    refs: int | None = None    # column of the names a service refers to
    for line in text.splitlines():
        indent = len(line) - len(line.lstrip())
        if block is not None:
            # More indented, or a list at the key's own column, which YAML
            # allows for a key's value (`password:` then `- x` under the p).
            if (not line.strip() or indent > block or
                    (indent == block and re.match(r"-(?:\s|$)", line.strip()))):
                if parent is not None and line.strip():
                    out[parent] = (out[parent].split("#")[0].rstrip()
                                   + " [redacted]")
                    parent = None
                continue
            block = parent = None
        if refs is not None and line.strip():
            if indent <= 4:
                refs = None
            elif refs < 0:
                refs = indent
        if _NAME_REFS.match(line):
            refs = -1          # the first line under it says the column
        if next_item and re.match(r"^\s*-\s+\S", line):
            line = re.sub(r"^(\s*-\s+).*$", r"\1[redacted]", line)
            next_item = False
        else:
            next_item = bool(_SECRET_ITEM.match(line))
            pair = _SECRET_PAIR.match(line)
            if pair and _BLOCK_SCALAR.match(pair.group(2)):
                # Keep the header, so the file still parses, and replace
                # every line of the block with one.
                block = len(re.match(r"^\s*(?:-\s+)?", line).group(0))
                out.append(line)
                given = re.search(r"[1-9]", pair.group(2).split("#")[0])
                out.append(" " * (block + int(given.group(0) if given else 2))
                           + "[redacted]")
                continue
            nested = _SECRET_PARENT.match(line)
            column = len(re.match(r"^\s*(?:-\s+)?", line).group(0))
            referred = refs is not None and indent == refs == column
            if nested and column != _NAMES_COLUMN and not referred:
                block, parent = column, len(out)
                out.append(line)
                continue
            if not referred:
                line = _SECRET_PAIR.sub(r"\1[redacted]", line)
            line = _SECRET_FLAG.sub(r"\1[redacted]", line)
            line = _SECRET_INLINE.sub(r"\1[redacted]", line)
        line = _SECRET_BEARER.sub(r"\1 [redacted]", line)
        line = _SECRET_URL.sub(r"\1[redacted]@", line)
        out.append(_SECRET_URL_USER.sub(r"\1[redacted]@", line))
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def want_diagnostics(state: dict) -> bool:
    """This step's diagnostics input, else what the `up` step was given."""
    try:
        if inp("diagnostics"):
            return flag("diagnostics")
    except Failure as failure:
        warn(str(failure))
        return False
    return bool(state.get("diagnostics"))


def _last_bytes(text: str, limit: int) -> bytes:
    """The end of text as UTF-8, in at most limit bytes, on a character.

    In bytes, because that is what the file is: output that is not UTF-8
    is read with a three-byte replacement for each bad byte, so a count of
    characters let a file reach three times the limit.
    """
    data = text.encode("utf-8", "replace")[-limit:] if limit > 0 else b""
    skip = 0
    while skip < min(3, len(data)) and data[skip] & 0xC0 == 0x80:
        skip += 1
    return data[skip:]


def _first_bytes(text: str, limit: int) -> bytes:
    """The start of text as UTF-8, in at most limit bytes, on a character."""
    data = text.encode("utf-8", "replace")[:max(limit, 0)]
    return data.decode("utf-8", "ignore").encode("utf-8")


def _cleared(path: Path) -> None:
    """Remove what an earlier collection left at path, whatever it is."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.is_symlink() or path.exists():
        path.unlink()


def guest_diagnostics(vm: dict, target: Path) -> list[str]:
    """One guest's files, under its own directory. Returns what is missing.

    The directory is emptied first, so what it holds afterwards is what
    this call wrote: a guest that no longer answers is not left showing
    the files of an earlier fleet of the same name.

    Each command is stopped in the guest when its time is up, so what it
    had printed by then still arrives; the limit on this side is a backstop
    for a guest that stops answering altogether.
    """
    _cleared(target)
    if bounded(ssh_base(vm) + ["true"], 30).returncode != 0:
        return [f"{vm['name']}: did not answer SSH, nothing collected"]
    target.mkdir(parents=True, exist_ok=True)
    missing = []
    began = time.monotonic()
    for name, command in GUEST_DIAGNOSTICS:
        if time.monotonic() - began > DIAG_GUEST_SECONDS:
            missing.append(f"{vm['name']}: {name} skipped, the guest had "
                           f"used its {DIAG_GUEST_SECONDS} s")
            continue
        remote = "bash -o pipefail -c " + shlex.quote(
            f"timeout -k 5 {DIAG_SECONDS} bash -c {shlex.quote(command)} "
            f"2>&1 | tail -c {DIAG_BYTES}")
        got = bounded(ssh_base(vm) + [remote], DIAG_SECONDS + 20)
        body = _last_bytes(got.stdout or "", DIAG_BYTES)
        if body:
            (target / name).write_bytes(body)
        if got.returncode != 0:
            why = ("timed out" if got.returncode in (124, 137)
                   else f"exited {got.returncode}")
            missing.append(f"{vm['name']}: {name} {why}"
                           + (", what it had printed was kept" if body
                              and got.returncode in (124, 137)
                              else "" if body else ", nothing written"))
    return missing


def compose_config(state: dict, target: Path) -> str | None:
    """The stack as docker compose resolves it, with credentials taken out.

    Through `qemu-tool compose`, which is how the action drives the stack,
    so the interpolated names are answered the way they were at `up`.
    Returns why it could not be written, or None.
    """
    stack_dir = Path(state["stack_dir"])
    tool = state.get("tool") or shutil.which("qemu-tool")
    if not (stack_dir / "docker-compose.yml").is_file():
        return "compose-config.yml: the stack was never generated"
    if not tool or not Path(tool).is_file():
        return "compose-config.yml: no qemu-tool to render it with"
    command = [tool, "compose", "--stack-dir", stack_dir]
    if state.get("images_dir"):
        command += ["--images", state["images_dir"]]
    if state.get("metrics"):
        command += ["--profile", "metrics"]
    # quick-start starts the stack with the source mount on the stack itself.
    env = dict(os.environ, QEMU_TOOL_SRC=str(stack_dir))
    got = bounded(command + ["config"], 60, env=env)
    if got.returncode != 0 or not got.stdout.strip():
        return (f"compose-config.yml: `qemu-tool compose config` "
                f"{'timed out' if got.returncode == 124 else 'failed'}: "
                f"{got.stderr.strip()[-300:]}")
    # The start of it, unlike a guest's files: a cut YAML file reads from
    # its top.
    target.write_bytes(_first_bytes(redact(got.stdout), DIAG_BYTES))
    return None


def collect_diagnostics(state: dict) -> list[str]:
    """What the diagnostics input asks for. Returns what could not be had."""
    target = Path(state["logs_dir"]) / "diagnostics"
    target.mkdir(parents=True, exist_ok=True)
    # A work-dir that outlives the job, or a second `up` in one, finds an
    # earlier collection here. Only what this call writes is reported.
    config = target / "compose-config.yml"
    for stale in (config, target / "NOTES.txt"):
        _cleared(stale)
    missing: list[str] = []
    guests = state.get("manifest") or state.get("guests") or []
    if not guests:
        missing.append("guests: the fleet never got as far as having any")
    else:
        with concurrent.futures.ThreadPoolExecutor(len(guests)) as pool:
            jobs = [(vm, pool.submit(guest_diagnostics, vm,
                                     target / vm["name"])) for vm in guests]
            for vm, job in jobs:
                try:
                    missing += job.result()
                except Exception as exc:  # noqa: BLE001 - keep the others
                    missing.append(f"{vm['name']}: {type(exc).__name__}: {exc}")
    try:
        problem = compose_config(state, config)
    except Exception as exc:  # noqa: BLE001 - the guests' files stand
        problem = f"compose-config.yml: {type(exc).__name__}: {exc}"
    if problem:
        missing.append(problem)
    mine = [target / vm["name"] for vm in guests]
    files = sorted(str(path.relative_to(target)) for directory in mine
                   if directory.is_dir() for path in directory.rglob("*")
                   if path.is_file())
    if config.is_file():
        files.append(config.name)
    earlier = sorted(
        str(path.relative_to(target)) for path in target.rglob("*")
        if path.is_file() and path != config
        and not any(directory in path.parents for directory in mine))
    notes = (["Collected:"] + ([f"  {name}" for name in files] or ["  nothing"])
             + ["", "Not collected:"]
             + ([f"  {line}" for line in missing] or ["  nothing"]))
    if earlier:
        notes += ["", "Left by an earlier collection, not part of this one:"]
        notes += [f"  {name}" for name in earlier]
    (target / "NOTES.txt").write_text("\n".join(notes) + "\n")
    say(f"diagnostics written to {target}: {len(files)} file(s)")
    for line in missing:
        warn(f"diagnostics: {line}")
    _append("GITHUB_STEP_SUMMARY",
            f"### qemu-tool diagnostics of `{state['stack_name']}`\n\n"
            f"{len(files)} file(s) in `{target}`.\n\n"
            + ("Not collected:\n\n" + "".join(
                f"- {line}\n" for line in missing) if missing
               else "Nothing was missed.\n") + "\n")
    return missing


def teardown(state: dict) -> None:
    stack, stack_dir = state["stack_name"], Path(state["stack_dir"])
    if not project_containers(stack):
        say(f"stack {stack}: nothing running, nothing to tear down")
        return
    try:
        lines = inp("log-lines", "40")
        collect_logs(state, int(lines) if lines.isdigit() else 40)
    except Exception as exc:  # noqa: BLE001 - logs must never block teardown
        warn(f"collecting the logs of stack {stack} failed: {exc}")
    if want_diagnostics(state):
        try:
            with group(f"Diagnostics of stack {stack}"):
                collect_diagnostics(state)
        except Exception as exc:  # noqa: BLE001 - nor may these
            warn(f"collecting the diagnostics of stack {stack} failed: {exc}")
    with group(f"Tear down stack {stack}"):
        if state.get("joined"):
            quiet(["docker", "network", "disconnect", "-f",
                   f"{stack}_fleet", state["joined"]])
        tool = state.get("tool") or shutil.which("qemu-tool")
        if tool and Path(tool).is_file() and \
                (stack_dir / "docker-compose.yml").is_file():
            run([tool, "compose", "--stack-dir", stack_dir, "down",
                 "--volumes", "--remove-orphans"], check=False)
        left = project_containers(stack)
        if left:
            say(f"{len(left)} container(s) survived compose down; removing")
            run(["docker", "rm", "-f", *left], check=False)
        networks = labelled(["network", "ls", "-q"], stack)
        if networks:
            run(["docker", "network", "rm", *networks], check=False)
        volumes = labelled(["volume", "ls", "-q"], stack)
        if volumes:
            run(["docker", "volume", "rm", "-f", *volumes], check=False)
        left = project_containers(stack)
        if left:
            raise Failure(f"{len(left)} container(s) of stack {stack} could "
                          "not be removed")


def op_down() -> None:
    state = load_state()
    set_output("logs-dir", state["logs_dir"])
    teardown(state)


def main() -> None:
    if os.environ.get("RUNNER_OS", "Linux") != "Linux":
        raise Failure("qemu-tool-action needs a Linux runner")
    operation = inp("operation", "up")
    if operation not in OPERATIONS:
        raise Failure(f"operation must be one of {', '.join(OPERATIONS)}, "
                      f"not {operation!r}. The v1 operations gen-vm, run-vm, "
                      "compose and gen-compose are now `operation: command` "
                      "with `args`.")
    if operation == "exec":
        op_exec()
    elif operation == "down":
        op_down()
    else:
        base = base_dir(operation)
        {"up": op_up, "install": op_install,
         "command": op_command}[operation](base)


def post() -> None:
    """Runs at the end of the job, whatever became of it. Never fails it."""
    path = os.environ.get("STATE_state_file")
    if not path or not Path(path).is_file():
        return
    state = json.loads(Path(path).read_text())
    if inp("teardown", "always") == "never":
        say(f"teardown: never -- stack {state['stack_name']} is left up")
        return
    teardown(state)


def entry(phase: str) -> int:
    try:
        post() if phase == "post" else main()
        return 0
    except Failure as failure:
        if phase == "post":
            warn(f"teardown: {failure}")
            return 0
        print(f"::error::{command_text(failure)}", flush=True)
        return 1
    except Exception as exc:  # noqa: BLE001 - say what it was, then the trace
        print("::group::Traceback", flush=True)
        traceback.print_exc(file=sys.stdout)
        print("::endgroup::", flush=True)
        text = command_text(f"qemu-tool-action: {type(exc).__name__}: {exc}")
        if phase == "post":
            print(f"::warning::{text}", flush=True)
            return 0
        print(f"::error::{text}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(entry(sys.argv[1] if len(sys.argv) > 1 else "main"))
