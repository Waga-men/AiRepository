#!/usr/bin/env bash
# Установка Playerok Tools бота на Ubuntu 24 VPS.
# Запуск:  bash setup_vps.sh
set -e

APP_DIR=/opt/playerok-bot

echo "== 1. Ставим Python и venv =="
apt-get update -y
apt-get install -y python3 python3-venv python3-pip

echo "== 2. Создаём папку =="
mkdir -p "$APP_DIR"

echo "== 3. Виртуальное окружение и зависимости =="
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "== 4. Проверяем .env =="
if [ ! -f "$APP_DIR/.env" ]; then
  echo "Создаю .env — ВПИШИТЕ токен!"
  cat > "$APP_DIR/.env" <<'EOF'
BOT_TOKEN=ВПИШИ_ТОКЕН_СЮДА
OWNER_ID=5848676904
EOF
  echo "!!! Отредактируйте $APP_DIR/.env и впишите BOT_TOKEN, затем повторите запуск сервиса."
  echo "nano $APP_DIR/.env"
  exit 0
fi

echo "== 5. Ставим systemd-сервис =="
cp "$APP_DIR/playerok-bot.service" /etc/systemd/system/playerok-bot.service
systemctl daemon-reload
systemctl enable playerok-bot
systemctl restart playerok-bot

echo ""
echo "Готово! Полезные команды:"
echo "  systemctl status playerok-bot   — статус"
echo "  journalctl -u playerok-bot -f   — логи (смотреть в реальном времени)"
echo "  systemctl restart playerok-bot  — перезапуск"
