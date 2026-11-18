#!/usr/bin/env bash
set -e

echo "[Universal Guest Audio Bridge] Starting initialization..."

CONFIG_PATH="/data/options.json"
DATA_DIR="/data"

# 옵션 로드 (기본값 fallback 포함)
if [ -f "$CONFIG_PATH" ]; then
    SPEAKER_NAME=$(jq -r '.speaker_name // "Guest Room Audio"' "$CONFIG_PATH")
    AUDIO_SAMPLE_RATE=$(jq -r '.audio_sample_rate // "44100:16:2"' "$CONFIG_PATH")
else
    SPEAKER_NAME="Guest Room Audio"
    AUDIO_SAMPLE_RATE="44100:16:2"
fi

export SPEAKER_NAME
export AUDIO_SAMPLE_RATE

echo "[Universal Guest Audio Bridge] Speaker Name: ${SPEAKER_NAME}"
echo "[Universal Guest Audio Bridge] Audio Sample Rate: ${AUDIO_SAMPLE_RATE}"
echo "[Universal Guest Audio Bridge] DLNA/UPnP Port: 49152 (Fixed)"

# host_network: true 환경에서 supervisor 도메인 해석 보장
if ! grep -q "supervisor" /etc/hosts 2>/dev/null; then
    echo "172.30.32.2 supervisor" >> /etc/hosts 2>/dev/null || true
fi
export SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN:-}"
export HASSIO_TOKEN="${HASSIO_TOKEN:-}"

# 영구 저장 스피커 목록 파일 초기화
mkdir -p "$DATA_DIR"
if [ ! -f "$DATA_DIR/selected_speakers.json" ]; then
    echo "[]" > "$DATA_DIR/selected_speakers.json"
fi

# D-Bus 및 시스템 런타임 디렉터리 준비
mkdir -p /run/dbus /var/run/dbus /var/run/avahi-daemon /run/mpd /var/lib/mpd/music /var/lib/mpd/playlists /var/lib/snapserver /var/log/supervisor
rm -f /run/dbus/system_bus_socket /run/dbus/pid /var/run/avahi-daemon/pid /run/mpd/pid /var/run/upmpdcli.pid

# D-Bus 머신 ID 생성 보장
if [ ! -f /var/lib/dbus/machine-id ]; then
    dbus-uuidgen --ensure=/var/lib/dbus/machine-id
fi

# Named FIFO 파이프 생성
echo "[Universal Guest Audio Bridge] Setting up FIFO pipes..."
rm -f /tmp/snapfifo_airplay /tmp/snapfifo_dlna /tmp/snapfifo_snap
mkfifo -m 666 /tmp/snapfifo_airplay
mkfifo -m 666 /tmp/snapfifo_dlna
mkfifo -m 666 /tmp/snapfifo_snap

# 설정 템플릿 환경변수 치환
echo "[Universal Guest Audio Bridge] Generating configuration files from templates..."
envsubst '${SPEAKER_NAME} ${AUDIO_SAMPLE_RATE}' < /etc/shairport-sync.conf.template > /etc/shairport-sync.conf
envsubst '${SPEAKER_NAME} ${AUDIO_SAMPLE_RATE}' < /etc/mpd.conf.template > /etc/mpd.conf
envsubst '${SPEAKER_NAME} ${AUDIO_SAMPLE_RATE}' < /etc/upmpdcli.conf.template > /etc/upmpdcli.conf
envsubst '${SPEAKER_NAME} ${AUDIO_SAMPLE_RATE}' < /etc/snapserver.conf.template > /etc/snapserver.conf

# supervisord 설정 복사 확인
if [ ! -f /etc/supervisord.conf ]; then
    cp /rootfs/etc/supervisord.conf /etc/supervisord.conf 2>/dev/null || true
fi

echo "[Universal Guest Audio Bridge] Initialization complete. Launching Supervisord..."
exec /usr/bin/supervisord -c /etc/supervisord.conf
