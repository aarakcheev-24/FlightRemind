import os, re, asyncio
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext


load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
AEROAPI_KEY = os.getenv("AEROAPI_KEY")
BASE_URL = "https://aeroapi.flightaware.com/aeroapi"


def aero_get(path, params=None):
    if not AEROAPI_KEY:
        raise RuntimeError("AEROAPI_KEY не задан (проверь .env)")
    r = requests.get(
        f"{BASE_URL}{path}",
        headers={"x-apikey": AEROAPI_KEY},
        params=params,
        timeout=25,
    )
    r.raise_for_status()
    return r.json()


def norm_flight(s):
    s = (s or "").strip().upper().replace(" ", "").replace("-", "")
    return s if re.fullmatch(r"[A-Z0-9]{2,3}\d{1,5}", s) else None


def parse_date(s):
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", (s or "").strip())
    if not m:
        return None
    d, mo, y = map(int, m.groups())
    try:
        return datetime(y, mo, d, tzinfo=timezone.utc)
    except ValueError:
        return None


def iso_utc(iso):
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def nice(iso):
    if not iso:
        return "—"
    return iso_utc(iso).strftime("%d.%m.%Y %H:%M UTC")


def pick_by_date(flights, target_utc):
    best, best_delta = None, None
    for f in flights:
        so = f.get("scheduled_out") or f.get("estimated_out")
        if not so:
            continue
        delta = abs((iso_utc(so) - target_utc).total_seconds())
        if best is None or delta < best_delta:
            best, best_delta = f, delta
    return best


def card(f):
    origin = f.get("origin") or {}
    dest = f.get("destination") or {}
    flight = f.get("ident_iata") or f.get("ident") or "—"
    op_iata = f.get("operator_iata") or f.get("operator") or "—"
    op = f.get("operator") or ""
    airline = op_iata if (not op or op == op_iata) else f"{op} ({op_iata})"

    return (
        f"✈️ *Рейс:* `{flight}`\n"
        f"🏷 *Авиакомпания:* {airline}\n"
        f"📌 *Статус:* *{f.get('status') or '—'}*\n"
        f"🛩 *Тип самолёта:* `{f.get('aircraft_type') or '—'}`\n\n"
        f"🛫 *Вылет*\n"
        f"• *Аэропорт:* {(origin.get('name') or '—')} (`{(origin.get('code_iata') or '—')}`)\n"
        f"• *План:* {nice(f.get('scheduled_out'))}\n"
        f"• *Ожид:* {nice(f.get('estimated_out'))}\n"
        f"• *Факт:* {nice(f.get('actual_out'))}\n"
        f"• *Терминал / Гейт:* {(f.get('terminal_origin') or '—')} / {(f.get('gate_origin') or '—')}\n\n"
        f"🛬 *Прилёт*\n"
        f"• *Аэропорт:* {(dest.get('name') or '—')} (`{(dest.get('code_iata') or '—')}`)\n"
        f"• *План:* {nice(f.get('scheduled_in'))}\n"
        f"• *Ожид:* {nice(f.get('estimated_in'))}\n"
        f"• *Факт:* {nice(f.get('actual_in'))}\n"
        f"• *Терминал / Гейт:* {(f.get('terminal_destination') or '—')} / {(f.get('gate_destination') or '—')}"
    )


class Form(StatesGroup):
    flight = State()
    date = State()


user_last = {}  # user_id -> ident


async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Введи номер рейса (например *SU123*), затем дату *ДД.ММ.ГГГГ*.",
        parse_mode="Markdown",
    )
    await state.set_state(Form.flight)


async def got_flight(message: Message, state: FSMContext):
    fl = norm_flight(message.text)
    if not fl:
        await message.answer("Неверный формат. Пример: SU1234. Введи ещё раз.")
        return
    await state.update_data(flight=fl)
    await message.answer("Теперь дату рейса: ДД.ММ.ГГГГ")
    await state.set_state(Form.date)


async def got_date(message: Message, state: FSMContext):
    target = parse_date(message.text)
    if not target:
        await message.answer("Неверная дата. Формат: ДД.ММ.ГГГГ")
        return

    fl = (await state.get_data())["flight"]

    start = (target - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (target + timedelta(hours=36)).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        payload = aero_get(f"/flights/{fl}", params={"start": start, "end": end, "max_pages": 1})
    except Exception as e:
        await message.answer(f"Ошибка AeroAPI: {e}")
        await state.clear()
        return

    flights = payload.get("flights") or []
    chosen = pick_by_date(flights, target) if flights else None
    if not chosen:
        await message.answer("Не нашёл рейс в этом окне. Проверь номер/дату: /start")
        await state.clear()
        return

    ident = chosen.get("ident_icao") or chosen.get("ident") or fl
    user_last[message.from_user.id] = ident

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh")]]
    )
    await message.answer(card(chosen), reply_markup=kb, parse_mode="Markdown")
    await state.clear()


async def cb_refresh(call: CallbackQuery):
    ident = user_last.get(call.from_user.id)
    if not ident:
        await call.message.answer("Сначала введи рейс и дату: /start")
        await call.answer()
        return

    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (now + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        payload = aero_get(f"/flights/{ident}", params={"start": start, "end": end, "max_pages": 1})
    except Exception as e:
        await call.message.answer(f"Ошибка обновления: {e}")
        await call.answer()
        return

    flights = payload.get("flights") or []
    if not flights:
        await call.message.answer("Нет данных по рейсу. /start")
        await call.answer()
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh")]]
    )
    await call.message.answer(card(flights[0]), reply_markup=kb, parse_mode="Markdown")
    await call.answer()


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан (проверь .env)")
    if not AEROAPI_KEY:
        raise RuntimeError("AEROAPI_KEY не задан (проверь .env)")

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    dp.message.register(cmd_start, Command("start"))
    dp.message.register(got_flight, Form.flight)
    dp.message.register(got_date, Form.date)
    dp.callback_query.register(cb_refresh, F.data == "refresh")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
