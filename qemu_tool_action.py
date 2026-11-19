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
import subprocess
import sys
import tarfile
import time
import urllib.request
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

OPERATIONS = ("up", "install", "exec", "command", "down")
NS_FLEET = "http://qemu-tool/fleet/1.0"
DEFAULT_BASE = Path("/var/lib/qemu-tool")
MIN_ORAS = (1, 3, 4)
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "BatchMode=yes",
    "-o", "IdentitiesOnly=yes",
    "-o", "ConnectTimeout=10",
]
# What quick-start's preflight insists on, and the apt package carrying each.
APT_FOR_TOOL = {
    "qemu-img": "qemu-utils",
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


def say(message: str) -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print(f"::warning::{message}", flush=True)


class group:
    def __init__(self, title: str) -> None:
        self.title = title

    def __enter__(self) -> None:
        print(f"::group::{self.title}", flush=True)

    def __exit__(self, *_exc: object) -> None:
        print("::endgroup::", flush=True)


def run(cmd: list[str], check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    say(f"$ {shlex.join(str(c) for c in cmd)}")
    result = subprocess.run([str(c) for c in cmd], **kwargs)
    if check and result.returncode != 0:
        raise Failure(f"`{shlex.join(str(c) for c in cmd[:3])} ...` "
                      f"exited {result.returncode}")
    return result


def quiet(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], capture_output=True,
                          text=True, **kwargs)


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


def base_dir() -> Path:
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
        return path
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
    if in_container():
        warn(f"{DEFAULT_BASE} is not writable, using {fallback}. In a "
             "container job the sibling containers will not see that path: "
             f"bind-mount {DEFAULT_BASE} into the job container.")
    return fallback


# ── Installing qemu-tool and what it needs ───────────────────────────────────

def requirement() -> list[str]:
    source = inp("qemu-tool-source")
    if source:
        path = Path(source).expanduser().resolve()
        if not (path / "pyproject.toml").is_file():
            raise Failure(f"qemu-tool-source {path} has no pyproject.toml; "
                          "pass the qemu/ directory of a qemu-minimal checkout")
        return ["-e", str(path)]
    version = inp("qemu-tool-version", ">=2.1,<3")
    if version == "latest":
        return ["qemu-tool"]
    if version[0].isdigit():
        return [f"qemu-tool=={version}"]
    return [f"qemu-tool{version}"]


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


def install_qemu_tool(base: Path) -> dict[str, str]:
    if not flag("install", True):
        tool = shutil.which("qemu-tool")
        if tool is None:
            raise Failure("install is false and no qemu-tool is on PATH")
    else:
        if sys.version_info < (3, 10):
            raise Failure("qemu-tool needs Python 3.10 or newer; this runner's "
                          f"python3 is {platform.python_version()}")
        spec = requirement()
        venv = base / "action" / "venv"
        # One install per job: a later step of the same job reuses it.
        stamp = ":".join([os.environ.get("GITHUB_RUN_ID", ""),
                          os.environ.get("GITHUB_RUN_ATTEMPT", ""),
                          os.environ.get("GITHUB_JOB", ""), *spec])
        marker = venv / ".qemu-tool-action"
        if (os.environ.get("GITHUB_RUN_ID") and marker.is_file()
                and marker.read_text() == stamp
                and (venv / "bin" / "qemu-tool").is_file()):
            say("reusing the install an earlier step of this job made")
            bin_dir = venv / "bin"
        elif quiet([sys.executable, "-m", "venv", "--clear",
                    venv]).returncode == 0:
            run([venv / "bin" / "python", "-m", "pip", "install", "-q",
                 "--disable-pip-version-check", *spec])
            marker.write_text(stamp)
            bin_dir = venv / "bin"
        else:
            # No venv module (python3-venv missing). A user install still
            # lands ahead of an image's own copy once its bin is on PATH.
            say("python3 -m venv is unavailable; installing with pip --user")
            run([sys.executable, "-m", "pip", "install", "-q", "--user",
                 "--break-system-packages", *spec])
            bin_dir = Path.home() / ".local" / "bin"
        add_path(bin_dir)
        tool = str(bin_dir / "qemu-tool")
        if not Path(tool).is_file():
            raise Failure(f"the install left no {tool}")
    version = quiet([tool, "--version"]).stdout.strip().split()[-1:]
    version_text = version[0] if version else "unknown"
    say(f"qemu-tool {version_text} at {tool}")
    if version_text[0].isdigit() and int(version_text.split(".")[0]) < 2:
        raise Failure(f"qemu-tool {version_text} is a 1.x release; this "
                      "version of the action drives 2.x. Use "
                      "sbates130272/qemu-tool-action@v1 for 1.x.")
    set_output("qemu-tool-version", version_text)
    set_output("qemu-tool-path", tool)
    return {"tool": tool, "python": tool_python(tool), "version": version_text}


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
    archive = _fetch(f"{release}/{name}")
    sums = _fetch(f"{release}/oras_{version}_checksums.txt").decode()
    wanted = next((line.split()[0] for line in sums.splitlines()
                   if line.strip().endswith(name)), None)
    if wanted != hashlib.sha256(archive).hexdigest():
        raise Failure(f"{name} does not match its published sha256")
    bin_dir = base / "action" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    packed = bin_dir / name
    packed.write_bytes(archive)
    with tarfile.open(packed) as tar:
        member = tar.getmember("oras")
        source = tar.extractfile(member)
        if source is None:
            raise Failure(f"{name} carries no oras binary")
        (bin_dir / "oras").write_bytes(source.read())
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
            env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
            run(root + ["env", "DEBIAN_FRONTEND=noninteractive",
                        "apt-get", "update", "-q"], check=False, env=env)
            run(root + ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get",
                        "install", "-y", "-q", "--no-install-recommends",
                        *missing], env=env)
    setup_oras(base)
    if shutil.which("docker") is None:
        warn("docker is not on PATH; the stacks need Docker with the "
             "compose plugin")


