# ✈️ FlightRemind

FlightRemind — Telegram-бот для отслеживания рейсов и напоминаний в день перелёта.

## Возможности
- Ввод номера рейса и даты
- Карточка рейса (статус, вылет, прилёт, гейт)
- Напоминания о регистрации, гейте и посадке
- Обновление данных кнопкой

## Установка и запуск

```bash
git clone https://github.com/<username>/FlightRemind.git
cd FlightRemind
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3 main.py
