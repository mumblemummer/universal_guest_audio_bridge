import asyncio
import json
import logging
import os
import socket
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [BridgeController] %(message)s",
)
logger = logging.getLogger("BridgeController")

app = FastAPI(title="Universal Guest Audio Bridge", docs_url=None, redoc_url=None)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

DATA_DIR = Path("/data")
CONFIG_FILE = DATA_DIR / "selected_speakers.json"
OPTIONS_FILE = DATA_DIR / "options.json"

SUPERVISOR_TOKEN = os.getenv("SUPERVISOR_TOKEN", "")
SUPERVISOR_API_URL = "http://supervisor/core/api"
SNAPSERVER_JSONRPC_URL = "http://127.0.0.1:1780/jsonrpc"

# 전역 런타임 상태
runtime_state = {
    "is_streaming": False,
    "active_source": "None",  # "AirPlay", "DLNA", "None"
    "stream_detected_at": 0.0,
    "last_idle_at": 0.0,
    "host_ip": "127.0.0.1",
    "selected_speakers": [],
    "last_error": None,
}


def get_local_host_ip() -> str:
    """호스트 네트워크에서 외부와 통신 가능한 대표 로컬 IP 주소 반환"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        # 8.8.8.8로 라우팅되는 기본 인터페이스 IP 획득 (실제 연결 없음)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


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
    """Home Assistant Supervisor API 호출 유틸리티"""
    if not SUPERVISOR_TOKEN:
        logger.warning("SUPERVISOR_TOKEN 환경변수가 없습니다. (로컬 디버그 모드)")
        return None

    headers = {
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Content-Type": "application/json",
    }
    url = f"{SUPERVISOR_API_URL}/{endpoint.lstrip('/')}"
    try:
        async with aiohttp.ClientSession() as session:
            if method.upper() == "GET":
                async with session.get(url, headers=headers, timeout=5) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    logger.error(f"HA API GET {url} 에러: {resp.status}")
            elif method.upper() == "POST":
                async with session.post(url, headers=headers, json=payload or {}, timeout=5) as resp:
                    if resp.status in (200, 201):
                        return await resp.json()
                    logger.error(f"HA API POST {url} 에러: {resp.status}")
    except Exception as e:
        logger.error(f"HA API 통신 오류: {e}")
    return None


async def call_ha_service(domain: str, service: str, service_data: dict) -> bool:
    """HA 서비스 호출 (예: media_player/play_media)"""
    res = await call_ha_api(f"services/{domain}/{service}", method="POST", payload=service_data)
    return res is not None


async def get_snapserver_status() -> Optional[dict]:
    """Snapserver JSON-RPC로 모든 스트림 상태 조회"""
    payload = {"jsonrpc": "2.0", "method": "Server.GetStatus", "id": 1}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SNAPSERVER_JSONRPC_URL, json=payload, timeout=2) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("result", {})
    except Exception:
        pass
    return None


async def relay_stream_to_speakers(source_name: str, force: bool = False):
    """선택된 스피커들로 Snapcast 오디오 스트림 재생 명령 전송"""
    speakers = load_selected_speakers()
    if not speakers:
        logger.info("선택된 출력 스피커가 없어 스트림 릴레이를 건너뜁니다.")
        return

    host_ip = runtime_state["host_ip"]
    stream_url = f"http://{host_ip}:1780/stream"
    logger.info(f"[{source_name}] 오디오 신호 감지 -> 선택 스피커({len(speakers)}대)로 스트림 릴레이: {stream_url}")

    # 1. 대상 스피커 전원 켜기
    await call_ha_service("media_player", "turn_on", {"entity_id": speakers})
    await asyncio.sleep(0.3)

    # 2. 재생 명령 전송 (개별 또는 그룹)
    # Music Assistant 전용 플레이어(mass) 또는 표준 media_player 모두 지원
    media_data = {
        "entity_id": speakers,
        "media_content_id": stream_url,
        "media_content_type": "music",
        "extra": {
            "title": f"Guest Audio ({source_name})",
            "thumb": "https://brands.home-assistant.io/shairport_sync/icon.png",
        },
    }

    # Music Assistant 전용 서비스 호출 시도 후 fallback
    success = await call_ha_service("mass", "play_media", media_data)
    if not success:
        # 일반 media_player.play_media로 전송
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
    logger.info(f"오디오 스트림 유입 중단 -> 선택 스피커({len(speakers)}대) 재생 중지")
    await call_ha_service("media_player", "media_stop", {"entity_id": speakers})


async def background_audio_monitor():
    """백그라운드에서 Snapserver 스트림 상태를 실시간 감지하여 자동 릴레이"""
    logger.info("오디오 감지 및 자동 브릿지 백그라운드 태스크 시작")
    idle_counter = 0

    while True:
        try:
            status_data = await get_snapserver_status()
            active_source = "None"
            is_active = False

            if status_data and "server" in status_data:
                streams = status_data.get("server", {}).get("streams", [])
                for stream in streams:
                    stream_id = stream.get("id", "")
                    stream_status = stream.get("status", "idle")
                    if stream_status == "playing":
                        is_active = True
                        active_source = stream_id
                        break

            if is_active:
                idle_counter = 0
                if not runtime_state["is_streaming"] or runtime_state["active_source"] != active_source:
                    runtime_state["is_streaming"] = True
                    runtime_state["active_source"] = active_source
                    await relay_stream_to_speakers(active_source)
            else:
                if runtime_state["is_streaming"]:
                    # 곡 변경 시 끊김 방지를 위한 디바운싱 (3초 유지)
                    idle_counter += 1
                    if idle_counter >= 3:
                        runtime_state["is_streaming"] = False
                        runtime_state["active_source"] = "None"
                        idle_counter = 0
                        await stop_stream_on_speakers()

        except Exception as e:
            logger.error(f"오디오 모니터링 루프 예외: {e}")

        await asyncio.sleep(1.0)


@app.on_event("startup")
async def on_startup():
    runtime_state["host_ip"] = get_local_host_ip()
    runtime_state["selected_speakers"] = load_selected_speakers()
    logger.info(f"Universal Guest Audio Bridge 시작됨 (Host IP: {runtime_state['host_ip']})")
    asyncio.create_task(background_audio_monitor())


# --- API Models ---
class SpeakerSelectionRequest(BaseModel):
    speakers: List[str]


class VolumeRequest(BaseModel):
    entity_id: str
    volume_level: float


# --- Routes ---
@app.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    """HA Ingress 웹 UI 렌더링"""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/status")
async def get_status():
    """현재 스트리밍 상태 및 선택된 스피커 반환"""
    return JSONResponse({
        "is_streaming": runtime_state["is_streaming"],
        "active_source": runtime_state["active_source"],
        "host_ip": runtime_state["host_ip"],
        "stream_url": f"http://{runtime_state['host_ip']}:1780/stream",
        "selected_speakers": load_selected_speakers(),
    })


@app.get("/api/speakers")
async def list_media_players():
    """Home Assistant에서 모든 media_player 엔티티 조회"""
    states = await call_ha_api("states")
    if not states or not isinstance(states, list):
        return JSONResponse({"speakers": []})

    speakers = []
    selected_set = set(load_selected_speakers())

    for state in states:
        entity_id = state.get("entity_id", "")
        if entity_id.startswith("media_player."):
            attributes = state.get("attributes", {})
            speakers.append({
                "entity_id": entity_id,
                "name": attributes.get("friendly_name", entity_id),
                "state": state.get("state", "off"),
                "volume_level": attributes.get("volume_level", 0.5),
                "is_volume_muted": attributes.get("is_volume_muted", False),
                "is_selected": entity_id in selected_set,
                "supported_features": attributes.get("supported_features", 0),
            })

    # 이름 기준 오름차순 정렬
    speakers.sort(key=lambda x: x["name"].lower())
    return JSONResponse({"speakers": speakers})


@app.post("/api/speakers/select")
async def set_selected_speakers(req: SpeakerSelectionRequest):
    """출력 스피커 목록 영구 저장 및 현재 스트리밍 중일 시 즉시 릴레이 갱신"""
    success = save_selected_speakers(req.speakers)
    if not success:
        raise HTTPException(status_code=500, detail="스피커 설정 저장에 실패했습니다.")

    # 스트리밍 중인 상태에서 스피커가 추가/변경되면 즉시 새 스피커로도 스트림 전송
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