# ── Fleet settings ───────────────────────────────────────────────────────────

_ELEMENT = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


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
    if "vm-vmem" in elements:
        # Guest RAM is a shared memfd and must fit the container's shm.
        elements["vm-shm-size"] = f"{int(elements['vm-vmem']) + 1024}m"
    for line in lines_of("fleet"):
        match = re.match(r"^([^:=\s]+)\s*[:=]\s*(.*)$", line)
        if not match or not _ELEMENT.match(match.group(1)):
            raise Failure(f"fleet: cannot read {line!r}; write one "
                          "`element: value` per line, e.g. `vm-nvme: 2`")
        elements[match.group(1)] = match.group(2).strip()
    return elements


def render_overlay(elements: dict[str, str]) -> str:
    body = "".join(f"  <{name}>{escape(value)}</{name}>\n"
                   for name, value in elements.items())
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            "<!-- Written by qemu-tool-action from its inputs. -->\n"
            f'<fleet xmlns="{NS_FLEET}">\n{body}</fleet>\n')


def packaged_fleet(python: str, flavor: str) -> Path:
    code = ("import sys\n"
            "from qemu_tool.quick_start import VARIANTS\n"
            "from qemu_tool.resources import STACKS, shipped_path\n"
            "print(shipped_path('stacks', STACKS, VARIANTS[sys.argv[1]][0]))\n")
    found = quiet([python, "-c", code, flavor])
    path = Path(found.stdout.strip() or "/nonexistent")
    if found.returncode != 0 or not path.is_file():
        raise Failure(f"cannot find the fleet XML of flavor {flavor!r} in "
                      f"this qemu-tool install: {found.stderr.strip()[-300:]}")
    return path


def fleet_layers(python: str, flavor: str, action_dir: Path) -> list[str]:
    layers = []
    for entry in lines_of("xml"):
        path = Path(entry).expanduser().resolve()
        if not path.is_file():
            raise Failure(f"xml: {path} does not exist")
        layers.append(str(path))
    elements = overlay_elements()
    if not elements:
        return layers
    if not layers:
        # quick-start replaces the flavor's file with any --xml it is given,
        # so an overlay alone would be a fleet with nothing under it.
        layers.append(str(packaged_fleet(python, flavor)))
    overlay = action_dir / "overlay.xml"
    overlay.write_text(render_overlay(elements))
    say(f"wrote {overlay}:")
    say(overlay.read_text())
    layers.append(str(overlay))
    return layers


# ── The guests ───────────────────────────────────────────────────────────────

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
            if key in ("ansible_host", "ansible_user") and value:
                shared[key] = value
            continue
        if indent == hosts_indent + 2:
            current = {"name": key, "nic_ips": []}
            hosts.append(current)
        elif current is not None:
            if key == "ip" and value:
                current["nic_ips"].append(value)
            elif value:
                current.setdefault(key, value)
    return shared, hosts


