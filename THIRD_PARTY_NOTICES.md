# Third-party notices

The AI Secretary source is licensed under Apache-2.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE). Ghost-ALICE is maintained in the separate [AidALL/ghost-alice](https://github.com/AidALL/ghost-alice) repository under Apache-2.0. AI Secretary uses its installed governance and installer interfaces rather than including a private Ghost-ALICE checkout in this source release.

## Runtime packages provisioned by the installer

| Package | Pinned version | License and notices |
| --- | --- | --- |
| OpenClaw | `2026.9.8` | [MIT license](https://github.com/openclaw/openclaw/blob/v2026.9.8/LICENSE), copyright OpenClaw Foundation. Its upstream `THIRD_PARTY_NOTICES.md` covers incorporated/adapted code and remains part of the installed package. |
| Node.js | `24.21.0` | [Node.js license file](https://github.com/nodejs/node/blob/v24.21.0/LICENSE), including bundled third-party license texts. The private Node distribution retains its license files. |

The repository does not publish local `node_modules`, runtime caches, compiled desktop binaries or installed model weights. The complete installer obtains the runtime packages separately and preserves the license and notice files contained in those packages. npm dependencies retain their own licenses; the top-level OpenClaw license does not replace them.

The installer checks the exact OpenClaw package version and npm integrity and checks the Node version and downloaded archive checksum. Transitive npm dependencies are resolved at installation time; this release does not include a reproducible transitive dependency lock. A binary redistribution or vendored runtime release would need a license inventory for that exact dependency set in addition to these source-release notices.

Python and the Swift/macOS development tools are prerequisites supplied separately by the operator. Ollama and any selected model weights are configured separately and are subject to their respective licenses. No model is downloaded by this source publication.
