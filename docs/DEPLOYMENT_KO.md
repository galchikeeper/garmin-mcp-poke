# 가민 연결 복구 배포 절차

이 변경은 이전 Garth 인증을 DI 인증으로 교체합니다. 기존
`GARMINTOKENS_BASE64`는 새 인증에 사용할 수 없습니다. 파일 보존과 새 토큰을
준비하기 전에 배포하면 `/health`는 응답하지만 운동 조회는 설정 오류를 반환합니다.

## 현재 작업으로 바뀌는 부분

- 시작·상태 확인 시 가민에 로그인하지 않습니다.
- 저장된 DI 토큰만 복원합니다. 서버에 비밀번호를 넣어 자동 재로그인하지 않습니다.
- 갱신된 토큰을 즉시 파일에 저장합니다. 저장 실패 시 후속 조회를 중단합니다.
- 429 대기 종료 시각과 실패 횟수도 저장합니다. 재시작해도 유지됩니다.
- 동일한 저장 경로를 쓰는 프로세스 간 요청·갱신을 잠금으로 직렬화합니다.
- 인증 거절 시 새 토큰 파일로 교체하기 전까지 자동 시도를 중단합니다.
- MCP 도구 오류가 성공처럼 표시되지 않도록 `isError`로 전달합니다.

가민이 제한을 해제하는 시각이나 공유 IP의 영향을 제어하지는 못합니다.
30/60/120분은 이 서버의 대기 정책이며 가민의 해제 시간을 의미하지 않습니다.

## 배포 전 준비

### 1. Render 파일 보존 설정 (비용 발생 — 사용자 승인 후)

기존 `garmin-mcp-server` 서비스를 선택합니다. 새 서비스를 만들 필요는 없습니다.

- 왼쪽 **Compute**에서 디스크를 사용할 수 있는 유료 인스턴스로 변경합니다.
- **Disks**에서 영구 디스크를 추가합니다. Mount path: `/var/data`, Size: `1 GB`.
- 요금은 변경 직전 Render에 표시되는 금액을 확인합니다. 이 저장소의 변경만으로
  요금제를 업그레이드하지 않습니다. `render.yaml`은 무료 플랜을 유지하며,
  **무료 플랜만으로 이 버전의 영구 저장 요건을 충족할 수 없습니다.**
- 서비스는 단일 인스턴스로 유지합니다. 별도 디스크를 쓰는 다른 서버와는 대기 상태를 공유하지 않습니다.

공식 설명: https://render.com/docs/disks 및 https://render.com/docs/free

### 2. Mac에서 새 토큰 만들기 (비밀번호와 인증 코드는 채팅에 보내지 않기)

이 저장소에서 Python 3.12 환경으로 다음 명령을 실행합니다.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/generate_tokens.py
```

본인이 터미널에 가민 이메일, 비밀번호, 인증 코드를 입력합니다.
비밀번호는 화면에 보이지 않습니다. 생성 결과는 `~/.garmin-mcp/garmin_tokens.json`에
권한 0600으로 저장됩니다. 토큰 내용은 터미널이나 채팅에 출력하지 않습니다.

429가 나오면 프로그램이 로컬 대기 상태를 기록합니다. 다시 실행하더라도
대기 중에는 로그인하지 않습니다. 한 번에 한 가지 로그인 방식만 사용합니다.
로컬 컴퓨터와 기존 Render 서버는 대기 상태를 공유하지 않으므로 양쪽에서
동시에 인증을 반복하지 않습니다.

### 3. Render에 토큰과 저장 경로 지정

**Environment → Secret Files → Add file**:

- Filename: `garmin_tokens.json`
- Contents: 위에서 만든 파일의 내용 (Render 입력란에만 입력)

**Environment → Environment Variables → Edit**:

| KEY | VALUE |
|---|---|
| `GARMIN_STATE_DIR` | `/var/data/garmin` |
| `GARMIN_TOKEN_SOURCE` | `/etc/secrets/garmin_tokens.json` |
| `PYTHON_VERSION` | `3.12.14` |

기존 `GARMINTOKENS_BASE64`, `GARMIN_EMAIL`, `GARMIN_PASSWORD`는 이 버전에서
읽지 않습니다. 복구 확인 전까지 기존 값을 따로 노출하거나 삭제할 필요는 없습니다.

Secret File은 최초 시드로만 사용합니다. 이미 갱신된 디스크 토큰을
재배포 때 오래된 시드로 덮어쓰지 않습니다.

토큰이 거절되어 **다시 발급한 토큰으로 교체**할 때는 Secret File을 갱신하고,
서비스의 **Shell**에서 다음 도구로 명시적으로 가져옵니다. 진행 중인 요청과
잠금을 공유하고, 429 대기 기간을 지우지 않습니다.

```sh
python scripts/import_tokens.py /etc/secrets/garmin_tokens.json
```

### 4. 코드 반영 및 배포

위 설정이 준비된 뒤 이 변경의 PR을 main에 병합하고,
**Manual Deploy → Deploy latest commit**으로 배포합니다.
기존 설정은 `autoDeploy: false`이므로 코드 업로드만으로 완료되지 않습니다.

### 5. 성공 판정

1. `/health`에 `storage_ready: true`, `retry_after_seconds: 0`이 나오는지 확인합니다.
2. 도구 `get_activities(start=0, limit=1)`을 한 번만 호출합니다.
3. 실제 활동 응답과 오류 없음이 확인돼야 연결 복구 완료입니다.
4. 재시작 후에도 저장된 토큰과 429 대기 상태가 유지되는지 확인합니다.

`/health`의 HTTP 200이나 `storage_ready: true`만으로 인증 성공을 판정하지 않습니다.
토큰은 최초 활동 요청 때 가져오기 때문에 첫 조회 전 `token_file_present: false`일 수 있습니다.

## 롤백

Render의 이전 배포로 롤백할 수 있습니다. 새 토큰 파일과 디스크는 삭제하지 마세요.
이전 배포는 기존 Garth/Base64 방식을 사용하므로 이전의 429 문제가 재발할 수 있습니다.

## 테스트 범위

실제 라이브러리와 FastMCP를 설치한 뒤 HTTP 응답을 모의합니다. 테스트는 가민에
연결하지 않으며 계정 데이터를 읽거나 수정하지 않습니다. 라이브러리 메서드의 존재,
토큰 갱신, 요청 중단, MCP 오류 응답을 검증하지만 실계정·배포 환경에서의 성공을
대체하지는 않습니다.