def image_username(key_dir: Path) -> str | None:
    try:
        return (json.loads((key_dir / "vm-info.json").read_text())
                .get("username") or "").strip() or None
    except (OSError, ValueError):
        return None


def container_ip(container: str) -> str | None:
    found = quiet(["docker", "inspect", "-f",
                   "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
                   container])
    addresses = found.stdout.split()
    return addresses[0] if found.returncode == 0 and addresses else None


def build_manifest(state: dict) -> list[dict]:
    stack_dir, key_dir = Path(state["stack_dir"]), Path(state["key_dir"])
    inventory = stack_dir / "fleet-inventory.yml"
    if not inventory.is_file():
        raise Failure(f"{inventory} was not generated; the stack did not "
                      "come up")
    shared, hosts = parse_inventory(inventory.read_text())
    if not hosts:
        raise Failure(f"{inventory} names no guests")
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
        ips = host["nic_ips"] or ([host["fleet_guest_ip"]]
                                  if host.get("fleet_guest_ip") else [])
        manifest.append({
            "name": host["name"],
            "index": index,
            "ssh_host": address,
            "ssh_port": port,
            "username": username,
            "ssh_key_file": str(key),
            "ssh_cmd": f"ssh -i {key} -p {port} {username}@{address}",
            "rdma_ip": ips[0] if ips else None,
            "rdma_ips": ips,
            "rdma_device": None,
        })
    return manifest


def ssh_base(vm: dict) -> list[str]:
    return ["ssh", *SSH_OPTS, "-i", vm["ssh_key_file"], "-p",
            str(vm["ssh_port"]), f"{vm['username']}@{vm['ssh_host']}"]


def wait_for_ssh(state: dict, manifest: list[dict], deadline: float) -> None:
    for vm in manifest:
        start = time.monotonic()
        while True:
            if quiet(ssh_base(vm) + ["true"]).returncode == 0:
                say(f"  {vm['name']}: answering SSH at "
                    f"{vm['ssh_host']}:{vm['ssh_port']} "
                    f"({int(time.monotonic() - start)}s)")
                break
            if time.monotonic() > deadline:
                with group(f"{vm['name']}: last SSH attempt and container log"):
                    subprocess.run(ssh_base(vm)[:1] + ["-v"] + ssh_base(vm)[1:]
                                   + ["true"], check=False)
                    subprocess.run(
                        ["docker", "logs", "--tail", "80",
                         f"{state['stack_name']}-qemu-{vm['index']}-1"],
                        check=False)
                raise Failure(f"{vm['name']} never answered SSH at "
                              f"{vm['ssh_host']}:{vm['ssh_port']} as "
                              f"{vm['username']}")
            time.sleep(5)


_RDMA_PROBE = r"""
dev=$(ibv_devinfo -l 2>/dev/null | sed -n '2p' | tr -d ' \t\r')
[ -n "$dev" ] || { sudo -n modprobe ionic_rdma 2>/dev/null; exit 1; }
ibv_devinfo -d "$dev" 2>/dev/null | grep -q PORT_ACTIVE || exit 1
echo "DEV=$dev"
echo "ADDRS=$(ip -4 -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | tr '\n' ' ')"
"""


def wait_for_rdma(manifest: list[dict], deadline: float) -> None:
    for vm in manifest:
        while True:
            probe = quiet(ssh_base(vm) + ["bash -s"], input=_RDMA_PROBE)
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
                with group(f"{vm['name']}: RDMA state"):
                    subprocess.run(ssh_base(vm) + [
                        "uname -r; lsmod | grep -E '^ionic'; "
                        "lspci -nn | grep -i 1dd8; ibv_devinfo; nproc"],
                        check=False)
                raise Failure(f"{vm['name']} has no RDMA device with an "
                              "active port. ionic_rdma needs at least 4 "
                              "vCPUs in the guest.")
            time.sleep(5)


def write_ssh_config(manifest: list[dict], path: Path) -> None:
    entries = []
    for vm in manifest:
        entries.append(
            f"Host {vm['name']} vm{vm['index']}\n"
            f"  HostName {vm['ssh_host']}\n"
            f"  Port {vm['ssh_port']}\n"
            f"  User {vm['username']}\n"
            f"  IdentityFile {vm['ssh_key_file']}\n"
            "  IdentitiesOnly yes\n"
            "  StrictHostKeyChecking no\n"
            "  UserKnownHostsFile /dev/null\n"
            "  LogLevel ERROR\n")
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
    chosen = []
    for token in re.split(r"[,\s]+", spec):
        hit = [vm for vm in manifest
               if token in (vm["name"], str(vm["index"]))]
        if not hit:
            raise Failure(f"run-on: no guest {token!r}; there are "
                          f"{', '.join(vm['name'] for vm in manifest)}")
        chosen += [vm for vm in hit if vm not in chosen]
    return chosen


