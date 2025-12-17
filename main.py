
import os
import re
import asyncio
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger


# ===================== ENV =====================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
AEROAPI_KEY = os.getenv("AEROAPI_KEY")

# ===================== AEROAPI =====================
BASE_URL = "https://aeroapi.flightaware.com/aeroapi"

def aero_get_json(path: str, params: dict | None = None) -> dict:
    """
    FlightAware AeroAPI: auth header x-apikey
    """
    if not AEROAPI_KEY:
        raise RuntimeError("AEROAPI_KEY не задан (проверь .env)")

    headers = {"x-apikey": AEROAPI_KEY}
    r = requests.get(f"{BASE_URL}{path}", headers=headers, params=params, timeout=25)

    # Полезно при 400/401: покажет detail
    if r.status_code >= 400:
        try:
            print("AeroAPI ERROR:", r.status_code, r.text)
        except Exception:
            pass

    r.raise_for_status()
    return r.json()


# ===================== UI =====================
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh")],
        [InlineKeyboardButton(text="🛑 Остановить напоминания", callback_data="stop")],
    ])


# ===================== PARSING =====================
def normalize_flight_number(s: str) -> str | None:
    """
    Принимаем designator: SU123 / BT767 / W6123 и т.п.
    """
    s = (s or "").strip().upper().replace(" ", "").replace("-", "")
    if re.fullmatch(r"[A-Z0-9]{2,3}\d{1,5}", s):
        return s
    return None

def parse_date_ddmmyyyy(s: str) -> datetime | None:
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", (s or "").strip())
    if not m:
        return None
    dd, mm, yyyy = map(int, m.groups())
    try:
        # 00:00 выбранной даты в UTC (для подбора рейса)
        return datetime(yyyy, mm, dd, tzinfo=timezone.utc)
    except ValueError:
        return None

