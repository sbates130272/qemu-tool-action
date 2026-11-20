#!/usr/bin/env python3
"""Render every fleet the installed qemu-tool ships, the action's way.

    render_flavors.py QEMU_TOOL TOOL_PYTHON SCRATCH_DIR [FLEET_XML ...]

For each flavor: layer the action's own overlay on the shipped fleet, have
qemu-tool check and render it, and read the generated inventory back with
the action's parser. Extra fleet XML files (the repository's examples) get
the same treatment. Nothing is pulled or started.

This is what catches a fleet XML change upstream: a renamed element the
overlay writes, an inventory key that moved, a flavor the lookup misses.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import qemu_tool_action as qta  # noqa: E402


def render(tool: dict, name: str, layers: list[str], out: Path) -> None:
    fleet = qta.check_fleet(tool, layers)
    assert fleet.get("count"), (name, fleet)
    command = [tool["tool"], "gen-compose", "--output-dir", str(out)]
    for layer in layers:
        command += ["--xml", layer]
    done = subprocess.run(command, capture_output=True, text=True)
    assert done.returncode == 0, (name, done.stderr[-800:])
    state = {"stack_dir": str(out), "key_dir": str(out),
             "stack_name": out.name, "in_network": False, "fleet": fleet}
    manifest = qta.build_manifest(state)
    shared, _hosts = qta.read_inventory(state)
    assert len(manifest) == fleet["count"], (name, len(manifest), fleet)
    assert shared.get("fleet_guest_rocjitsu_image"), (name, shared)
    for vm in manifest:
        assert vm["name"] and vm["ssh_port"] and vm["rdma_ip"], (name, vm)
        assert vm["rdma_ip"] in vm["rdma_ips"], (name, vm)
        assert all(nic.get("ip") for nic in vm["nics"]), (name, vm)
    nics = sum(len(vm["nics"]) for vm in manifest)
    print(f"  {name}: {len(manifest)} guest(s), {nics} NIC(s), "
          f"GPU generation {qta.gpu_generation(state)}")


def main() -> None:
    tool = {"tool": sys.argv[1], "python": sys.argv[2], "version": "under test"}
    scratch = Path(sys.argv[3])
    scratch.mkdir(parents=True, exist_ok=True)
    overlay = scratch / "overlay.xml"
    overlay.write_text(qta.render_overlay(
        {"vm-vcpus": "4", "vm-vmem": "4096", "vm-shm-size": "5120m"}))
    fleets = qta.shipped_fleets(tool["python"])
    assert "default" in fleets, fleets
    print(f"flavors: {', '.join(sorted(fleets))}")
    for flavor, path in sorted(fleets.items()):
        render(tool, flavor, [path, str(overlay)], scratch / flavor)
    for extra in sys.argv[4:]:
        render(tool, extra, [str(Path(extra).resolve())],
               scratch / Path(extra).stem)


if __name__ == "__main__":
    main()
