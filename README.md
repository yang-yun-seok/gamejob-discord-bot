# Gamejob Discord Bot

게임잡 공고를 주기적으로 확인해서, 새 공고를 디스코드 채널로 자동 전송하는 저장소입니다.

## 이 저장소가 하는 일

- 내가 원하는 조건의 게임잡 공고 목록을 감시합니다.
- 새 공고가 발견되면 디스코드 웹훅으로 보냅니다.
- 이미 보낸 공고는 `sent_jobs.txt`에 기록해서 다시 보내지 않습니다.

## 처음에 해야 할 설정

### 1. 디스코드 웹훅 등록

GitHub 저장소에서 아래로 이동합니다.

`Settings -> Secrets and variables -> Actions -> Secrets`

추가할 항목:

- `DISCORD_WEBHOOK_URL`

### 2. 감시할 게임잡 링크 등록

GitHub 저장소에서 아래로 이동합니다.

`Settings -> Secrets and variables -> Actions -> Variables`

추가할 항목:

- `GAMEJOB_TARGET_URL`

예시:

```text
https://www.gamejob.co.kr/Recruit/joblist?menucode=duty&dutyCtgr=1&duty=9&career_stat=0,2&career=1_3
```

위 예시는 게임기획 + 신입 + 1~3년 + 경력무관 조건입니다.

## 꼭 켜야 하는 GitHub 설정

`Settings -> Actions -> General`

아래 항목이 필요합니다.

- Actions 사용 허용
- `Workflow permissions` = `Read and write permissions`

이 권한이 있어야 봇이 실행 후 `sent_jobs.txt`를 다시 저장소에 커밋할 수 있습니다.

## 어떻게 실행되나

이 저장소는 아래 경우에 자동으로 실행됩니다.

- 매시간 1번
- `Actions` 탭에서 수동 실행
- `main.py`, `requirements.txt`, 워크플로 파일이 `main`에 푸시될 때

## 처음 실행하면

처음 실행 시 현재 검색 조건에 걸리는 공고들을 디스코드로 보냅니다.

그 다음부터는:

1. 새 공고만 확인
2. 디스코드 전송
3. 전송한 공고 번호를 `sent_jobs.txt`에 저장
4. 다음 실행 때 중복 전송 방지

## 링크는 어떻게 만들면 되나

1. 게임잡에서 원하는 조건으로 검색합니다.
2. 결과 목록 페이지가 보이는 상태에서 주소창 URL을 복사합니다.
3. 그 URL 전체를 `GAMEJOB_TARGET_URL`에 넣습니다.

주의:

- 공고 상세 페이지가 아니라 공고 목록 페이지여야 합니다.
- 페이지 2, 3 링크보다는 첫 페이지 기준 URL이 안전합니다.

## 문제가 있을 때

### 디스코드에 메시지가 안 오는 경우

- `DISCORD_WEBHOOK_URL`이 맞는지 확인
- `GAMEJOB_TARGET_URL`이 공고 목록 페이지인지 확인
- `Actions` 탭에서 최근 실행 로그 확인

### 같은 공고가 또 오는 경우

- `Workflow permissions`가 `Read and write permissions`인지 확인
- 실행 후 `sent_jobs.txt`가 자동 커밋되는지 확인

### 현재 공고를 다시 한 번 전송하고 싶은 경우

- `sent_jobs.txt` 내용을 비우고 다시 실행하면 됩니다.

## 파일 설명

- `main.py`: 크롤링과 디스코드 전송 로직
- `sent_jobs.txt`: 이미 보낸 공고 목록
- `.github/workflows/gamejob-discord-bot.yml`: GitHub Actions 실행 설정