def iso_to_dt_utc(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def nice_dt(iso: str | None) -> str:
    """
    Красивый вывод времени:
    17.12.2025 05:00 UTC
    """
    if not iso:
        return "—"
    dt = iso_to_dt_utc(iso)
    return dt.strftime("%d.%m.%Y %H:%M UTC")


# ===================== FLIGHT LOGIC =====================
def choose_flight_by_date(flights: list[dict], target_date_utc: datetime) -> dict | None:
    """
    Выбираем рейс, у которого scheduled_out/estimated_out ближе всего к 00:00 выбранной даты (UTC).
    """
    best = None
    best_delta = None
    for f in flights:
        so = f.get("scheduled_out") or f.get("estimated_out")
        if not so:
            continue
        t = iso_to_dt_utc(so)
        delta = abs((t - target_date_utc).total_seconds())
        if best is None or delta < best_delta:
            best = f
            best_delta = delta
    return best

def pick_departure_out_utc(f: dict) -> datetime | None:
    """
    Для напоминаний используем OUT:
      estimated_out -> scheduled_out -> estimated_off -> scheduled_off
    """
    for k in ("estimated_out", "scheduled_out", "estimated_off", "scheduled_off"):
        v = f.get(k)
        if v:
            return iso_to_dt_utc(v)
    return None


# ===================== MESSAGE STYLE =====================
def format_flight_message(f: dict) -> str:
    """
    Читабельная карточка (как утвердили):
    - Рейс / авиакомпания / статус / тип
    - Вылет и прилёт с План/Ожид/Факт
    - Терминал/гейт одной строкой
    - Без codeshare
    """
    origin = f.get("origin", {}) or {}
    dest = f.get("destination", {}) or {}

    flight_iata = f.get("ident_iata") or f.get("ident") or "—"

    # авиакомпания: в AeroAPI оператор может быть кодом (BTI/BT). Показываем код, если нет имени.
    airline_code = f.get("operator_iata") or f.get("operator") or "—"
    airline_name = f.get("operator") or ""  # часто ICAO код, но пусть будет
    airline_line = f"{airline_code}" if not airline_name or airline_name == airline_code else f"{airline_name} ({airline_code})"

    status = f.get("status") or "—"
    aircraft_type = f.get("aircraft_type") or "—"

    term_o = f.get("terminal_origin") or "—"
    gate_o = f.get("gate_origin") or "—"
    term_d = f.get("terminal_destination") or "—"
    gate_d = f.get("gate_destination") or "—"

    origin_name = origin.get("name") or "—"
    origin_iata = origin.get("code_iata") or "—"
    dest_name = dest.get("name") or "—"
    dest_iata = dest.get("code_iata") or "—"

    return (
        f"✈️ *Рейс:* `{flight_iata}`\n"
        f"🏷 *Авиакомпания:* {airline_line}\n"
        f"📌 *Статус:* *{status}*\n"
        f"🛩 *Тип самолёта:* `{aircraft_type}`\n\n"

        f"🛫 *Вылет*\n"
        f"• *Аэропорт:* {origin_name} (`{origin_iata}`)\n"
        f"• *План:* {nice_dt(f.get('scheduled_out'))}\n"
        f"• *Ожид:* {nice_dt(f.get('estimated_out'))}\n"
        f"• *Факт:* {nice_dt(f.get('actual_out'))}\n"
        f"• *Терминал / Гейт:* {term_o} / {gate_o}\n\n"

        f"🛬 *Прилёт*\n"
        f"• *Аэропорт:* {dest_name} (`{dest_iata}`)\n"
        f"• *План:* {nice_dt(f.get('scheduled_in'))}\n"
        f"• *Ожид:* {nice_dt(f.get('estimated_in'))}\n"
        f"• *Факт:* {nice_dt(f.get('actual_in'))}\n"
        f"• *Терминал / Гейт:* {term_d} / {gate_d}"
    )


# ===================== REMINDERS =====================
REMINDERS_OFFSETS = [
    ("🧾 Онлайн-регистрация скоро закроется", timedelta(hours=2)),
    ("🧳 Стойки/багаж: лучше быть в аэропорту", timedelta(hours=1)),
    ("🚪 Пора идти к гейту", timedelta(minutes=45)),
    ("✈️ Скоро посадка", timedelta(minutes=30)),
]

scheduler = AsyncIOScheduler()

# В памяти (для MVP). Для продакшена — SQLite/Postgres.
user_jobs: dict[int, list[str]] = {}      # user_id -> job_ids
user_last: dict[int, dict] = {}           # user_id -> {"ident": stable_ident}

async def send_reminder(bot: Bot, user_id: int, text: str):
    await bot.send_message(user_id, text)

def clear_jobs(user_id: int):
    for jid in user_jobs.get(user_id, []):
        try:
            scheduler.remove_job(jid)
        except Exception:
            pass
    user_jobs[user_id] = []

def schedule_reminders(bot: Bot, user_id: int, ident: str, dep_out_utc: datetime):
    clear_jobs(user_id)
    now = datetime.now(timezone.utc)

    for title, offset in REMINDERS_OFFSETS:
        when = dep_out_utc - offset
        if when <= now:
            continue

        jid = f"{user_id}:{ident}:{int(when.timestamp())}"
        text = (
            f"{title}\n"
            f"Рейс {ident}\n"
            f"⏰ {when.strftime('%d.%m.%Y %H:%M')} UTC"
        )

        scheduler.add_job(
            send_reminder,
            trigger=DateTrigger(run_date=when),
            args=[bot, user_id, text],
            id=jid,
            replace_existing=True
        )
        user_jobs.setdefault(user_id, []).append(jid)


# ===================== FSM =====================
class Form(StatesGroup):
    waiting_flight = State()
    waiting_date = State()


# ===================== HANDLERS (НЕТ lambda!) =====================
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Привет!\n\n"
        "Я — бот-помощник по рейсу ✈️\n"
        "Покажу актуальные данные и поставлю напоминания в день поездки:\n"
        "• регистрация / багаж\n"
        "• гейт\n"
        "• посадка\n\n"
        "Как пользоваться:\n"
        "1) Введи номер рейса (пример: *SU123* или *BT767*)\n"
        "2) Введи дату рейса (формат: *ДД.ММ.ГГГГ*)\n"
        "3) Получишь карточку и кнопки для обновления\n\n"
        "Введи номер рейса 👇",
        parse_mode="Markdown"
    )
    await state.set_state(Form.waiting_flight)