# ── Copying into the guests and running there ────────────────────────────────

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
                              capture_output=True, text=True)
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


def guest_env(vm: dict, manifest: list[dict]) -> dict[str, str]:
    env = {
        "QT_VM_NAME": vm["name"],
        "QT_VM_INDEX": str(vm["index"]),
        "QT_VM_COUNT": str(len(manifest)),
        "QT_VM_NAMES": " ".join(peer["name"] for peer in manifest),
        "QT_RDMA_IP": vm["rdma_ip"] or "",
        "QT_RDMA_DEVICE": vm["rdma_device"] or "",
        "QT_RDMA_IPS": " ".join(peer["rdma_ip"] or "" for peer in manifest),
        "QT_PEER_RDMA_IPS": " ".join(peer["rdma_ip"] or "" for peer in manifest
                                     if peer is not vm),
    }
    for peer in manifest:
        env[f"QT_VM_{peer['index']}_NAME"] = peer["name"]
        env[f"QT_VM_{peer['index']}_RDMA_IP"] = peer["rdma_ip"] or ""
    for line in lines_of("env"):
        key, sep, value = line.partition("=")
        if not sep or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key.strip()):
            raise Failure(f"env: cannot read {line!r}; write KEY=VALUE")
        env[key.strip()] = value
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
# /etc/profile.d (where the guest image puts ROCm on PATH) applies.
_REMOTE_RUNNER = ('f=$(mktemp /tmp/qt-action.XXXXXX) && cat > "$f" && '
                  'bash -leo pipefail "$f" < /dev/null; rc=$?; '
                  'rm -f "$f"; exit $rc')


def run_script(manifest: list[dict], targets: list[dict], script: str,
               workdir: str | None) -> None:
    def one(vm: dict, capture: bool) -> subprocess.CompletedProcess:
        return subprocess.run(
            ssh_base(vm) + [_REMOTE_RUNNER],
            input=guest_script(vm, manifest, script, workdir), text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None)

    failed = []
    if len(targets) == 1:
        with group(f"run on {targets[0]['name']}"):
            if one(targets[0], False).returncode != 0:
                failed.append(targets[0]["name"])
    else:
        say(f"running on {len(targets)} guests in parallel")
        with concurrent.futures.ThreadPoolExecutor(len(targets)) as pool:
            jobs = [(vm, pool.submit(one, vm, True)) for vm in targets]
            for vm, job in jobs:
                done = job.result()
                with group(f"run on {vm['name']} (exit {done.returncode})"):
                    sys.stdout.write(done.stdout or "")
                    sys.stdout.flush()
                if done.returncode != 0:
                    failed.append(vm["name"])
    if failed:
        raise Failure(f"the run script failed on {', '.join(failed)}")


# ── State shared with later steps and with the post step ─────────────────────

def state_path(base: Path, stack: str) -> Path:
    return base / "action" / stack / "state.json"


