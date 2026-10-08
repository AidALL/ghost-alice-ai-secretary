# AI Secretary

Language: English | [Korean](./README_ko.md)

AI Secretary is an independent [Ghost-ALICE](https://github.com/AidALL/ghost-alice) addon for local desktop work across apps, including KakaoTalk and WeChat. The Secretary core owns actual user admission, exact outgoing approval, conversation identity and desktop effect records. Its replaceable OpenClaw adapter hosts inference; native macOS tools observe and operate windows, mouse, keyboard and files. App-specific APIs are optional.

Current addon source version: `0.1.0`. Public source: [AidALL/ghost-alice-ai-secretary](https://github.com/AidALL/ghost-alice-ai-secretary). This page is the complete operator guide. See [Architecture](./ARCHITECTURE.md) for implementation boundaries, [Apache-2.0](./LICENSE) for the source license and [third-party notices](./THIRD_PARTY_NOTICES.md) for the provisioned runtime.

## Contents

- [Requirements](#requirements)
- [Install the complete addon](#install-the-complete-addon)
- [Check first-use readiness](#check-first-use-readiness)
- [Configure the on-prem model](#configure-the-on-prem-model)
- [Check the endpoint and real inference](#check-the-endpoint-and-real-inference)
- [Check macOS permissions](#check-macos-permissions)
- [Start a real task](#start-a-real-task)
- [Stop and retry](#stop-and-retry)
- [Approve outgoing work](#approve-outgoing-work)
- [Custom paths, reinstall and removal](#custom-paths-reinstall-and-removal)
- [Privacy and current limits](#privacy-and-current-limits)
- [Development validation](#development-validation)

## Requirements

- macOS 14 or later, Python 3.11 or later and a Swift compiler.
- An installed Ghost-ALICE core and its actual source checkout path.
- A reachable on-prem Ollama server with the selected model already installed. Model weights and the model server are separate from this addon.
- A model that actually supports vision and tool calling for GUI work, plus macOS Accessibility and Screen Recording permission for the execution environment.

Ghost-ALICE supplies the input ledger and current gate. [Autopilot](https://github.com/AidALL/ghost-alice-autopilot) has its own approved-run state and continuation hooks; installing Secretary does not approve or activate an Autopilot run. The English/Korean pairing follows the Core and Autopilot documentation convention, while the operator instructions remain together on this page.

## Install the complete addon

Set `SECRETARY_GHOST_ROOT` to your actual Ghost-ALICE source checkout, then install from this addon checkout:

```sh
export SECRETARY_GHOST_ROOT="$HOME/ghost-alice"
git clone https://github.com/AidALL/ghost-alice-ai-secretary.git "$HOME/ghost-alice-ai-secretary"
cd "$HOME/ghost-alice-ai-secretary"
./scripts/install.sh --ghost-alice-root "$SECRETARY_GHOST_ROOT"
python3 scripts/installer.py status
```

The installer provisions the skill, Secretary program, native helper, OpenClaw `2026.9.8` and Node `24.21.0` together. Defaults are `~/.local/share/ai-secretary` for the program, `~/.agents/skills/ai-secretary` for the skill and `~/.ghost-alice/addons/ai-secretary.json` for the registry record. It does not start a background service, install model weights or prove inference readiness. If already installed, continue with the readiness checks using the original installation paths.

The official Core `ai-secretary` alias is being prepared in [Core integration PR #47](https://github.com/AidALL/ghost-alice/pull/47), pending merge and delivery. Use this addon's installer until that integration is available in your installed Core. The existing generic Core `--addon-source` path installs skill content and does not provision this complete runtime.

The installer checks the top-level OpenClaw version and npm integrity, and the Node version and distribution archive checksum. Network installation still resolves npm transitive dependencies at install time; this is not a reproducible lock of the entire dependency set. `--openclaw-source` and `--node-source` reuse verifiable pinned caches for development validation.

## Check first-use readiness

For the default install, use its launchers in every terminal used below. Substitute your original prefix if customized:

```sh
export SECRETARY_PREFIX="$HOME/.local/share/ai-secretary"
export PATH="$SECRETARY_PREFIX/managed/bin:$PATH"
openclaw config file
```

The launchers select `<prefix>/state/openclaw.json` and `<prefix>/state/openclaw/`. The default active configuration is `~/.local/share/ai-secretary/state/openclaw.json`. Edit this mutable state configuration, rather than the managed template at `<prefix>/managed/config/openclaw.json`. Reinstall preserves an existing state configuration, so reinstalling does not reset your endpoint or model selection.

| Check | What it establishes | What remains to check |
| --- | --- | --- |
| `python3 scripts/installer.py status` | Owned installed files, drift and registration state | Model and permissions are explicitly `not-checked`; status may recover an interrupted installation under its lock. |
| `openclaw config validate` | Configuration schema validity | Endpoint reachability, model capability and inference. |
| `ai-secretary doctor` | Native Accessibility and Screen Recording permission flags | The configured model and real app behavior. Its `model_endpoint` is a hardcoded localhost hint, not a probe or active-config read. |
| Ollama catalog and model details | Endpoint response, available model ID and declared capabilities | A real inference response and actual GUI behavior. |
| A small real inference request | The selected server/model answers that request | Vision, tool use and the selected app/file outcome. |
| A bounded Secretary task in your selected app | The observed outcome for that app, file and request | Other apps or workflows remain separate checks. |

An `installed` status or `true` permission flags can coexist with a refused model connection. Report the highest stage actually checked; installation, Gateway startup and protocol smoke checks do not establish a functioning model or successful file workflow.

## Configure the on-prem model

The initial template declares `http://127.0.0.1:11434` and `qwen3-vl:8b`. These are defaults, not evidence that a server or model exists. Supply your actual on-prem native Ollama URL and exact installed model ID. Use the server's native API base URL without `/v1` or a trailing slash; the provider remains `ollama`.

Three settings must agree:

| Field | Value |
| --- | --- |
| `models.providers.ollama.baseUrl` | Actual on-prem Ollama base URL |
| `models.providers.ollama.models[0].id` | Exact model ID served there |
| `agents.defaults.model.primary` | `ollama/` followed by that same model ID |

Stop the foreground Gateway before changing these values if it is running. Replace both placeholders before executing this recipe. Python encodes the values as JSON, and the batch changes the three coupled fields together:

```sh
export SECRETARY_OLLAMA_URL='http://YOUR_ON_PREM_HOST:11434'
export SECRETARY_MODEL_ID='YOUR_INSTALLED_VISION_TOOLS_MODEL'
SECRETARY_MODEL_CONFIG_JSON="$(python3 - "$SECRETARY_OLLAMA_URL" "$SECRETARY_MODEL_ID" <<'PY'
import json, sys
endpoint, model = sys.argv[1:]
print(json.dumps([
    {"path": "models.providers.ollama.baseUrl", "value": endpoint},
    {"path": "models.providers.ollama.models[0].id", "value": model},
    {"path": "agents.defaults.model.primary", "value": "ollama/" + model},
]))
PY
)"
openclaw config set --batch-json "$SECRETARY_MODEL_CONFIG_JSON" --strict-json --dry-run
```

If the dry run passes and the values are correct, apply the same batch and read back the active settings:

```sh
openclaw config set --batch-json "$SECRETARY_MODEL_CONFIG_JSON" --strict-json
openclaw config get models.providers.ollama.baseUrl
openclaw config get 'models.providers.ollama.models[0].id'
openclaw config get agents.defaults.model.primary
openclaw config validate
```

Bracket paths are quoted for zsh. These commands use the pinned runtime's JSON batch mode and validate the final configuration; successful validation does not call the model. Its shipped command reference is `<prefix>/managed/openclaw/node_modules/openclaw/docs/cli/config.md`; the launcher/state wiring is in [the installer](./scripts/installer.py). Check the remaining model declaration too: `input: ["text", "image"]`, context window and output token limits must reflect the actual selected model. Writing `image` in configuration cannot add vision capability to a text-only model. If you already maintain a different model list, select the intended catalog entry consistently rather than changing an unrelated entry.

## Check the endpoint and real inference

Use the same actual endpoint and model variables configured above. The [catalog API](https://docs.ollama.com/api/tags) and [model-details API](https://docs.ollama.com/api-reference/show-model-details) provide the availability and capability checks:

```sh
curl --fail --silent --show-error "$SECRETARY_OLLAMA_URL/api/tags"
SECRETARY_SHOW_JSON="$(python3 - "$SECRETARY_MODEL_ID" <<'PY'
import json, sys
print(json.dumps({"model": sys.argv[1]}))
PY
)"
curl --fail --silent --show-error "$SECRETARY_OLLAMA_URL/api/show" \
  -H 'Content-Type: application/json' --data "$SECRETARY_SHOW_JSON"
```

Confirm the exact model appears in the catalog and inspect its declared `vision` and `tools` capabilities. GUI use requires both to work on your server/model; a catalog entry or capability declaration alone is insufficient. If the connection is refused, prepare or start the intended on-prem server before continuing. If the model is absent or unsuitable, have the server operator install/select a suitable model and repeat the configuration checks. [Ollama installation](https://ollama.com/download/mac) and the [model catalog](https://ollama.com/library) are separate server preparation references.

Request a real text response with the [chat API](https://docs.ollama.com/api/chat). This diagnostic sends a small prompt, without app content:

```sh
SECRETARY_PROBE_JSON="$(python3 - "$SECRETARY_MODEL_ID" <<'PY'
import json, sys
print(json.dumps({"model": sys.argv[1], "stream": False,
    "messages": [{"role": "user", "content": "Reply with READY only."}]}))
PY
)"
curl --fail --silent --show-error "$SECRETARY_OLLAMA_URL/api/chat" \
  -H 'Content-Type: application/json' --data "$SECRETARY_PROBE_JSON"
```

Inspect the returned model response and any error. A successful HTTP response with real generated content establishes this text inference check only. Verify actual screenshot understanding and Secretary tool use in the bounded app task below before claiming GUI readiness.

## Check macOS permissions

```sh
ai-secretary doctor
```

Check `accessibility` and `screen_recording`. If either is false, grant the execution environment access in macOS System Settings → Privacy & Security → Accessibility / Screen Recording, then rerun from that same environment. The `model_endpoint` localhost text is fixed in the current doctor implementation even when the active configuration uses another on-prem server. Doctor does not test a model connection, load a model or run inference.

## Start a real task

After configuration, endpoint, inference and permission checks, keep the foreground Gateway running in one terminal:

```sh
openclaw gateway run --bind loopback
```

In a second terminal, set the same prefix and PATH, then supply your actual request to the user-operated runner. Replace the example's app, attachment and destination with your selected real task:

```sh
export SECRETARY_PREFIX="$HOME/.local/share/ai-secretary"
export PATH="$SECRETARY_PREFIX/managed/bin:$PATH"
ai-secretary run --runtime openclaw \
  'In WeChat, download the attachment I selected, save it to the folder I named, and open that folder.'
```

Choose a bounded first task with an identifiable app/window, actual attachment or file, absolute destination and observable completion condition. For example, inspect the saved file at that path and verify its contents before counting an attachment download as complete. A task with no selected app/file or destination cannot establish a real workflow result. KakaoTalk, WeChat and other apps use the same observation/action tools, but each app's GUI behavior needs its own observed outcome.

`run` records the original text you supply in Ghost-ALICE, checks the current gate and passes an explicit conversation route and UUID to the runtime. Ordinary follow-ups preserve the conversation and TaskId; `run --new-task` starts a distinct task. Existing conversation UUIDs and approval records are preserved. OpenClaw is the currently shipped inference adapter. Development can select an executable with `--runtime-executable /absolute/path/to/openclaw`; `--openclaw` remains a compatibility alias.

The demonstrated pinned CLI path does not supply all native current-request, admission-ID and provenance fields, so use `ai-secretary run` for user tasks. A host providing those real fields can use native admission. Assembled prompts, old conversation, tool arguments and received documents do not admit new user work.

For direct tools or MCP, first register the actual user task in the same conversation:

```sh
ai-secretary --session agent:main:secretary task 'Inspect the files in the downloads folder I named.'
ai-secretary --session agent:main:secretary invoke secretary_files \
  --args-json '{"path":"/absolute/path/to/downloads"}'
```

When continuing a conversation with a native session ID, pass `sessionKey::sessionId` to `--session`. OpenClaw `/new` and `/reset` separate earlier work through native session identity. Reusing an old session without the user runner does not guarantee current-request binding.

## Stop and retry

For the foreground Gateway started above, press Ctrl-C in its own terminal and wait for the process to exit. Ctrl-C in the `ai-secretary run` terminal cleans up the local client; it does not establish that Gateway work stopped. If you interrupted the client while work might still be running, stop the foreground Gateway too.

Before retrying, inspect the actual business effect: did the file save, did the composer change, or did a message send? Reobserve the relevant app and check the destination file or sent-message result. A completed matching OperationId reuses its recorded result; a different effect cannot reuse that ID. Uncertain effects are recorded as `Unknown` and require reconciliation before another attempt. OS input completion alone does not prove a saved file or sent message. Automatic `Unknown` reconciliation is not implemented. Preserve the state and obtain an evidence-backed resolution for an uncertain effect before reissuing it.

## Approve outgoing work

The user is the sender and approves the exact draft. The Secretary stores the account, recipients, body, attachment content hashes and invitation details with `secretary_draft`, then shows the draft. In your own interactive terminal, review the stored draft and its actual digest:

```sh
ai-secretary approve ACTUAL_DRAFT_ID --digest ACTUAL_DRAFT_DIGEST
```

The command prints the stored record and asks you to type `APPROVE ACTUAL_DRAFT_DIGEST`. Replace the placeholders with the stored values. A changed draft invalidates approval, and the actual app composer must match the approved content before sending. General computer-control permission does not authorize messages, replies or invitations. Reading, downloading, opening folders and preparing drafts inside the admitted task do not require approval for every click.

The outgoing-button label check is only an assistive heuristic. It cannot recognize every opaque GUI meaning or automatically prove that a composer matches the approved draft. Visual interpretation, exact user approval and observation of the final effect remain required.

## Custom paths, reinstall and removal

Keep the original prefix, skill directories and registry selection for status, reinstall and uninstall. This example deliberately reuses the same variables for all three operations; set them to your actual installation paths, rather than using this example to adopt an existing install:

```sh
export SECRETARY_GHOST_ROOT='/absolute/path/to/ghost-alice'
export SECRETARY_PREFIX='/absolute/path/to/ai-secretary'
export SECRETARY_SKILLS_DIR='/absolute/path/to/skills'
export SECRETARY_REGISTRY_DIR='/absolute/path/to/addons'
./scripts/install.sh --ghost-alice-root "$SECRETARY_GHOST_ROOT" \
  --prefix "$SECRETARY_PREFIX" --skills-dir "$SECRETARY_SKILLS_DIR" \
  --registry-dir "$SECRETARY_REGISTRY_DIR"
python3 scripts/installer.py status --ghost-alice-root "$SECRETARY_GHOST_ROOT" \
  --prefix "$SECRETARY_PREFIX" --skills-dir "$SECRETARY_SKILLS_DIR" \
  --registry-dir "$SECRETARY_REGISTRY_DIR"
```

Reinstall from the selected updated source checkout with the same paths:

```sh
./scripts/install.sh --ghost-alice-root "$SECRETARY_GHOST_ROOT" \
  --prefix "$SECRETARY_PREFIX" --skills-dir "$SECRETARY_SKILLS_DIR" \
  --registry-dir "$SECRETARY_REGISTRY_DIR"
```

Remove that installation when wanted:

```sh
./scripts/uninstall.sh --ghost-alice-root "$SECRETARY_GHOST_ROOT" \
  --prefix "$SECRETARY_PREFIX" --skills-dir "$SECRETARY_SKILLS_DIR" \
  --registry-dir "$SECRETARY_REGISTRY_DIR"
```

For default paths, run `./scripts/uninstall.sh`. Removal targets unchanged owned program, skill and registry resources. It preserves user modifications, pre-existing unowned resources, task ledgers, OpenClaw conversations and model configuration. Modified registry records block removal before program/skill deletion. Partial removal retains ownership evidence and a retry command, and reports preserved files. Reinstall uses locking, staging, rollback and interrupted-install recovery; `status` also enters the recovery path under the lock.

Repeat `--skills-dir` for multiple skill destinations; `--claude` also adds the Claude skill directory and must be reused for later operations if originally selected. To select Core's platform registries explicitly, repeat `--platform-registry codex=/absolute/dir` and `--platform-registry claude=/absolute/dir` with the corresponding skill directories. Do not combine `--platform-registry` with `--registry-dir`. Use the exact original platform and skill selections for subsequent status/reinstall/removal. Changing a recorded registry selection requires removal with the old selection followed by installation with the new one; task/conversation state is preserved.

An official multi-platform install is one shared bundle. Removal requested through one registered platform removes the bundle and all its owned platform registrations and skills. Other addons and personal work data are outside that removal scope.

## Privacy and current limits

Keep personal messages, attachments and screens on the verified on-prem model route. Do not configure external model fallbacks, channels or telemetry paths that forward this content. The default profile permits Secretary tools and disables general runtime tools. The canonical route policy is `src/ai_secretary/resources/model-policy.json`, which currently permits the declared Ollama provider. The name `ollama` does not prove that its URL, server, model execution or network are on premises. Check the actual endpoint and network isolation; an upstream host using MCP needs its own model/network review.

The implementation covers broad macOS GUI work, with fresh window/process guards and desktop effect records. It does not automatically prove all opaque app semantics, composer/draft equivalence or `Unknown` reconciliation. Real model inference and actual KakaoTalk/WeChat attachment workflows are separate validation from installation and protocol checks. The portable Python core does not establish desktop support on other operating systems.

## Development validation

The repository includes core contract, standalone package, protocol, install/removal and opt-in real macOS test-window checks. Ordinary unit tests explicitly skip real-window checks. Gateway smoke uses state tools and a real Ghost-ALICE gate without model inference; the agent harness uses a local protocol stub. Neither establishes LLM performance or a real collaboration-app workflow.

`SECRETARY_GHOST_ROOT` is the installed Ghost-ALICE source path, defaulting to `~/ghost-alice`. `SECRETARY_NODE` selects Node, defaulting to PATH; selecting the private installed Node makes the smoke runtime explicit:

```sh
export SECRETARY_GHOST_ROOT="$HOME/ghost-alice"
export SECRETARY_NODE="$HOME/.local/share/ai-secretary/managed/node/bin/node"
./scripts/build_native.sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
AI_SECRETARY_NATIVE_BINARY=build/desktop-secretary PYTHONPATH=src \
  python3 -m unittest discover -s tests -p test_native.py -v
```

The last command briefly opens a test window to check real input, save and screenshots. That fixture does not establish behavior in collaboration apps. For pinned-runtime checks without model inference, keep output in local temporary paths:

```sh
python3 scripts/smoke_openclaw.py \
  --runtime "$HOME/.local/share/ai-secretary/managed/openclaw" --output .tmp/gateway-smoke
python3 scripts/smoke_agent_harness.py \
  --runtime "$HOME/.local/share/ai-secretary/managed/openclaw" \
  --output .tmp/agent-harness --admission-mode user-run
```
