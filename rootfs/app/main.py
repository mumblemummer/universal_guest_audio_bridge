import asyncio
from collections import deque
import json
import logging
import os
from pathlib import Path
import select
import socket
import struct
import threading
import time
from typing import Any, Deque, Dict, List, Optional, Set

import aiohttp
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [BridgeController] %(message)s",
)
logger = logging.getLogger("BridgeController")

app = FastAPI(title="Universal Guest Audio Bridge", docs_url=None, redoc_url=None)

INDEX_HTML_PATH = Path(__file__).parent / "templates" / "index.html"
DATA_DIR = Path("/data")
CONFIG_FILE = DATA_DIR / "selected_speakers.json"
OPTIONS_FILE = DATA_DIR / "options.json"
CUSTOM_TOKEN_FILE = DATA_DIR / "custom_ha_token.txt"

FIFO_AIRPLAY = "/tmp/snapfifo_airplay"
FIFO_DLNA = "/tmp/snapfifo_dlna"
FIFO_SNAP = "/tmp/snapfifo_snap"

SUPERVISOR_TOKEN = os.getenv("SUPERVISOR_TOKEN", "") or os.getenv("HASSIO_TOKEN", "")

# 전역 런타임 상태
runtime_state = {
    "is_streaming": False,
    "active_source": "None",  # "AirPlay", "DLNA", "None"
    "last_audio_ts": 0.0,
    "host_ip": "127.0.0.1",
    "selected_speakers": [],
    "last_error": None,
}

# 오디오 스트림 브로드캐스트 허브 상태
_preroll_buffer: Deque[bytes] = deque(maxlen=25)  # 최근 약 0.5초 PCM 청크 보관
_stream_subscribers: Set[asyncio.Queue] = set()
_main_loop: Optional[asyncio.AbstractEventLoop] = None
_active_api_url: Optional[str] = None

# 50ms 분량의 무음 PCM 프레임 (44100Hz * 2ch * 2bytes * 0.05s = 8820 bytes)
SILENCE_CHUNK = b"\x00" * 8820


