# Physical rehearsal with SuperDex

Designer **Simulate** and Protocol Assistant strict simulation now run the native
Bravo primitive tasks on an isolated simulation controller. Each controller
motion is checked before positions change. The physical scene uses
[SuperDex Physics](https://projectsuperdex.com/physics/docs/overview/) with closed
Bravo URDF tool/gripper collision meshes, calibrated deck planes, catalog
labware envelopes, and carried plate envelopes. The separate **Walk through
draft** action illustrates order and does not grant physical clearance.

## Installation

In the repository environment, using Python 3.12 on a supported platform:

```sh
python -m pip install -e '.[physics]'
```

The deployment used for this change has SuperDex 1.0.0 installed. Its current
native wheels support macOS arm64, Linux x86_64 and Windows amd64. A missing SDK,
invalid mesh, missing teachpoint or missing collision dimensions causes an
explicit failed rehearsal. It never falls back to unchecked motion. Read-only
planning and the control panel can still load without the optional SDK.

## Planning and tip state

`GET /api/workflows/{workflow_id}/mechanical-readiness` checks the saved native
graph against the selected head and catalog without motion. It reports missing
deck bodies, incompatible selections, and repeated tip-lifecycle errors. This
is a static planning check; strict simulation still checks the actual expanded
loops, runtime inventory, liquid classes and commanded motion.

The native ledger keeps **fresh**, **spent**, **occupied**, and **empty** wells
separate. A returned tip occupies a hole but is spent and cannot be picked as
fresh. The ledger follows moved racks. Only an explicit restock/load resets
fresh supply. In a repeated fresh-tip workflow, the model must plan a clean
supply and a separate compatible empty return rack or configured waste. A
catalog rack is not evidence of loaded inventory. A fixed repeated pickup/return
anchor is rejected; the native runtime also stops on spent pickup, exhaustion,
or occupied return wells. A dedicated source can return its final spent set
to its original rack when that rack will never be used again.

The drafter now preserves each proposed rack's `tipbox_fill_state`: clean
supply is full and the separate spent-return box is empty. These are proposed
loading instructions, not evidence of physical inspection. For a gripper-equipped
Bravo, include a visible native DockGripper before tip handling. The native
regression found an undocked finger hitting a rack on a later single-tip pickup;
the same twelve-cycle path clears after docking.

## What is checked

- Axis limits and finite positions; sampled interpolation of every actual
  native controller command, including endpoints and positions between them.
- Tool/gripper contacts with deck pads and labware. A low path through a
  neighboring stack fails even when both endpoints are clear.
- Carried plate envelopes during verified native PickPlace stages, including
  Stack, Destack, Mount and Unmount. Their poses follow the gripper calibration
  and move to their new deck locations after release.
- Mounted tip axes against well centers and bottoms, using recorded tip length,
  well grid, depth and diameter. Intentional rack seating and flange contact
  have narrow task-specific allowances; they do not suppress neighbor contacts.
- Drift of profile calibration, teachpoints or labware metadata during the
  rehearsal. Reports pin robot assets, profile and initial deck geometry by
  SHA-256 and retain the failed task, bodies, penetration and sampled pose.

The native SDK runs on one dedicated owner thread. Initialization and disposal
do not freeze the UI event loop; task worker threads synchronously wait for each
collision guard. A rejected multi-axis move leaves all controller positions
unchanged. Errors stop the rehearsal and highlight the task in Designer.

## Limits of a clear result

The default spacing is 0.5 mm with a 0.05 mm penetration reporting threshold.
These are sampled geometric checks, not certified continuous collision
detection. They interpolate each commanded move in axis space; firmware timing
may differ. The 384ST head uses its collision CAD mesh; other head housings use
a conservative envelope because separate CAD exports are unavailable.

Plate envelopes include solid flanges and approximate well access. Detailed
tip taper, grasp force, fluid retention, calibration accuracy, frame/gantry
self-collision and wet-lab performance are not qualified. Lid operations,
sealed/lidded labware and enabled accessories without collision geometry fail
closed. Passing checks does not authorize hardware execution or qualify a
liquid method. The saved revision's method review and release remain required.

Some deployed tip-box records have zero or missing hole diameters. Those records
remain incomplete: return-tip entry fails with the missing geometry identified.
The simulator does not replace unknown catalog measurements with guessed
clearances. Native tip-cycle regressions use explicitly labeled synthetic
mechanical fixtures; they do not qualify the deployment's rack dimensions.
