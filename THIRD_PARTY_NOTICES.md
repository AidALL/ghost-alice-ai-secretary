# Third-party notices

AI Secretary source is licensed under Apache-2.0; see [LICENSE](./LICENSE) and [NOTICE](./NOTICE). Ghost-ALICE is maintained separately in [AidALL/ghost-alice](https://github.com/AidALL/ghost-alice) under Apache-2.0. Secretary uses its installed governance/installer interfaces rather than publishing a private Core checkout.

## Runtime packages provisioned by the installer

| Package | Pinned version | License and retained notices |
| --- | --- | --- |
| OpenClaw | `2026.9.8` | [MIT license](https://github.com/openclaw/openclaw/blob/v2026.9.8/LICENSE), OpenClaw Foundation. The installed package retains upstream third-party notices and dependency licenses. |
| Node.js | `24.21.0` | [Node.js license](https://github.com/nodejs/node/blob/v24.21.0/LICENSE), including bundled third-party texts retained in the private distribution. |

The source repository excludes runtime caches, local `node_modules`, compiled desktop binaries and model weights. The installer provisions runtime packages separately, checks the exact OpenClaw version/npm integrity and the Node version/archive checksum, and preserves included licenses. npm transitive dependencies are resolved at installation time; these top-level pins do not constitute a reproducible whole-runtime dependency lock.

## Selected optional native sources

Peekaboo `4.6.0` is pinned at `b54551ce2d0013e52c2a405bfeabced88ec5c2e0`, matching the selected OpenClaw source revision `fc23bc864e4553c2d215e479eeec47b67a0bf943`. Secretary links selected `PeekabooAutomationKit`/`PeekabooFoundation` libraries and their required closure, with three disclosed source changes: private coordinator injection in two files and exact-window pointer delivery of the forbidden AX-value flag in one file. The source manifest, patch/hash records, Swift dependency lock and full notices are retained under [native/](./native/THIRD_PARTY_NOTICES.md). The independent builtin backend does not require these optional libraries.

| Selected dependency | License text |
| --- | --- |
| Peekaboo | [MIT, Peter Steinberger 2025](./native/Notices/Peekaboo.txt) |
| AXorcist | [MIT, Peter Steinberger 2025](./native/Notices/AXorcist.txt) |
| swift-log | [Apache-2.0](./native/Notices/swift-log.txt) |
| swift-algorithms | [Apache-2.0 with runtime library exception](./native/Notices/swift-algorithms.txt) |
| swift-numerics | [Apache-2.0 with runtime library exception](./native/Notices/swift-numerics.txt) |
| Commander | [MIT, Peter Steinberger 2026](./native/Notices/Commander.txt); resolved by SwiftPM, not linked into the selected product |

Optional native installation retains those notices. Source or binary redistribution must preserve the applicable full license texts and disclosed modifications. Any broader runtime/binary redistribution needs a license inventory for its exact dependency set; the top-level upstream license does not replace dependency licenses.

Python and Swift/macOS development tools are supplied separately. Ollama and selected model weights are configured separately and have their own licenses. This source publication downloads no model.