def make_wav_header(sample_rate: int = 44100, bits_per_sample: int = 16, channels: int = 2) -> bytes:
    """실시간 라이브 스트리밍용 표준 44바이트 RIFF WAVE 헤더 생성"""
    byte_rate = sample_rate * channels * (bits_per_sample // 8)
    block_align = channels * (bits_per_sample // 8)
    data_size = 0x7FFFFFFF  # 무한 스트림용 최대 길이
    riff_size = data_size + 36
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        riff_size,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM format
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )


WAV_HEADER = make_wav_header(44100, 16, 2)


def _broadcast_pcm_chunk(chunk: bytes):
    """이벤트 루프 스레드에서 모든 연결된 HTTP 스트림 클라이언트로 오디오 청크 전달"""
    dead_queues = []
    for q in _stream_subscribers:
        try:
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(chunk)
        except Exception:
            dead_queues.append(q)
    for q in dead_queues:
        _stream_subscribers.discard(q)


def _fifo_reader_thread():
    """전용 데몬 스레드에서 AirPlay 및 DLNA FIFO 파이프를 Non-blocking으로 읽어 실시간 브로드캐스트"""
    logger.info("FIFO 오디오 허브 리더 스레드 시작")
    fd_airplay = -1
    fd_dlna = -1
    fd_snap = -1

    while True:
        try:
            if fd_airplay < 0 and os.path.exists(FIFO_AIRPLAY):
                fd_airplay = os.open(FIFO_AIRPLAY, os.O_RDWR | os.O_NONBLOCK)
            if fd_dlna < 0 and os.path.exists(FIFO_DLNA):
                fd_dlna = os.open(FIFO_DLNA, os.O_RDWR | os.O_NONBLOCK)
            if fd_snap < 0 and os.path.exists(FIFO_SNAP):
                fd_snap = os.open(FIFO_SNAP, os.O_RDWR | os.O_NONBLOCK)

            read_fds = [fd for fd in (fd_airplay, fd_dlna) if fd >= 0]
            if not read_fds:
                time.sleep(0.5)
                continue

            readable, _, _ = select.select(read_fds, [], [], 0.05)
            got_active_audio = False

            for fd in readable:
                try:
                    chunk = os.read(fd, 8192)
                except BlockingIOError:
                    continue
                except Exception:
                    if fd == fd_airplay:
                        os.close(fd_airplay)
                        fd_airplay = -1
                    elif fd == fd_dlna:
                        os.close(fd_dlna)
                        fd_dlna = -1
                    continue

                if not chunk:
                    continue

                # 무음(전부 0x00)이 아닌 실제 오디오 신호인지 확인
                is_audible = chunk.count(0) < len(chunk)
                if is_audible:
                    got_active_audio = True
                    source_name = "AirPlay" if fd == fd_airplay else "DLNA"
                    runtime_state["last_audio_ts"] = time.monotonic()
                    runtime_state["active_source"] = source_name
                    _preroll_buffer.append(chunk)

                    # Snapserver 파이프로 전달
                    if fd_snap >= 0:
                        try:
                            os.write(fd_snap, chunk)
                        except Exception:
                            pass

                    # HTTP 스트림 구독자들에게 브로드캐스트
                    if _main_loop and _stream_subscribers:
                        _main_loop.call_soon_threadsafe(_broadcast_pcm_chunk, chunk)

            # 파이프 입력이 없는 순간에도 연결된 스트림 클라이언트가 끊기지 않도록 무음 프레임 공급
            if not got_active_audio and _main_loop and _stream_subscribers:
                _main_loop.call_soon_threadsafe(_broadcast_pcm_chunk, SILENCE_CHUNK)

        except Exception as e:
            logger.error(f"FIFO 리더 스레드 오류: {e}")
            time.sleep(0.5)


def get_local_host_ip() -> str:
    """호스트 네트워크에서 외부와 통신 가능한 대표 로컬 IP 주소 반환"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def has_custom_token() -> bool:
    if CUSTOM_TOKEN_FILE.exists():
        try:
            return bool(CUSTOM_TOKEN_FILE.read_text(encoding="utf-8").strip())
        except Exception:
            pass
    return False


def get_active_token() -> str:
    """사용자 등록 커스텀 토큰 또는 Supervisor 토큰 반환"""
    if CUSTOM_TOKEN_FILE.exists():
        try:
            tok = CUSTOM_TOKEN_FILE.read_text(encoding="utf-8").strip()
            if tok:
                return tok
        except Exception:
            pass
    return SUPERVISOR_TOKEN


def load_selected_speakers() -> List[str]:
    """영구 저장소에서 선택된 스피커 엔티티 목록 로드"""
    if not CONFIG_FILE.exists():
        return []
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception as e:
        logger.error(f"스피커 설정 파일 로드 실패: {e}")
    return []


def save_selected_speakers(speakers: List[str]) -> bool:
    """선택된 스피커 엔티티 목록을 /data/selected_speakers.json에 영구 저장"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(speakers, f, ensure_ascii=False, indent=2)
        runtime_state["selected_speakers"] = speakers
        return True
    except Exception as e:
        logger.error(f"스피커 설정 저장 실패: {e}")
        return False


async def call_ha_api(endpoint: str, method: str = "GET", payload: Optional[dict] = None) -> Any:
    """Home Assistant Core / Supervisor API 호출"""
    global _active_api_url
    token = get_active_token()
    if not token:
        msg = "인증 토큰(SUPERVISOR_TOKEN 또는 사용자 지정 토큰)이 없습니다."
        logger.warning(msg)
        runtime_state["last_error"] = msg
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # 사용자 지정 토큰(LLAT)인 경우 로컬 8123 포트를 직접 호출 (Supervisor 프록시는 LLAT를 401 거부함)
    if has_custom_token():
        candidate_urls = ["http://127.0.0.1:8123/api"]
    else:
        base_list = [
            "http://supervisor/core/api",
            "http://172.30.32.2/core/api",
            "http://127.0.0.1:8123/api",
        ]
        candidate_urls = [url for url in base_list if url == _active_api_url] + \
                         [url for url in base_list if url != _active_api_url]

    clean_endpoint = endpoint.lstrip("/")

    async with aiohttp.ClientSession() as session:
        for base_url in candidate_urls:
            url = f"{base_url}/{clean_endpoint}"
            try:
                if method.upper() == "GET":
                    async with session.get(url, headers=headers, timeout=5) as resp:
                        if resp.status == 200:
                            _active_api_url = base_url
                            runtime_state["last_error"] = None
                            return await resp.json()
                        err_text = f"{base_url} 응답 상태: {resp.status}"
                        logger.warning(f"HA API GET {url} 에러: {err_text}")
                        runtime_state["last_error"] = err_text
                elif method.upper() == "POST":
                    async with session.post(url, headers=headers, json=payload or {}, timeout=8) as resp:
                        if resp.status in (200, 201):
                            _active_api_url = base_url
                            runtime_state["last_error"] = None
                            return await resp.json()
                        body_text = await resp.text()
                        err_text = f"{base_url} 응답 상태: {resp.status} ({body_text[:120]})"
                        logger.warning(f"HA API POST {url} 에러: {err_text}")
                        runtime_state["last_error"] = err_text
                        # 400/500은 서버 연결은 되었으나 서비스 파라미터 오류이므로 다른 URL로 재시도하지 않음
                        if resp.status in (400, 422, 500):
                            break
            except Exception as e:
                err_text = f"{base_url} 연결 오류: {str(e)}"
                logger.debug(err_text)
                runtime_state["last_error"] = err_text

    return None


async def call_ha_service(domain: str, service: str, service_data: dict) -> bool:
    """HA 서비스 호출"""
    res = await call_ha_api(f"services/{domain}/{service}", method="POST", payload=service_data)
    return res is not None


def get_stream_url() -> str:
    """스피커들이 접속할 본 애드온의 실시간 WAV 오디오 스트림 URL"""
    return f"http://{runtime_state['host_ip']}:8099/stream.wav"


async def relay_stream_to_speakers(source_name: str, force: bool = False):
    """선택된 스피커들로 실시간 WAV 오디오 스트림 재생 명령 전송"""
    speakers = load_selected_speakers()
    if not speakers:
        logger.info("선택된 출력 스피커가 없어 스트림 릴레이를 건너뜁니다.")
        return

    stream_url = get_stream_url()
    logger.info(f"[{source_name}] 오디오 신호 감지 -> 선택 스피커({speakers})로 스트림 릴레이: {stream_url}")

    # 현재 스피커 상태를 확인하여 'off'이고 TURN_ON(128)을 지원하는 스피커만 켜기 호출
    states = await call_ha_api("states")
    if isinstance(states, list):
        state_map = {s.get("entity_id"): s for s in states}
        off_speakers = []
        for spk in speakers:
            st = state_map.get(spk)
            if st and st.get("state") == "off":
                features = st.get("attributes", {}).get("supported_features", 0)
                if features & 128:  # SUPPORT_TURN_ON
                    off_speakers.append(spk)
        if off_speakers:
            await call_ha_service("media_player", "turn_on", {"entity_id": off_speakers})
            await asyncio.sleep(0.5)

    # 표준 media_player.play_media 호출 (Music Assistant 및 일반 DLNA/Cast 스피커 모두 호환)
    success = await call_ha_service("media_player", "play_media", {
        "entity_id": speakers,
        "media_content_id": stream_url,
        "media_content_type": "audio/wav",
    })
    if not success:
        # fallback: media_content_type = "music"
        await call_ha_service("media_player", "play_media", {
            "entity_id": speakers,
            "media_content_id": stream_url,
            "media_content_type": "music",
        })


async def stop_stream_on_speakers():
    """스트림 종료 시 선택된 스피커 재생 중지"""
    speakers = load_selected_speakers()
    if not speakers:
        return
    logger.info(f"오디오 스트림 유입 중단 -> 선택 스피커({speakers}) 재생 중지")
    await call_ha_service("media_player", "media_stop", {"entity_id": speakers})


async def background_audio_monitor():
    """FIFO 오디오 유입 상태를 감시하여 스피커 재생/중지 자동 트리거"""
    logger.info("오디오 감지 및 자동 브릿지 백그라운드 태스크 시작")

    while True:
        try:
            now = time.monotonic()
            elapsed = now - runtime_state["last_audio_ts"]
            is_active = (runtime_state["last_audio_ts"] > 0) and (elapsed < 1.5)

            if is_active:
                if not runtime_state["is_streaming"]:
                    runtime_state["is_streaming"] = True
                    await relay_stream_to_speakers(runtime_state["active_source"])
            else:
                # 5초 이상 오디오 신호가 없으면 스트림 종료로 판단 (곡 전환 시 끊김 방지)
                if runtime_state["is_streaming"] and elapsed >= 5.0:
                    runtime_state["is_streaming"] = False
                    runtime_state["active_source"] = "None"
                    await stop_stream_on_speakers()

        except Exception as e:
            logger.error(f"오디오 모니터링 루프 예외: {e}")

        await asyncio.sleep(0.5)


@app.on_event("startup")
async def on_startup():
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    runtime_state["host_ip"] = get_local_host_ip()
    runtime_state["selected_speakers"] = load_selected_speakers()
    logger.info(f"Universal Guest Audio Bridge 시작됨 (Host IP: {runtime_state['host_ip']}, Stream URL: {get_stream_url()})")

    t = threading.Thread(target=_fifo_reader_thread, daemon=True)
    t.start()
    asyncio.create_task(background_audio_monitor())


# --- API Models ---
class SpeakerSelectionRequest(BaseModel):
    speakers: List[str]


class VolumeRequest(BaseModel):
    entity_id: str
    volume_level: float


class TokenRequest(BaseModel):
    token: str


# --- Live Audio Stream Endpoints ---
@app.head("/stream")
@app.head("/stream.wav")
async def stream_head():
    """Music Assistant verify_item_uri HEAD 요청 대응"""
    return Response(
        status_code=200,
        headers={
            "Content-Type": "audio/wav",
            "Cache-Control": "no-cache, no-store",
            "Accept-Ranges": "none",
            "icy-name": "Universal Guest Audio Bridge",
        },
    )


@app.get("/stream")
@app.get("/stream.wav")
async def stream_audio(request: Request):
    """스피커 및 Music Assistant가 접속하는 실시간 WAV(PCM) 라이브 스트리밍 엔드포인트"""
    client_ip = request.client.host if request.client else "unknown"
    logger.info(f"오디오 스트림 클라이언트 연결됨: {client_ip}")

    async def wav_generator():
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        _stream_subscribers.add(q)
        try:
            # 1. 표준 44바이트 WAV 헤더 즉시 전송
            yield WAV_HEADER

            # 2. 초기 프리롤 버퍼 즉시 전송 (ffprobe / verify_item_uri 즉시 통과 보장)
            initial_chunks = list(_preroll_buffer)
            initial_bytes = b"".join(initial_chunks)
            if len(initial_bytes) < 32768:
                padding = b"\x00" * (32768 - len(initial_bytes))
                initial_bytes = padding + initial_bytes
            yield initial_bytes

            # 3. 실시간 오디오 청크 연속 스트리밍
            while True:
                if await request.is_disconnected():
                    break
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=1.0)
                    yield chunk
                except asyncio.TimeoutError:
                    yield SILENCE_CHUNK
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"스트림 전송 종료 ({client_ip}): {e}")
        finally:
            _stream_subscribers.discard(q)
            logger.info(f"오디오 스트림 클라이언트 연결 해제됨: {client_ip}")

    return StreamingResponse(
        wav_generator(),
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-cache, no-store",
            "Connection": "keep-alive",
            "Accept-Ranges": "none",
            "icy-name": "Universal Guest Audio Bridge",
        },
    )


