# Architecture

AI Secretary is an independent Ghost-ALICE addon for broad local desktop work. Its core owns actual user admission, conversation identity, outgoing draft approval and desktop effect records. The bundled OpenClaw adapter supplies an inference host; replacing that adapter does not move these responsibilities out of the core.

```mermaid
flowchart TD
    User[User request and exact outgoing approval] --> Core[Secretary admission and conversation runner]
    Core --> Gate[Ghost-ALICE current input gate]
    Core --> Runtime[Runtime contract]
    Runtime --> Host[OpenClaw adapter]
    Host --> Tools[Secretary tool worker]
    Gate --> Tools
    Tools --> Ledger[Task, approval, operation and desktop lease ledger]
    Ledger --> Native[macOS native helper]
    Native --> Apps[Desktop apps, downloads, files and draft composers]
```

## Source boundaries

| Surface | Responsibility |
| --- | --- |
| `src/ai_secretary/runtime.py` | Frozen `RunRequest`, replaceable `Runtime` protocol, persisted conversation identity, original user admission and gate before dispatch. |
| `src/ai_secretary/governance.py` | Bridge to the installed Ghost-ALICE input ledger and current downstream gate. |
| `src/ai_secretary/service.py`, `ledger.py`, `contracts.py` | Task-bound tools, exact draft approval, operation identity, retries, desktop lease and effect uncertainty. |
| `src/ai_secretary/resources/` | One canonical tool catalog, provider route policy and Ghost-ALICE gate script. Python reads these as package resources. |
| `src/ai_secretary/adapters/openclaw.py` | OpenClaw executable resolution, argv and local client cleanup. |
| `openclaw-plugin/` | OpenClaw hooks, native session binding, tool registration and image result formatting. |
| `native/Desktop.swift` | macOS window observation, accessibility, screenshots and guarded mouse/keyboard/file actions. |
| `scripts/installer.py` | Complete private Node/OpenClaw bundle, native build, launchers and ownership-aware install/reinstall/uninstall. |
| `native/TestWindow.swift`, `tests/`, smoke scripts | Synthetic and opt-in integration fixtures. The test-window executable is excluded from the installed product. |

The Python package's MCP catalog and governance gate work without the `openclaw-plugin` directory. The current distribution ships one inference adapter, OpenClaw. It does not ship a second inference engine.

OpenClaw may capture the plugin package into a temporary loading directory. At registration the plugin reads canonical schemas and policy through its configured `source`, which also supplies the Python worker's import path. Ordinary direct imports can resolve the physical sibling `src` directory. No second schema or policy copy is maintained inside the plugin.

## Admission and continuity

The production user-run function accepts the exact text supplied by the user, records a real admission, checks the current Ghost-ALICE gate and then sends an immutable request to the selected runtime. Provider route rejection happens before runtime dispatch.

`control-sessions.sqlite` persists the route and conversation UUID. The binding is `route::UUID`; ordinary follow-ups preserve the conversation and TaskId. `run --new-task` creates a distinct conversation and task. OpenClaw native session keys and session IDs are bound together so a new native session cannot reuse a previous conversation's admission. Admission identities cannot be replayed with changed text or replace a newer admission.

The existing Ghost-ALICE ledger namespace `openclaw` is explicit compatibility storage. It does not make the Secretary core dependent on OpenClaw as an inference host. Existing admitted conversations and grants retain their identity.

Current-user text, admission identity and external-user provenance must come from the user-operated runner or actual host facts. A projected model prompt, tool arguments, received document or old conversation is not a substitute for new user input. The pinned CLI's native admission fields are incomplete on the demonstrated path, so the user-operated `ai-secretary run` entrypoint is required there.

## Approval and effects

Preparing an outgoing draft does not approve it. The worker stores the account, recipients, body, attachment content hashes and invitation details. The user reviews that stored content through an interactive terminal and approves its exact digest. Changes to the draft invalidate the grant. Grants are task-bound; continuation preserves them, while a separate task cannot use the old draft's approval.

Each `operation_id` binds the current task to the action, target and business effect. Refreshed observation tokens do not change the effect identity. Reusing an operation for a different effect is rejected; a completed matching effect reuses its result. A desktop lease prevents simultaneous cooperating workers from claiming the desktop. The worker checks the current gate, task, input lineage and approval again before invoking an effect.

Native actions require a fresh observed window and matching process/window guards. If an effect may have started but its outcome is uncertain, the ledger records `Unknown` and requires reconciliation before another attempt. Recorded OS input completion does not establish that an app saved a file or sent a message.

The outgoing button label check is an assistive heuristic. It does not recognize every app's opaque GUI meaning or automatically prove that a composer matches the approved draft. The user's approval rule and visual interpretation remain required.

## Model and process boundaries

`resources/model-policy.json` currently accepts the declared Ollama provider route. The bundled configuration defaults to a loopback Ollama endpoint and permits Secretary tools. A provider name does not attest that the actual model server is on premises. Raw MCP transport also cannot verify an upstream host's model or network location; endpoint configuration and network isolation require separate checks.

Interrupting the local OpenClaw client terminates and reaps that client, including repeated Ctrl-C cleanup and cancellation from a worker-thread event loop. This does not prove the Gateway run stopped. Use Gateway stop controls and reconcile pending effects before retrying.

## Installation and release boundaries

The complete addon installer stages and verifies the private runtime, application sources, native executable, skills and registry attribution before publication. It tracks immutable managed files separately from mutable Secretary/OpenClaw state. Collision, drift and interrupted-install recovery are handled before replacing owned resources. Removal preserves user modifications and state; its retry command remains available through partial removal.

Typed `platform_registries` map platform identities to ordinary Ghost-ALICE registry directories. Each selected platform sidecar participates in the same addon-owned transaction as the shared runtime and skills. The standalone single-directory mode remains compatible with existing installs. Switching a recorded registry selection requires explicit removal with the original selection before reinstall; it does not silently adopt another record. Removing a shared official installation removes all of its selected platform resources. Core dispatch uses its fixed managed handler rather than executing arbitrary registry command text.

The source release includes the adapter and installer, while the installer provisions the pinned OpenClaw and Node packages in a private prefix. It does not vendor a local runtime cache into this repository. Exact top-level versions and package integrity are checked, but network installation currently resolves transitive npm dependencies at install time. It is not a reproducible dependency lock for the entire runtime.

The first desktop backend requires macOS 14 or later and a Swift compiler. The portable Python core does not imply that the desktop addon is supported on other operating systems. Real model inference, actual KakaoTalk/WeChat attachment workflows, universal outgoing GUI semantics and automatic `Unknown` reconciliation remain separate validation work.