async def got_flight(message: Message, state: FSMContext):
    flight = normalize_flight_number(message.text)
    if not flight:
        await message.answer("Не понял номер рейса. Пример: SU123. Введи ещё раз.")
        return

    await state.update_data(flight=flight)
    await message.answer("Ок. Теперь введи дату рейса (ДД.ММ.ГГГГ), например 17.12.2025")
    await state.set_state(Form.waiting_date)

async def got_date(message: Message, state: FSMContext):
    bot = message.bot

    target_date = parse_date_ddmmyyyy(message.text)
    if not target_date:
        await message.answer("Не понял дату. Формат: ДД.ММ.ГГГГ. Введи ещё раз.")
        return

    data = await state.get_data()
    flight = data["flight"]

    # ВАЖНО: держим окно ровно 48 часов (чтобы не ловить 400)
    start_dt = target_date - timedelta(hours=12)
    end_dt = target_date + timedelta(hours=36)
    start_utc = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_utc = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        payload = aero_get_json(
            f"/flights/{flight}",
            params={"start": start_utc, "end": end_utc, "max_pages": 1}
        )
    except requests.HTTPError as e:
        await message.answer(f"HTTP ошибка AeroAPI: {e}")
        await state.clear()
        return
    except Exception as e:
        await message.answer(f"Ошибка AeroAPI: {e}")
        await state.clear()
        return

    flights = payload.get("flights") or []
    if not flights:
        await message.answer("Не нашёл рейсы по этому номеру в выбранном окне. Проверь номер/дату: /start")
        await state.clear()
        return

    chosen = choose_flight_by_date(flights, target_date)
    if not chosen:
        await message.answer("Не смог подобрать рейс по дате. /start")
        await state.clear()
        return

    stable_ident = chosen.get("ident_icao") or chosen.get("ident") or flight
    user_last[message.from_user.id] = {"ident": stable_ident}

    await message.answer(format_flight_message(chosen), reply_markup=kb_main(), parse_mode="Markdown")

    dep_out = pick_departure_out_utc(chosen)
    if dep_out:
        schedule_reminders(bot, message.from_user.id, stable_ident, dep_out)
    else:
        await message.answer("Не смог определить время вылета (OUT) для напоминаний. Нажми 🔄 позже.")

    await state.clear()

async def cb_refresh(call: CallbackQuery):
    bot = call.message.bot
    st = user_last.get(call.from_user.id)

    if not st:
        await call.message.answer("Сначала введи рейс и дату: /start")
        await call.answer()
        return

    ident = st["ident"]

    # Обновляем в окне 48 часов вокруг текущего времени
    now = datetime.now(timezone.utc)
    start_dt = now - timedelta(hours=24)
    end_dt = now + timedelta(hours=24)
    start_utc = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_utc = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        payload = aero_get_json(
            f"/flights/{ident}",
            params={"start": start_utc, "end": end_utc, "max_pages": 1}
        )
    except Exception as e:
        await call.message.answer(f"Ошибка обновления: {e}")
        await call.answer()
        return

    flights = payload.get("flights") or []
    if not flights:
        await call.message.answer("Нет данных по рейсу. /start")
        await call.answer()
        return

    chosen = flights[0]
    await call.message.answer(format_flight_message(chosen), reply_markup=kb_main(), parse_mode="Markdown")

    dep_out = pick_departure_out_utc(chosen)
    if dep_out:
        schedule_reminders(bot, call.from_user.id, ident, dep_out)
    else:
        await call.message.answer("Нет времени OUT — напоминания не обновил.")

    await call.answer()

async def cb_stop(call: CallbackQuery):
    clear_jobs(call.from_user.id)
    await call.message.answer("🛑 Напоминания отключены. Чтобы заново — /start")
    await call.answer()


# ===================== MAIN =====================
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан (проверь .env)")
    if not AEROAPI_KEY:
        raise RuntimeError("AEROAPI_KEY не задан (проверь .env)")

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    dp.message.register(cmd_start, Command("start"))
    dp.message.register(got_flight, Form.waiting_flight)
    dp.message.register(got_date, Form.waiting_date)

    dp.callback_query.register(cb_refresh, F.data == "refresh")
    dp.callback_query.register(cb_stop, F.data == "stop")

    scheduler.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())