# --- Web UI & Control Routes ---
@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
async def index_page(request: Request):
    """HA Ingress 웹 UI 렌더링"""
    if INDEX_HTML_PATH.exists():
        content = INDEX_HTML_PATH.read_text(encoding="utf-8")
        return HTMLResponse(content=content)
    return HTMLResponse(content="<h1>Dashboard index.html not found</h1>", status_code=404)


@app.get("/api/status")
async def get_status():
    """현재 스트리밍 상태 및 선택된 스피커 반환"""
    return JSONResponse({
        "is_streaming": runtime_state["is_streaming"],
        "active_source": runtime_state["active_source"],
        "host_ip": runtime_state["host_ip"],
        "stream_url": get_stream_url(),
        "selected_speakers": load_selected_speakers(),
        "has_token": bool(get_active_token()),
        "active_api_url": _active_api_url,
    })


@app.get("/api/speakers")
async def list_media_players():
    """Home Assistant에서 모든 media_player 엔티티 조회 및 스마트 정렬"""
    token = get_active_token()
    states = await call_ha_api("states")

    debug_info = {
        "token_exists": bool(token),
        "token_len": len(token) if token else 0,
        "active_api_url": _active_api_url,
        "last_error": runtime_state.get("last_error"),
        "total_entities_found": len(states) if isinstance(states, list) else 0,
    }

    if not states or not isinstance(states, list):
        return JSONResponse({
            "speakers": [],
            "debug": debug_info,
        })

    speakers = []
    selected_set = set(load_selected_speakers())
    own_speaker_name = os.getenv("SPEAKER_NAME", "Guest Room Audio").lower()

    for state in states:
        entity_id = state.get("entity_id", "")
        if not entity_id.startswith("media_player."):
            continue

        attributes = state.get("attributes", {})
        friendly_name = attributes.get("friendly_name", entity_id)

        # 자기 자신(가상 수신기)은 오디오 무한 루프 방지를 위해 출력 대상에서 제외
        if "guest_room_audio" in entity_id.lower() or own_speaker_name in friendly_name.lower():
            continue

        st_val = state.get("state", "off")
        is_selected = entity_id in selected_set
        is_available = st_val not in ("unavailable", "unknown")
        is_edifier_or_mass = (
            "edifier" in friendly_name.lower()
            or "edifier" in entity_id.lower()
            or attributes.get("app_id") == "music_assistant"
            or "mass_player_type" in attributes
        )

        speakers.append({
            "entity_id": entity_id,
            "name": friendly_name,
            "state": st_val,
            "volume_level": attributes.get("volume_level", 0.5),
            "is_volume_muted": attributes.get("is_volume_muted", False),
            "is_selected": is_selected,
            "is_available": is_available,
            "is_priority": is_edifier_or_mass,
            "supported_features": attributes.get("supported_features", 0),
        })

    # 정렬 우선순위: 1. 선택됨 -> 2. 사용 가능(Available) -> 3. Edifier/MA 기기 -> 4. 이름순
    speakers.sort(key=lambda x: (
        not x["is_selected"],
        not x["is_available"],
        not x["is_priority"],
        x["name"].lower(),
    ))

    return JSONResponse({
        "speakers": speakers,
        "debug": debug_info,
    })


