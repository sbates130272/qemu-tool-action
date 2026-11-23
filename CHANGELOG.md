# Changelog

All notable changes to this action are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the action uses
[Semantic Versioning](https://semver.org/). `vX` and `vX.Y` are floating tags
that follow the newest matching release.

## [Unreleased]

## [2.2.0] - 2026-11-23

qemu-tool 3 by default, and four things a downstream CI asked for: a
strict GPU mode, diagnostics at teardown, your own SSH names for the
guests, and a limit on the stack coming up.

**Read the first entry under Changed before upgrading.** A job that does
not pin `qemu-tool-version` moves from qemu-tool 2.1.0 to 3.x, which
refuses ten fleet XML elements that 2.x read. `qemu-tool-version: '~=2.1'`
keeps a job on 2.x.

### Added

- `gpu: required` brings up every guest it can and then fails the step
  unless every guest ended with its GPU bound (`loaded`), naming each
  guest that did not and its status; a guest with no GPU (`absent`) or no
  `amdgpu-probe` (`unknown`) is a failure there. `gpu: optional` runs the
  same bring-up and nothing in it fails the step.
  `true` and `false` do what they did.
- `diagnostics: true` writes, at teardown and whether the job passed or
  not, a `diagnostics` directory under `logs-dir`: each guest's whole
  `dmesg`, `lspci -nnk`, `lsblk`, `lsmod` and the parameters of the GPU,
  NIC, NVMe and SDXI kernel modules, and the stack's resolved compose
  configuration. Values in it are redacted by name (`password`, `secret`,
  `token`, `api-key`, `*_KEY`, `*_PASS`, `*_AUTH` and the like), a
  mapping or list under such a name whole, and the user part of a URL; a
  secret under another name in an override file of yours is not, nor is
  one glued to a short flag (`-pSECRET`). Best effort, bounded in time and
  in bytes, and never a reason for teardown to fail. `NOTES.txt` and the
  summary list what that teardown wrote: each guest's directory is
  emptied first, so an earlier fleet's files are not shown as this one's.
- `ssh-aliases`: your own Host names for the guests in the `ssh-config`
  output, as a pattern (`guest{index}`), a name per guest, or both
  (`guest guest{index}`). The guest's name stays, and so does `vm<n>`
  unless you give that word to another guest. Each `vm-manifest` entry
  gains `ssh_aliases`.
- `stack-timeout`: a limit on `qemu-tool quick-start up`, the image pulls
  and the start of the stack's containers, which had none. Off by default.
  `wait-timeout` remains the limit on the guests' boot.
- CI boots two guests through the action, with RDMA between them, on every
  pull request, push to `main` and scheduled run; it was a manual job.

### Changed

- **The default `qemu-tool-version` is `>=2.1,<4`; it was `>=2.1,<3`.** A
  job that does not pin now gets qemu-tool 3.x, where it got 2.1.0. The
  2.1.0 README said a qemu-tool 3 would come with a new major version of
  this action; it comes in this release instead, on `@v2`.
  qemu-tool 3 refuses ten fleet XML elements that 2.x read:
  `<qemu-digest>`, `<ernic-digest>`, `<rocjitsu-digest>`,
  `<backing-digest>`, `<domain-pattern>`, `<prom-port>`, `<loki-port>`,
  `<vm-backing-shared>`, `<ernic-subnet>` and `<ernic-mac-prefix>`. A
  fleet XML started from this action's `examples/fleet-minimal.xml` as it
  was in 2.1.0, or from one of the fleets qemu-tool 2.x shipped, sets
  `<vm-backing-shared>` and is refused until that line is deleted. A
  fleet XML of yours that sets any of the ten has to drop it, or the job
  has to pin 2.x (`qemu-tool-version: '~=2.1'`); the README's "From
  qemu-tool 2.x to 3.x" says what each was. qemu-tool 3 also starts every
  guest with the CPU vendor `AuthenticAMD`, whatever the runner's CPU is.
- Generating the GPU firmware (600 s) and copying it into a guest (300 s)
  now have limits; they had none. A generation that runs out of time has
  its container removed. A guest whose firmware install and GPU bind
  overrun their 600 s fails the step with a one-line error; that was a
  traceback.
- When the GPU step fails, the outputs, `QEMU_TOOL_SSH_CONFIG` and the
  step summary are written first, so they show the status each guest's GPU
  was left in. The step fails as before. The fleet then counts as up for a
  later `exec` step and for the guest logs collected at teardown.
- Every guest's GPU status is read before the step fails on one of them;
  it used to stop at the first.
- `gpu` is validated on an `exec` step too, as are `stack-timeout`,
  `ssh-aliases` and `diagnostics`: a `gpu` that is not `true`, `required`,
  `optional` or `false` is now refused there, where it was ignored. An
  `exec` step that carries a mistyped `gpu` fails until it is corrected
  or removed.
- `examples/fleet-minimal.xml` no longer sets `<vm-backing-shared>`.
  qemu-tool 2.1.0 renders the same stack with and without it, and
  qemu-tool 3 refuses a fleet file that sets it.
- CI installs from the `v2.1.0` and `v3.0.0` tags of qemu-minimal, boots
  the example fleet on each, and runs the smoke test on PyPI's 2.1.0 as
  well as on the default.

### Fixed

- A whole-number input given a non-ASCII digit (`²`, `５`) is refused with
  the usual one-line error; `²` was a traceback and `５` was accepted.

## [2.1.0] - 2026-11-20

A fleet of your own becomes a documented, checked path; qemu-tool can be
installed from a tag or commit of qemu-minimal; and the stack's containers
run the same qemu-tool as the runner. Workflows written for 2.0.0 keep
working; the entries under Changed are where behaviour differs.

### Added

- `qemu-tool-ref`: install qemu-tool from a tag, a branch or a full commit
  SHA of qemu-minimal instead of from PyPI, with `qemu-tool-repository` to
  name a fork. Outputs `qemu-tool-sha` and `qemu-tool-checkout`.
- `xml-overlay`: fleet XML files layered on a shipped flavor or on a fleet
  of your own, so a change kept in a file no longer needs `extra-args`.
- The fleet is read and rendered by qemu-tool before anything is pulled, so
  an element the installed version does not know or a guest image on a
  moving tag fails in the first minute, in qemu-tool's own words. A fleet
  with no `<backing-ref>` is refused by the action.
- `ssh-key-file` and `ssh-user`, for a guest image that was not published
  with `qemu-tool publish-qcow`.
- `nics` in each `vm-manifest` entry, and `QT_NIC_IPS` for the script.
- `examples/fleet-minimal.xml` and `examples/overlay-one-guest.xml`, and a
  "Which fleet" and "A fleet of your own" guide in the README.

### Changed

- The stack's own containers now run the runner's qemu-tool. They used to
  install the latest release from PyPI at start, whatever the runner had,
  so a pinned `qemu-tool-version` pinned only half of the stack.
  `container-qemu-tool: pypi` restores that.
- `size-from-host` defaults to `auto`: guests are sized from the runner
  only when neither the inputs nor a fleet XML file of your own sets a
  size. Sizes in an `xml` file used to be overridden unless it was set to
  `false`; `size-from-host: true` still does that.
- `qemu-tool-source` is installed from a copy and no longer editable, so
  nothing is written into the checkout. It still overrides
  `qemu-tool-version`; `qemu-tool-ref` cannot be combined with either.
- The host is prepared before qemu-tool is installed (it was after), so
  that `qemu-tool-ref` can use the git that step installs.
- `flavor` takes any fleet the installed qemu-tool ships, and an unknown
  one is refused with the names that version has.
- `rdma_ip`, and `QT_RDMA_IP`, are the address of the NIC qemu-tool treats
  as the guest's own, the one on the mesh, where a guest has several. It
  was the first NIC listed.
- `gpu-generation` defaults to the generation the fleet's
  `<rocjitsu-config>` names.
- `quick-start up` is run from the action's own directory. Relative paths
  given to its file and directory flags in `extra-args` are still taken
  against the workspace.
- `fleet-xml` always lists the fleet's file first; it was empty for a
  shipped flavor used unchanged.

### Fixed

- `wait-for: rdma` on a guest image without rdma-core says so instead of
  suggesting more vCPUs.
- A guest whose image has no `amdgpu-probe` is reported as such in the GPU
  step.
- The `args` example in `action.yml` used `gen-compose --check` without
  `--output-dir`, which compares a fleet of your own against a shipped
  stack.

## [2.0.0] - 2026-11-19

A rebuild around [`qemu-tool`](https://github.com/sbates130272/qemu-minimal)
2.x fleets. One step now boots QEMU guests with an emulated ROCm GPU, an
RDMA NIC and NVMe, runs your script in them, and tears them down when the
job ends:

```yaml
- uses: sbates130272/qemu-tool-action@v2
  with:
    vm-count: 1
    copy: .
    run: |
      make
      make test
```

**This is a breaking release.** Nothing moves `@v1`, but `@v1` installs the
latest `qemu-tool` unless told otherwise, which is now 2.x, so a workflow
that stays on `@v1` has to set `qemu-tool-version` to a 1.x release. See
[Migrating from v1](https://github.com/sbates130272/qemu-tool-action#migrating-from-v1).

### Added

- `operation: up` (the default): install `qemu-tool`, prepare the runner
  (`/dev/kvm` access, `oras`, `qemu-utils`, `zstd`, `ansible-core`), pull the
  published guest image, boot a fleet, wait for it, bring up the emulated
  GPU, then `copy` files in, `run` a script and `fetch` results out.
- `operation: exec` to do the same copy, run and fetch against a fleet an
  earlier step booted; `operation: down` to collect logs and tear it down
  early; `operation: install` and `operation: command` for driving
  `qemu-tool` yourself.
- A post step that collects container and guest logs and removes the stack
  whether the job passed or failed (`teardown: never` leaves it running).
- `gpu` (on by default): stages the firmware of the rocjitsu serving each
  guest and loads `amdgpu` with the emulation's parameters, so `/dev/kfd`
  and a GPU node exist when the script starts. `quick-start up` alone leaves
  the GPU on the PCI bus and unusable.
- Fleet selection with `flavor` (`default`, `mi355x`, `helios`) or your own
  fleet `xml`, with `vm-count`, `vcpus`, `vmem` and `fleet` overrides. Guests
  are sized from the runner by default.
- `wait-for: ssh | rdma | none`, `run-on`, `env`, `working-directory`, and
  `QT_*` variables that tell each copy of the script which guest it is and
  where its RDMA peers are.
- `metrics: true` starts Prometheus, Grafana and log collection.
- Outputs `ssh-config`, `inventory`, `key-file`, `key-dir`, `stack-dir`,
  `stack-name`, `images-dir`, `fleet-xml`, `logs-dir`, `fetch-dir`,
  `vm-names`, `vm-count`, `qemu-tool-version` and `qemu-tool-path`.
  `QEMU_TOOL_SSH_CONFIG` and `QEMU_TOOL_STACK` are exported for later steps.
- `fetch` copies files out of the guests after the script, whether it
  passed or not.
- `in-network: auto` reaches the guests over the stack's Docker network when
  the job itself runs in a container.

### Changed

- `operation` is optional and defaults to `up`. `wait-timeout` now bounds
  the wait for the guests and defaults to 900 (was 120). `extra-args` are
  flags for `qemu-tool quick-start up`; `vcpus` and `vmem` size every guest
  of the fleet.
- Requires `qemu-tool` 2.1 or later; the default `qemu-tool-version` is
  `>=2.1,<3`.
- The action is now a JavaScript action on `node24` (it was composite), so
  that it can have a post step. It needs `python3` 3.10 or newer on the
  runner, and a self-hosted runner recent enough to run `node24` actions.
- `vm-manifest` entries gain `index`, `ssh_host`, `username`,
  `ssh_key_file`, `ssh_cmd`, `rdma_ip`, `rdma_ips`, `rdma_device` and `gpu`;
  `image_path` is gone.

### Removed

- `operation: gen-vm`, `run-vm`, `compose` and `gen-compose` and their
  per-flag inputs. They are refused with a message naming the replacement:
  use `operation: command` with `args`.
- `env-file`, `stack`, `compose-profiles`, `compose-args` and
  `wait-for-healthy`. `qemu-tool` 2.0 replaced env-files with fleet XML: use
  `xml`, `flavor`, `fleet`, `metrics` and `wait-for`.

## [1.0.0] - 2026-10-06

Initial release: a composite action wrapping the `qemu-tool` 1.x `gen-vm`,
`run-vm`, `compose` and `gen-compose` subcommands.

[Unreleased]: https://github.com/sbates130272/qemu-tool-action/compare/v2.2.0...HEAD
[2.2.0]: https://github.com/sbates130272/qemu-tool-action/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/sbates130272/qemu-tool-action/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/sbates130272/qemu-tool-action/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/sbates130272/qemu-tool-action/releases/tag/v1.0.0
