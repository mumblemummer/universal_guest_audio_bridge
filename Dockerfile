ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    DEBIAN_FRONTEND=noninteractive

# 리눅스 오디오, mDNS, 스트리밍 데몬 및 필수 유틸리티 설치
RUN apt-get update && apt-get install -y --no-install-recommends \
    dbus \
    avahi-daemon \
    avahi-utils \
    libnss-mdns \
    shairport-sync \
    mpd \
    upmpdcli \
    snapserver \
    supervisor \
    gettext-base \
    jq \
    curl \
    procps \
    net-tools \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Python 백엔드 종속성 설치 (PEP 668 대비 --break-system-packages 사용)
RUN pip3 install --no-cache-dir --break-system-packages \
    fastapi \
    "uvicorn[standard]" \
    aiohttp \
    jinja2 \
    python-multipart

# 필요한 런타임 디렉터리 및 권한 준비
RUN mkdir -p /run/dbus /var/run/dbus /var/run/avahi-daemon /var/log/supervisor /data \
    && chown -R messagebus:messagebus /run/dbus /var/run/dbus \
    && chown -R avahi:avahi /var/run/avahi-daemon

# 루트 파일시스템 복사
COPY rootfs /

# 실행 권한 설정
RUN chmod +x /usr/local/bin/run.sh

# 포트 안내 (host_network: true로 동작하므로 호스트와 직접 공유됨)
# 8099: Ingress Web UI
# 1704: Snapcast TCP Stream
# 1705: Snapcast Control
# 1780: Snapcast HTTP Web & Stream
# 5000/7000: AirPlay RTSP
# 6600: MPD
# 49152+: UPnP / DLNA
EXPOSE 8099 1704 1705 1780 5000 6600

WORKDIR /app

CMD ["/usr/local/bin/run.sh"]