@app.post("/api/speakers/select")
async def set_selected_speakers(req: SpeakerSelectionRequest):
    """출력 스피커 목록 영구 저장"""
    success = save_selected_speakers(req.speakers)
    if not success:
        raise HTTPException(status_code=500, detail="스피커 설정 저장에 실패했습니다.")

    if runtime_state["is_streaming"] and req.speakers:
        await relay_stream_to_speakers(runtime_state["active_source"], force=True)

    return JSONResponse({
        "success": True,
        "selected_speakers": req.speakers,
    })


@app.post("/api/speakers/volume")
async def set_speaker_volume(req: VolumeRequest):
    """개별 스피커 볼륨 조절"""
    level = max(0.0, min(1.0, req.volume_level))
    success = await call_ha_service("media_player", "volume_set", {
        "entity_id": req.entity_id,
        "volume_level": level,
    })
    return JSONResponse({"success": success})


@app.post("/api/token")
async def save_custom_token(req: TokenRequest):
    """사용자가 직접 입력한 Home Assistant 장기 액세스 토큰 저장"""
    clean_token = req.token.strip()
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if clean_token:
            CUSTOM_TOKEN_FILE.write_text(clean_token, encoding="utf-8")
        elif CUSTOM_TOKEN_FILE.exists():
            CUSTOM_TOKEN_FILE.unlink()
        global _active_api_url
        _active_api_url = None
        return JSONResponse({"success": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/playback/stop")
async def stop_playback():
    """모든 선택 스피커 수동 정지"""
    await stop_stream_on_speakers()
    return JSONResponse({"success": True})


@app.post("/api/playback/play_test")
async def test_playback():
    """선택 스피커로 강제 스트림 연결 테스트"""
    await relay_stream_to_speakers("Test Mode", force=True)
    return JSONResponse({"success": True})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8099, log_level="info")