def write_state(state: dict) -> None:
    path = Path(state["state_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


def load_state() -> dict:
    explicit = inp("stack-dir")
    candidates = []
    if explicit:
        stack_dir = Path(explicit).expanduser().resolve()
        candidates += [root / "action" / stack_dir.name / "state.json"
                       for root in (stack_dir.parent.parent, DEFAULT_BASE)]
    if os.environ.get("QEMU_TOOL_ACTION_STATE"):
        candidates.append(Path(os.environ["QEMU_TOOL_ACTION_STATE"]))
    for candidate in candidates:
        if candidate.is_file():
            return json.loads(candidate.read_text())
    raise Failure("no fleet from an earlier `operation: up` step was found "
                  "in this job")


def publish(state: dict, manifest: list[dict]) -> None:
    action_dir = Path(state["state_file"]).parent
    ssh_config, inventory = action_dir / "ssh_config", action_dir / "inventory.yml"
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


def summarise(state: dict, manifest: list[dict], seconds: float) -> None:
    rows = "".join(
        f"| `{vm['name']}` | `{vm['ssh_host']}:{vm['ssh_port']}` | "
        f"`{vm['username']}` | {vm['rdma_device'] or ''} | "
        f"{vm['rdma_ip'] or ''} |\n" for vm in manifest)
    _append("GITHUB_STEP_SUMMARY",
            f"### qemu-tool fleet `{state['stack_name']}`\n\n"
            f"{len(manifest)} guest(s) up in {int(seconds)}s with qemu-tool "
            f"{state['version']}.\n\n"
            "| Guest | SSH | User | RDMA device | RDMA address |\n"
            "|---|---|---|---|---|\n" + rows + "\n")


# ── Operations ───────────────────────────────────────────────────────────────

def op_install(base: Path) -> dict[str, str]:
    with group("Install qemu-tool"):
        tool = install_qemu_tool(base)
    if flag("setup-host", True):
        with group("Prepare the host"):
            setup_host(base)
    return tool


def op_up(base: Path) -> None:
    started = time.monotonic()
    tool = op_install(base)
    stack = inp("stack-name", "ci")
    if not re.match(r"^[a-z0-9][a-z0-9_-]*$", stack):
        raise Failure(f"stack-name {stack!r} must be lowercase letters, "
                      "digits, - and _ (it is the compose project name)")
    stack_dir = Path(inp("stack-dir") or base / "stacks" / stack).expanduser().resolve()
    stack = stack_dir.name
    state = {
        "state_file": str(state_path(base, stack)),
        "stack_name": stack,
        "stack_dir": str(stack_dir),
        "key_dir": str(Path(inp("key-dir") or base / "keys" / stack)
                       .expanduser().resolve()),
        "images_dir": str(Path(inp("images-dir") or base / "images")
                          .expanduser().resolve()),
        "logs_dir": str(base / "action" / stack / "logs"),
        "tool": tool["tool"],
        "version": tool["version"],
        "flavor": inp("flavor", "default"),
        "in_network": (in_container() if inp("in-network", "auto") == "auto"
                       else flag("in-network")),
        "layers": [],
        "manifest": [],
    }
    action_dir = Path(state["state_file"]).parent
    action_dir.mkdir(parents=True, exist_ok=True)
    Path(state["images_dir"]).mkdir(parents=True, exist_ok=True)
    with group("Fleet settings"):
        state["layers"] = fleet_layers(tool["python"], state["flavor"],
                                       action_dir)
        say("layers: " + (" + ".join(state["layers"])
                          or f"the {state['flavor']} flavor as shipped"))
    # Recorded before anything starts, so the post step can clear up a stack
    # that got only part of the way.
    write_state(state)
    save_state("state_file", state["state_file"])
    export_env("QEMU_TOOL_ACTION_STATE", state["state_file"])
    export_env("QEMU_TOOL_STACK", state["stack_dir"])
    for key in ("stack-dir", "stack-name", "key-dir", "images-dir", "logs-dir"):
        set_output(key, state[key.replace("-", "_")])
    set_output("key-file", str(Path(state["key_dir"]) / "id_rsa"))

    command = [tool["tool"], "quick-start", "up", "--flavor", state["flavor"]]
    for layer in state["layers"]:
        command += ["--xml", layer]
    command += ["--stack-dir", state["stack_dir"], "--key-dir",
                state["key_dir"], "--images", state["images_dir"], "--yes"]
    if not flag("metrics"):
        command.append("--no-metrics")
    if inp("vcpus") or inp("vmem") or not flag("size-from-host", True):
        command.append("--no-size-from-host")
    if state["in_network"]:
        command.append("--in-network")
    if flag("clean-overlays", True):
        command.append("--clean-overlays")
    command += shlex.split(inp("extra-args"))
    with group("Boot the fleet"):
        subprocess.run(["df", "-h", state["images_dir"]], check=False)
        run(command)
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
    wait_for = inp("wait-for", "ssh")
    if wait_for not in ("ssh", "rdma", "none"):
        raise Failure(f"wait-for must be ssh, rdma or none, not {wait_for!r}")
    deadline = time.monotonic() + int(inp("wait-timeout", "900"))
    if wait_for != "none":
        with group("Wait for the guests"):
            wait_for_ssh(state, manifest, deadline)
            if wait_for == "rdma":
                wait_for_rdma(manifest, deadline)
    publish(state, manifest)
    summarise(state, manifest, time.monotonic() - started)
    for vm in manifest:
        say(f"{vm['name']}: {vm['ssh_cmd']}")
    after_up(state, manifest)


def after_up(state: dict, manifest: list[dict]) -> None:
    """The copy and run inputs, for `up` and for `exec` alike."""
    targets = select(manifest, inp("run-on", "all"))
    workdir = inp("working-directory") or state.get("workdir")
    if lines_of("copy"):
        if inp("wait-for", "ssh") == "none":
            raise Failure("copy needs the guests reachable; it cannot be "
                          "combined with wait-for: none")
        with group("Copy into the guests"):
            copied = copy_all(targets, lines_of("copy"))
        if copied and not inp("working-directory"):
            workdir = copied
            state["workdir"] = copied
            write_state(state)
    script = os.environ.get("INPUT_RUN", "")
    if script.strip():
        run_script(manifest, targets, script, workdir)


def op_exec() -> None:
    state = load_state()
    if not state.get("manifest"):
        raise Failure("the fleet of this job never finished coming up")
    if not lines_of("copy") and not os.environ.get("INPUT_RUN", "").strip():
        raise Failure("operation: exec needs a `run` script or `copy` entries")
    after_up(state, state["manifest"])


def op_command(base: Path) -> None:
    arguments = shlex.split(inp("args"))
    if not arguments:
        raise Failure("operation: command needs `args`, the qemu-tool command "
                      "line without its first word, e.g. "
                      "`gen-compose --xml fleet.xml --check`")
    tool = shutil.which("qemu-tool") if not flag("install", True) else None
    if tool is None:
        tool = op_install(base)["tool"]
    result = run([tool, *arguments], check=False)
    if result.returncode != 0:
        raise Failure(f"qemu-tool {arguments[0]} exited {result.returncode}")


def project_containers(stack: str, running_only: bool = False) -> list[str]:
    command = ["docker", "ps", "-q" if running_only else "-aq", "--filter",
               f"label=com.docker.compose.project={stack}"]
    return quiet(command).stdout.split()


def collect_logs(state: dict, tail: int) -> None:
    logs = Path(state["logs_dir"])
    logs.mkdir(parents=True, exist_ok=True)
    listing = quiet(["docker", "ps", "-a", "--filter",
                     f"label=com.docker.compose.project={state['stack_name']}"])
    (logs / "docker-ps.txt").write_text(listing.stdout)
    for vm in state.get("manifest", []):
        try:
            got = quiet(ssh_base(vm) + ["sudo -n dmesg | tail -n 500"],
                        timeout=30)
        except subprocess.TimeoutExpired:
            continue
        if got.returncode == 0:
            (logs / f"guest-{vm['name']}-dmesg.log").write_text(got.stdout)
    for container in project_containers(state["stack_name"]):
        name = quiet(["docker", "inspect", "-f", "{{.Name}}",
                      container]).stdout.strip().lstrip("/") or container
        text = quiet(["docker", "logs", "--timestamps", container])
        body = text.stdout + text.stderr
        (logs / f"{name}.log").write_text(body)
        if tail > 0:
            with group(f"{name}: last {tail} lines"):
                say("\n".join(body.splitlines()[-tail:]))
    say(f"logs written to {logs}")


def teardown(state: dict) -> None:
    stack, stack_dir = state["stack_name"], Path(state["stack_dir"])
    if not project_containers(stack):
        say(f"stack {stack}: nothing running, nothing to tear down")
        return
    with group(f"Collect logs of stack {stack}"):
        collect_logs(state, int(inp("log-lines", "40")))
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
            networks = quiet(["docker", "network", "ls", "-q", "--filter",
                              f"label=com.docker.compose.project={stack}"])
            if networks.stdout.split():
                run(["docker", "network", "rm", *networks.stdout.split()],
                    check=False)
        left = project_containers(stack)
        if left:
            raise Failure(f"{len(left)} container(s) of stack {stack} could "
                          "not be removed")


def op_down() -> None:
    state = load_state()
    set_output("logs-dir", state["logs_dir"])
    teardown(state)


def main() -> None:
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
        base = base_dir()
        {"up": op_up, "install": op_install, "command": op_command}[operation](base)


def post() -> None:
    """Runs at the end of the job, whatever became of it. Never fails it."""
    path = os.environ.get("STATE_state_file")
    if not path or not Path(path).is_file():
        return
    state = json.loads(Path(path).read_text())
    try:
        if inp("teardown", "always") == "never":
            say(f"teardown: never -- stack {state['stack_name']} is left up")
            return
        teardown(state)
    except Exception as exc:  # noqa: BLE001 - a post step must not mask the job
        warn(f"teardown of stack {state.get('stack_name')}: {exc}")


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "main"
    try:
        post() if phase == "post" else main()
    except Failure as failure:
        print(f"::error::{failure}", flush=True)
        sys.exit(1)
