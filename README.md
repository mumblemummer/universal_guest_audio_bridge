# Universal Guest Audio Bridge (Home Assistant Custom Add-on)

손님 스마트폰(아이폰/안드로이드)의 유튜브, 멜론, 스포티파이 등 모든 오디오를 Home Assistant 및 Music Assistant(MA)를 통해 **선택한 스피커(예: Edifier ES300 2대 스테레오/그룹)**로 실시간 동기화 중계하는 로컬 커스텀 애드온입니다.

---

## 🌟 주요 기능
1. **iOS (AirPlay 2 / 1 수신):** `shairport-sync` 가상 오디오 렌더러를 통해 아이폰/아이패드 제어 센터에서 바로 스트리밍 수신.
2. **Android (DLNA/UPnP 수신):** `upmpdcli` + `mpd`를 통해 안드로이드 및 윈도우 캐스트 앱(BubbleUPnP, 삼성 뮤직 등) 오디오 수신.
3. **고품질 오디오 파이프 & Snapcast 통합:** 각 입력을 개별 Named Pipe(FIFO)로 수신하여 `snapserver`가 무손실(FLAC/PCM) 실시간 스트림으로 서빙.
4. **HA Ingress 웹 대시보드:** 별도 로그인 없이 HA 사이드바에서 즉시 접근 가능한 반응형 웹 UI 제공.
   - HA 내의 모든 `media_player` 자동 검색.
   - **출력 대상 스피커 다중 선택(체크박스)** (예: 2대의 Edifier ES300 동시 선택).
   - 스피커별 개별 볼륨 슬라이더 및 전체 일시정지/테스트 재생 기능.
   - 영구 설정 저장 (`/data/selected_speakers.json`).
5. **스마트 자동 릴레이:** 손님 폰에서 오디오 재생이 감지되면 자동으로 대상 스피커 전원을 켜고 스트림 재생 시작, 재생 중단 시 자동 일시정지.

---

## 📂 파일 구조
```text
/addons/local/universal_guest_audio_bridge/
├── config.yaml                       # 애드온 메타데이터, Ingress 및 권한 설정
├── build.yaml                        # amd64 및 aarch64 베이스 이미지 설정
├── Dockerfile                        # 오디오/스트리밍 패키지 및 Python 백엔드 빌드
├── supervisord.conf                  # 전체 데몬 프로세스 수명주기 통합 관리
├── README.md                         # 설치 및 연동 가이드
└── rootfs/
    ├── etc/
    │   ├── shairport-sync.conf.template # AirPlay FIFO 파이프 출력 템플릿
    │   ├── mpd.conf.template            # DLNA MPD 오디오 엔진 템플릿
    │   ├── upmpdcli.conf.template       # UPnP/DLNA 프론트엔드 템플릿
    │   ├── snapserver.conf.template     # Snapcast 스트림 서버 템플릿
    │   └── supervisord.conf             # supervisord 설정 복사본
    ├── usr/local/bin/
    │   └── run.sh                       # FIFO 생성, 템플릿 치환 및 데몬 기동 스크립트
    └── app/
        ├── main.py                      # FastAPI 백엔드 (오디오 감지 + HA API 연동)
        └── templates/
            └── index.html               # Tailwind CSS 기반 Ingress 웹 대시보드
```

---

## 🚀 설치 방법

### 1. 애드온 파일 배치
Home Assistant 호스트 또는 Samba/SSH 공유를 통해 `/addons/local/universal_guest_audio_bridge` 디렉터리에 본 리포지토리의 모든 파일과 폴더를 복사합니다.

```bash
# HA OS SSH 터미널 접속 시 예시
mkdir -p /addons/local/universal_guest_audio_bridge
# 본 프로젝트 파일들을 위 디렉터리로 복사
```

### 2. 애드온 저장소 새로고침 및 설치
1. Home Assistant 웹 UI에서 **설정(Settings) > 애드온(Add-ons) > 애드온 스토어(Add-on Store)**로 이동합니다.
2. 우측 상단의 점 3개 메뉴(`⋮`)를 누르고 **저장소 새로고침(Check for updates)**을 클릭합니다.
3. 상단 **로컬 애드온(Local add-ons)** 섹션에 `Universal Guest Audio Bridge`가 나타나면 클릭하여 들어갑니다.
4. **설치(Install)** 버튼을 클릭하여 빌드 및 설치를 진행합니다.

### 3. 애드온 옵션 설정 및 실행
1. 애드온 상세 페이지의 **구성(Configuration)** 탭에서 필요 시 옵션을 조정합니다:
   - `speaker_name`: 손님 스마트폰에 노출될 가상 스피커 이름 (기본값: `"Guest Room Audio"`)
   - `audio_sample_rate`: 오디오 샘플레이트 (기본값: `"44100:16:2"`)
2. **정보(Info)** 탭에서:
   - **시스템 시작 시 실행(Start on boot)**: 활성화
   - **사이드바에 표시(Show in sidebar)**: 활성화
