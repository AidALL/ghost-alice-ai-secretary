# AI Secretary

Ghost-ALICE 아래에서 동작하는 독립 비서 애드온입니다. 비서 코어가 원래 사용자 입력, 발신 승인, 작업과 데스크톱 효과 원장을 소유합니다. 기본 추론 실행 백엔드는 교체 가능한 OpenClaw 어댑터이며, macOS 네이티브 도구가 실제 창·마우스·키보드·파일을 조작합니다. KakaoTalk과 WeChat을 포함한 여러 앱을 대상으로 하는 초기 구현이며, 앱별 API는 필수 조건이 아닙니다.

공개 소스는 [AidALL/ghost-alice-ai-secretary](https://github.com/AidALL/ghost-alice-ai-secretary)에 있으며 현재 애드온 버전은 `0.1.0`입니다. 구성 요소와 승인·실행 경계는 [ARCHITECTURE](ARCHITECTURE.md)에 설명합니다. 소스 라이선스는 [Apache-2.0](LICENSE)이며, 함께 구성되는 런타임의 라이선스는 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)를 확인하세요.

## 전체 애드온 설치

macOS 14 이상, Python 3.11 이상, Swift 컴파일러와 설치된 [Ghost-ALICE](https://github.com/AidALL/ghost-alice)가 필요합니다. 아래의 `SECRETARY_GHOST_ROOT`는 실제 Ghost-ALICE 소스 체크아웃 경로로 설정하세요.

```sh
export SECRETARY_GHOST_ROOT="$HOME/ghost-alice"
git clone https://github.com/AidALL/ghost-alice-ai-secretary.git "$HOME/ghost-alice-ai-secretary"
cd "$HOME/ghost-alice-ai-secretary"
./scripts/install.sh --ghost-alice-root "$SECRETARY_GHOST_ROOT"
python3 scripts/installer.py status
```

이 인스톨러는 스킬, 비서 프로그램, 네이티브 도구, OpenClaw `2026.9.8`과 Node `24.21.0`을 전용 설치 경로에 함께 구성합니다. 기본 프로그램 경로는 `~/.local/share/ai-secretary`, 스킬 경로는 `~/.agents/skills/ai-secretary`, 기본 등록 기록은 `~/.ghost-alice/addons/ai-secretary.json`입니다. `--prefix`, `--skills-dir`, `--registry-dir`로 경로를 지정할 수 있고, `--claude`는 Claude 스킬 경로도 추가합니다. 설치가 백그라운드 서비스를 자동 시작하지는 않습니다.

코어의 플랫폼별 등록 경로를 직접 선택하려면 `--platform-registry codex=/absolute/dir`와 `--platform-registry claude=/absolute/dir`를 반복하고, 해당 `--skills-dir`도 함께 지정합니다. 이 옵션은 `--registry-dir`와 함께 사용하지 않습니다. 여러 플랫폼의 스킬과 등록 기록은 한 공유 실행 환경의 동일한 소유권 트랜잭션에 포함됩니다. 이미 설치된 등록 경로 집합을 바꾸려면 원래 경로 옵션으로 제거한 뒤 재설치합니다. 작업·대화 상태는 보존됩니다.

Ghost-ALICE 코어의 공식 `ai-secretary` 별칭은 별도 코어 통합 PR에서 준비 중입니다. 해당 변경이 병합되어 배포되기 전에는 위의 애드온 자체 인스톨러를 사용하세요. 기존 코어의 일반 `--addon-source` 설치는 스킬만 구성하므로 이 전체 런타임 설치를 대신하지 않습니다.

최상위 OpenClaw 버전·npm 무결성과 Node 버전·배포 아카이브 체크섬을 검사합니다. 현재 네트워크 설치는 npm 전이 의존성을 설치 시점에 해석하므로 전체 의존성 집합을 재현하는 릴리스 잠금은 아닙니다. `--openclaw-source`와 `--node-source`는 검증 가능한 고정 버전 캐시를 개발 검증에 재사용하는 옵션입니다.

## 제거와 데이터 보존

```sh
./scripts/uninstall.sh
```

사용자 지정 경로로 설치했다면 제거에도 같은 경로 옵션을 전달하세요. 제거는 설치 원장이 소유한 변경되지 않은 프로그램·스킬·등록 기록을 대상으로 합니다. 사용자 수정, 기존 미소유 리소스, 작업 원장, OpenClaw 대화와 모델 설정 등 상태 데이터는 보존됩니다. 등록 기록이 변경되었으면 실행 파일·스킬을 지우기 전에 제거를 중단합니다. 부분 제거에는 소유권 원장과 재시도 명령을 유지하며, 보존된 파일은 결과에 표시합니다. 재설치에는 잠금·스테이징·롤백·중단 복구가 적용됩니다.

공식 코어 경로로 여러 플랫폼에 설치한 비서는 한 공유 번들입니다. 등록된 플랫폼 중 하나에서 애드온 제거를 요청하면 그 번들과 모든 소유 플랫폼 등록·스킬을 함께 제거합니다. 다른 애드온과 개인 작업 데이터는 해당 제거 대상에 포함되지 않습니다.

## 로컬 실행

기본 설치 경로의 실행기를 PATH에 추가합니다. 사용자 지정 prefix를 사용했다면 해당 경로로 바꾸세요.

```sh
export PATH="$HOME/.local/share/ai-secretary/managed/bin:$PATH"
ai-secretary doctor
openclaw gateway run --bind loopback
```

게이트웨이를 실행한 상태에서 다른 터미널의 같은 PATH를 설정하고 사용자 요청을 실행합니다.

```sh
ai-secretary run --runtime openclaw '위챗 첨부파일을 내려받고 저장 폴더를 열어 줘'
```

`doctor`는 접근성·화면 녹화 권한을 보고합니다. 권한이 없으면 macOS 시스템 설정에서 실행 프로그램의 권한을 허용해야 합니다. 현재 실행 백엔드는 OpenClaw 하나입니다. 개발 시 `run --runtime-executable /absolute/path/to/openclaw`로 실행기를 지정할 수 있으며, 기존 `--openclaw` 옵션도 호환 별칭입니다.

`run`은 사용자가 직접 넘긴 원래 요청을 Ghost-ALICE에 등록하고 현재 판정을 확인한 뒤 명시적인 세션 경로·UUID를 런타임에 전달합니다. 일반 후속 대화는 같은 대화와 TaskId를 유지하고 `run --new-task`는 별도 작업을 시작합니다. 기존 설치의 대화 UUID와 승인 원장은 유지됩니다.

고정 OpenClaw CLI의 확인된 경로는 SDK의 현재 요청·admission ID·provenance를 일부 전달하지 않았으므로 이 사용자 실행기를 사용합니다. 해당 필드를 전달하는 호스트에서는 플러그인이 실제 네이티브 사실을 사용할 수 있습니다. 조립된 프롬프트·이전 대화·도구 인자·받은 문서는 새 사용자 요청으로 간주하지 않습니다.

직접 도구 호출이나 MCP를 사용할 때도 같은 대화에 먼저 실제 사용자 작업을 등록해야 합니다.

```sh
ai-secretary --session agent:main:secretary task '다운로드 폴더의 파일을 확인해 줘'
ai-secretary --session agent:main:secretary invoke secretary_files --args-json '{"path":"/absolute/path/to/downloads"}'
```

네이티브 sessionId가 있는 대화를 이어 사용할 때는 `sessionKey::sessionId` 식별자를 `--session`에 지정합니다. OpenClaw `/new`·`/reset`은 네이티브 세션 식별자를 통해 이전 작업과 분리합니다. 오래된 세션을 사용자 실행기 없이 재사용하는 경로는 현재 요청 연결을 보장하지 않습니다. 클라이언트 중단만으로 게이트웨이 작업이 중단되었다고 판단하지 말고, 게이트웨이 중지 제어와 효과 확인을 사용하세요.

## 모델과 개인정보 경로

기본 구성은 온프레미스 Ollama `http://127.0.0.1:11434`와 `qwen3-vl:8b`를 사용합니다. 모델 서버와 모델 가중치는 별도로 준비해야 합니다. [Ollama 설치 안내](https://ollama.com/download/mac)와 [모델 정보](https://ollama.com/library/qwen3-vl)를 참고하세요. 설치·프로토콜 검증만으로 실제 모델이 연결되어 추론한다고 주장하지 않습니다.

기존 사내 Ollama 서버를 사용하면 전용 상태 설정의 provider baseUrl과 모델 ID를 실제 온프레미스 주소로 변경하세요. 개인 메시지·첨부파일·화면을 외부 모델, 채널 또는 텔레메트리 경로로 전달하는 구성을 사용하지 않습니다. 기본 실행 프로필은 Secretary 도구만 허용합니다.

공통 모델 정책은 `src/ai_secretary/resources/model-policy.json`이며 현재 Ollama 제공자 경로를 허용합니다. 제공자 이름만으로 실제 서버의 사내 위치나 네트워크 격리를 증명하지는 않습니다. MCP를 호출하는 다른 호스트의 모델과 네트워크 경로도 별도로 확인해야 합니다.

## 발신 승인과 효과 재시도

발신 승인은 사용자에게 받습니다. 계정·수신자·본문·첨부파일 내용 해시·초대 세부사항을 `secretary_draft`로 저장하고 사용자에게 보여 줍니다. 사용자가 로컬 터미널에서 저장된 초안과 해시를 확인한 뒤 승인합니다.

```sh
ai-secretary approve reply-17 --digest ACTUAL_DRAFT_DIGEST
```

발신 내용이 바뀌면 승인이 무효화됩니다. 실제 작성창의 내용과 승인된 초안이 같은지도 확인해야 합니다. 일반적인 컴퓨터 조작 권한만으로 메시지·답장·초대 발신을 허용하지 않습니다. 업무 범위 안의 읽기, 다운로드, 폴더 열기와 작성 준비에는 클릭마다 재승인을 요구하지 않습니다.

OperationId는 현재 작업과 실제 효과에 묶입니다. 동일 효과의 완료 결과는 재사용하고, 다른 효과에 같은 ID를 사용하면 거절합니다. 실행이 중단되거나 결과가 불확실하면 `Unknown`으로 기록하여 확인 없이 다시 입력하지 않습니다. OS 입력의 처리 기록은 파일 저장이나 발신 성공까지 증명하지 않으므로 재관찰과 결과 확인이 필요합니다.

## 검증 범위와 남은 작업

코어 계약, 패키지 단독 실행, 프로토콜, 설치·제거, 실제 macOS 자체 테스트 창을 각각 검사하는 코드가 포함되어 있습니다. 일반 단위 테스트는 실제 창 검사를 명시적으로 건너뜁니다. Gateway 스모크는 모델 추론 없이 상태 도구와 실제 Ghost-ALICE 판정을 검사하고, 에이전트 하네스는 로컬 프로토콜 스텁을 사용합니다. 이 검사는 실제 LLM 성능이나 카톡·위챗 첨부파일 처리를 검증하지 않습니다.

발신 버튼 이름 검사는 보조 장치입니다. 모든 앱의 불투명한 GUI 의미, 작성창과 승인 초안의 자동 동등성 증명, 자동 `Unknown` 조정은 아직 구현되지 않았습니다. 모델의 시각 판단과 사용자 승인 규약이 함께 필요합니다. 실제 모델 연결과 지정된 실제 파일을 대상으로 한 앱별 검증이 남아 있습니다.

## 개발 검증

`SECRETARY_GHOST_ROOT`는 설치된 Ghost-ALICE 소스 경로이며 기본값은 `~/ghost-alice`입니다. `SECRETARY_NODE`는 사용할 Node 실행기이며 기본값은 PATH의 `node`입니다. 스모크에서는 설치된 전용 Node를 지정하는 편이 명확합니다.

```sh
export SECRETARY_GHOST_ROOT="$HOME/ghost-alice"
export SECRETARY_NODE="$HOME/.local/share/ai-secretary/managed/node/bin/node"
./scripts/build_native.sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
AI_SECRETARY_NATIVE_BINARY=build/desktop-secretary PYTHONPATH=src python3 -m unittest discover -s tests -p test_native.py -v
```

마지막 명령은 자체 테스트 창을 잠시 표시하여 실제 입력·저장·스크린샷을 검사합니다. 자체 창의 동작은 실제 협업 앱 작업의 증거를 대신하지 않습니다. 모델 추론 없이 설치된 고정 런타임을 검사하려면 다음 명령을 사용할 수 있습니다. 출력은 로컬 임시 경로에만 저장하세요.

```sh
python3 scripts/smoke_openclaw.py --runtime "$HOME/.local/share/ai-secretary/managed/openclaw" --output .tmp/gateway-smoke
python3 scripts/smoke_agent_harness.py --runtime "$HOME/.local/share/ai-secretary/managed/openclaw" --output .tmp/agent-harness --admission-mode user-run
```
