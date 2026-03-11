#!/bin/bash
# Oracle Cloud ARM (Ubuntu 22.04) 초기 설정 스크립트
# 사용법: bash deploy/setup.sh
set -e

echo "=== 시스템 업데이트 ==="
sudo apt update && sudo apt upgrade -y

echo "=== Python 3.11 + Nginx 설치 ==="
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev python3-pip \
    nginx certbot python3-certbot-nginx \
    iptables-persistent

echo "=== 프로젝트 디렉토리 설정 ==="
APP_DIR="/opt/dup-monitor"
sudo mkdir -p "$APP_DIR"
sudo chown "$USER:$USER" "$APP_DIR"

# 현재 디렉토리에서 프로젝트 파일 복사 (이미 APP_DIR에 있으면 스킵)
if [ "$(pwd)" != "$APP_DIR" ]; then
    echo "프로젝트 파일을 $APP_DIR 로 복사 중..."
    cp -r . "$APP_DIR/"
fi

cd "$APP_DIR"

echo "=== Python 가상환경 + 의존성 설치 ==="
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "=== Playwright Chromium 설치 ==="
playwright install --with-deps chromium

echo "=== 데이터 디렉토리 생성 ==="
mkdir -p data/db data/pdfs logs

echo "=== systemd 서비스 등록 ==="
sudo cp deploy/dup-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable dup-monitor

echo "=== Nginx 설정 ==="
sudo cp deploy/nginx.conf /etc/nginx/sites-available/dup-monitor
sudo ln -sf /etc/nginx/sites-available/dup-monitor /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl restart nginx

echo "=== 방화벽 설정 ==="
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save

echo ""
echo "=== 설정 완료 ==="
echo "다음 단계:"
echo "  1. .env 파일 편집: nano $APP_DIR/.env"
echo "  2. 서비스 시작: sudo systemctl start dup-monitor"
echo "  3. 상태 확인: sudo systemctl status dup-monitor"
echo "  4. 로그 확인: sudo journalctl -u dup-monitor -f"
echo ""
echo "외부 접속: http://<서버 공인IP>"