3. **시작(Start)** 버튼을 클릭합니다.
4. **로그(Log)** 탭에서 `dbus`, `avahi`, `snapserver`, `shairport-sync`, `upmpdcli`, `FastAPI`가 정상 기동되었는지 확인합니다.

---

## 🎛️ 사용 방법 (Ingress 웹 UI)

1. Home Assistant 사이드바에서 **Guest Audio** 아이콘을 클릭합니다.
2. **동기화 출력 스피커 선택** 목록에 현재 HA에 등록된 모든 스피커가 나열됩니다.
3. 손님 오디오를 출력할 스피커(예: `media_player.edifier_es300_left`, `media_player.edifier_es300_right` 또는 Music Assistant 그룹)의 체크박스를 클릭하여 활성화합니다.
4. 손님의 스마트폰에서:
   - **아이폰/아이패드:** 화면 우상단을 쓸어내려 제어 센터를 열고, 오디오 카드의 AirPlay 아이콘을 터치한 뒤 `Guest Room Audio`를 선택합니다.
   - **안드로이드:** BubbleUPnP, mconnect Player, 혹은 DLNA 캐스트를 지원하는 음악 앱에서 `Guest Room Audio (DLNA)`를 재생 기기로 선택합니다.
5. 오디오가 유입되는 순간 웹 UI 상단 배지가 **실시간 스트리밍 중**으로 바뀌며 선택된 Edifier ES300 스피커들에서 즉시 소리가 나옵니다.

---

## 🎵 Music Assistant (MA) 연동 팁

### A. Music Assistant 그룹 플레이어 사용 (가장 권장)
- Edifier ES300 2대를 Music Assistant에서 **Sync Group** 또는 **Universal Group**(예: `media_player.edifier_stereo_pair`)으로 묶어둔 경우, 본 애드온 웹 UI에서 **해당 그룹 엔티티 1개만 체크**하십시오.
- Music Assistant의 고유 DSP 및 지연 시간 자동 보정이 적용되어 두 스피커 간의 위상 차이나 딜레이 없이 완벽한 스테레오 동기화가 이뤄집니다.

### B. Snapcast Provider 연동
- 본 애드온은 표준 Snapcast 서버(포트 `1704`)를 내장하고 있습니다.
- Music Assistant 설정 > **플레이어 프로바이더(Player Providers)** > **Snapcast**를 추가하고 서버 IP를 `127.0.0.1`(또는 HA 호스트 IP), 포트 `1704`로 지정하면 MA 내부에서 직접 Snapcast 클라이언트로 제어할 수도 있습니다.

### C. 고정 웹 라디오 URL로 등록
- Snapcast HTTP 스트림 주소: `http://<HA_IP>:1780/stream`
- Music Assistant의 라디오 방송국 추가 메뉴에서 위 URL을 등록해두면 필요할 때 언제든 수동으로 원하는 스피커에서 게스트 스트림을 청취할 수 있습니다.

---

## 🌐 OPNsense 및 공유기 mDNS / 멀티캐스트 체크리스트

손님의 스마트폰이 `Guest_Wi-Fi`나 별도의 IoT VLAN에 접속해 있는 경우, mDNS 패킷이 차단되어 AirPlay/DLNA 스피커가 검색되지 않을 수 있습니다. 아래 네트워크 설정을 확인하세요.

### 1. OPNsense 방화벽 설정
- **mDNS 리피터 (os-mdns-repeater 또는 Avahi 플러그인):**
  - OPNsense > 시스템 > 펌웨어 > 플러그인에서 `os-mdns-repeater` 설치.
  - 서비스 > mDNS Repeater에서 **게스트 Wi-Fi 인터페이스**와 **HA가 위치한 LAN 인터페이스**를 동시에 선택하여 활성화.
- **방화벽 룰 (게스트 VLAN -> HA):**
  - **프로토콜:** UDP / **대상:** `224.0.0.251` (멀티캐스트 IP) / **포트:** `5353` (mDNS) 허용
  - **스트리밍 포트 허용:**
    - AirPlay: TCP `5000`, TCP/UDP `7000`, UDP `6001:6100`
    - DLNA/UPnP: TCP/UDP `1900` (SSDP 디스커버리), TCP `49152` (upmpdcli 고정 수신 포트, 방화벽 허용 대상)
    - Snapcast: TCP `1704`, `1705`, `1780`

### 2. Wi-Fi 공유기 / AP(Access Point) 필수 옵션
- **AP 격리 (Client Isolation) 비활성화:**
  - 게스트 Wi-Fi 설정에서 `AP Isolation` 또는 `Client Isolation`이 켜져 있으면 폰끼리/장비 간 로컬 통신이 완전히 차단됩니다. 반드시 해제하거나 예외 규칙을 두어야 합니다.
- **IGMP Snooping / 멀티캐스트 향상(Multicast Enhancement):**
  - Wi-Fi 멀티캐스트 패킷 손실 방지를 위해 AP 관리자 화면에서 `Enable IGMP Snooping` 또는 `Multicast to Unicast (IGMPv2/v3)`를 활성화합니다.
