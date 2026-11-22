# -*- coding: utf-8 -*-
"""
Telegram-бот расписания ВГЛТУ.

Возможности:
  1. Ежедневно в 07:00 (МСК) присылает расписание группы (можно отключить в настройках).
  2. Кнопки: «Сегодня», «Завтра», «Неделя».
  3. Настройки: смена группы, включение/выключение авторассылки.
  4. Поиск расписания любой группы по названию.

Команды:
  /start     — показать меню и справку
  /now       — расписание на сегодня
  /tomorrow  — расписание на завтра
  /week      — расписание на неделю
  /find <N>  — найти группу N (например /find ИС2-253-ОБ)
  /settings  — настройки
  /help      — справка

Запуск:  python vgltu_bot.py
Тест:    python vgltu_bot.py --test    (расписание группы GROUP на сегодня)
Разово:  python vgltu_bot.py --once    (отправить сегодняшнее расписание и выйти)
"""

import json
import os
import re
import sys
import time
import threading
import urllib.request
import urllib.parse
import datetime
import html as _html
import socket as _socket

# На некоторых серверах IPv6-маршрут до api.telegram.org не работает,
# из-за чего urllib зависает на чтении ответа («read operation timed out»),
# хотя curl (который идёт по IPv4) работает. Принудительно используем IPv4.
_orig_getaddrinfo = _socket.getaddrinfo


def _ipv4_getaddrinfo(host, *args, **kwargs):
    res = _orig_getaddrinfo(host, *args, **kwargs)
    ipv4 = [r for r in res if r[0] == _socket.AF_INET]
    return ipv4 or res


_socket.getaddrinfo = _ipv4_getaddrinfo

# ----------------------------- НАСТРОЙКИ -----------------------------

TOKEN = os.environ.get("BOT_TOKEN", "9255264310:AAEFEh0QvctBAAWwMRGinjFxWSwSHXxwhsM")   # токен от @BotFather (или из env BOT_TOKEN)
GROUP = "ИС2-252-ОБ"                                      # группа по умолчанию
SEND_HOUR = 7                                             # час авторассылки (МСК)
SEND_MINUTE = 0                                           # минута авторассылки

# Для режима --once (GitHub Actions / планировщик).
CHAT_ID = 8181215522

LIST_URL = "https://kis.vgltu.ru/list?type=group"
SCHEDULE_URL = "https://kis.vgltu.ru/schedule"

# Прокси для Telegram (если запускаете из РФ и Telegram недоступен напрямую).
PROXY = ""

SETTINGS_FILE = "settings.json"

# ------------------------- ВСПОМОГАТЕЛЬНОЕ ---------------------------

def now_msk():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Europe/Moscow"))
    except Exception:
        return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).replace(tzinfo=None)


_proxy_installed = False


def setup_proxy():
    global _proxy_installed
    if _proxy_installed:
        return
    _proxy_installed = True

    proxy = (PROXY or "").strip()
    if not proxy:
        return

    m = re.match(r"^(socks5|socks4|http|https)://(?:([^:@]+):([^@]*)@)?([^:]+):(\d+)/?$", proxy)
    if not m:
        print("[bot] неверный формат PROXY, использую прямое подключение:", proxy)
        return

    scheme = m.group(1).lower()
    user = m.group(2)
    pwd = m.group(3)
    host = m.group(4)
    port = int(m.group(5))

    if scheme in ("socks5", "socks4"):
        try:
            import socks
        except ImportError:
            print("[bot] нет модуля socks.py — SOCKS-прокси недоступен.")
            return
        ptype = socks.SOCKS5 if scheme == "socks5" else socks.SOCKS4
        socks.set_default_proxy(ptype, host, port, rdns=True,
                                username=user, password=pwd)
        socks.wrap_module(urllib.request)
        print("[bot] прокси SOCKS%s: %s:%s" % (scheme[5:], host, port))
    else:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(handler)
        urllib.request.install_opener(opener)
        print("[bot] прокси HTTP: %s:%s" % (host, port))


def http_get_text(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}


def parse_date(text):
    m = re.match(r"(\d{1,2})\s+([А-Яа-яЁё]+)\s+(\d{4})", (text or "").strip())
    if not m:
        return None
    month = MONTHS.get(m.group(2).lower())
    if not month:
        return None
    try:
        return datetime.date(int(m.group(3)), month, int(m.group(1)))
    except ValueError:
        return None


def parse_lesson_cell(inner, current_time):
    txt = re.sub(r"<br\s*/?>", "\n", inner)
    txt = re.sub(r"<a\b[^>]*>", "", txt)
    txt = txt.replace("</a>", "")
    txt = re.sub(r"<[^>]+>", "", txt)
    lines = [l.strip() for l in txt.split("\n") if l.strip()]

    lesson = {"time": current_time, "subject": None, "subgroup": None,
              "group": None, "auditory": None, "teacher": None}
    if not lines:
        return lesson

    lesson["subject"] = lines[0]
    i = 1
    if i < len(lines) and re.match(r"^\d+\s*п\.г\.$", lines[i]):
        lesson["subgroup"] = lines[i]
        i += 1

    if len(lines) - i >= 2:
        lesson["auditory"] = lines[-2]
        lesson["teacher"] = lines[-1]
        group_lines = lines[i:-2]
        lesson["group"] = ", ".join(group_lines) if group_lines else None
    elif len(lines) - i == 1:
        lesson["teacher"] = lines[-1]
    return lesson


def parse_schedule(html):
    import html as html_mod
    html = html_mod.unescape(html)

    days = []
    parts = re.split(r'<div style="margin-bottom[^"]*">', html)
    for part in parts[1:]:
        day = {"date_text": None, "date": None, "weekday": None, "lessons": []}

        dm = re.search(r"<strong>\s*(.*?)\s*</strong>", part, re.S)
        if dm:
            day["date_text"] = dm.group(1).strip()
            day["date"] = parse_date(day["date_text"])

        wm = re.search(r"</strong>\s*</div>\s*<div>\s*(.*?)\s*</div>", part, re.S)
        if wm:
            day["weekday"] = wm.group(1).strip()

        if re.search(r"Нет пар", part):
            days.append(day)
            continue

        current_time = None
        tokens = re.findall(r"<td\b[^>]*>.*?</td>", part, re.S)
        for tok in tokens:
            attrs = re.match(r"<td\b([^>]*)>", tok).group(1)
            inner = re.sub(r"^<td\b[^>]*>", "", tok)
            inner = re.sub(r"</td>$", "", inner)

            if "rowspan" in attrs:
                tm = re.search(r"(\d{1,2}:\d{2}-\d{1,2}:\d{2})", inner)
                if tm:
                    current_time = tm.group(1)
                continue

            if "width:auto" in attrs:
                day["lessons"].append(parse_lesson_cell(inner, current_time))

        days.append(day)

    return days


def fetch_days(group, date_obj):
    """Возвращает все дни, которые отдал сервер, начиная с date_obj."""
    date_str = date_obj.strftime("%Y-%m-%d")
    url = SCHEDULE_URL + "?group=" + urllib.parse.quote(group) + "&date=" + date_str
    html = http_get_text(url)
    return parse_schedule(html)


def fetch_day_schedule(date_obj, group=GROUP):
    days = fetch_days(group, date_obj)
    if not days:
        return None
    for d in days:
        if d.get("date") == date_obj:
            return d
    return days[0]


def esc(s):
    """Экранирует текст для HTML-разметки Telegram."""
    return _html.escape(str(s if s is not None else ""), quote=False)


_PREFIX_RE = re.compile(r"^(лаб|лек|пр|семинар|зач[её]т|экзамен)\.?\s+", re.IGNORECASE)
_TYPE_MAP = {
    "лаб": ("🧪", "Лабораторная"),
    "лек": ("📖", "Лекция"),
    "пр": ("✏️", "Практика"),
    "семинар": ("🗣", "Семинар"),
    "зачёт": ("📝", "Зачёт"),
    "зачет": ("📝", "Зачёт"),
    "экзамен": ("🎓", "Экзамен"),
}


def _lesson_type(subject):
    """Возвращает (эмодзи, тип, чистое название) по префиксу дисциплины."""
    s = (subject or "").strip()
    m = _PREFIX_RE.match(s)
    if m:
        p = m.group(1).lower()
        rest = s[m.end():].strip() or s
        emoji, label = _TYPE_MAP.get(p, ("📚", "Занятие"))
        return emoji, label, rest
    return "📚", "Занятие", s


def _join_header(weekday, date_text):
    weekday = (weekday or "").strip()
    date_text = (date_text or "").strip()
    if weekday and date_text:
        return weekday.capitalize() + ", " + date_text
    return (weekday.capitalize() or date_text or "Расписание")


def format_day(day, html=False):
    weekday = (day.get("weekday") or "").strip()
    date_text = (day.get("date_text") or "").strip()
    header = _join_header(weekday, date_text)
    group_name = GROUP

    if not day.get("lessons"):
        if html:
            return ("📅 <b>" + esc(header) + "</b>\n"
                    "👥 <i>" + esc(group_name) + "</i>\n"
                    "━━━━━━━━━━━━━━━━━━\n\n"
                    "😴 <b>Пар нет — можно отдыхать!</b>")
        return "📚 " + header + "\n\n😴 Пар нет — можно отдыхать!"

    if html:
        out = [
            "📅 <b>" + esc(header) + "</b>",
            "👥 <i>" + esc(group_name) + "</i>",
            "━━━━━━━━━━━━━━━━━━",
        ]
        for L in day["lessons"]:
            if not L.get("subject"):
                continue
            emoji, label, subject = _lesson_type(L["subject"])
            out.append("")
            out.append("🕘 <code>" + esc(L.get("time") or "—") + "</code>")
            line = emoji + " <b>" + esc(subject) + "</b>"
            if L.get("subgroup"):
                line += "  <i>(" + esc(L["subgroup"]) + ")</i>"
            out.append(line)
            details = []
            if L.get("auditory"):
                details.append("🚪 " + esc(L["auditory"]))
            if L.get("teacher"):
                details.append("👤 " + esc(L["teacher"]))
            if details:
                out.append("   " + "  ·  ".join(details))
        return "\n".join(out)

    # обычный текст (для консоли / --test)
    lines = ["📚 " + header, ""]
    for L in day["lessons"]:
        if not L.get("subject"):
            continue
        s = "🕘 " + (L.get("time") or "—") + "\n• " + L["subject"]
        if L.get("subgroup"):
            s += " (" + L["subgroup"] + ")"
        detail = []
        if L.get("auditory"):
            detail.append("ауд. " + L["auditory"])
        if L.get("teacher"):
            detail.append(L["teacher"])
        if detail:
            s += "\n  " + " · ".join(detail)
        lines.append(s)
    return "\n".join(lines)


def format_week(days, html=False):
    """Компактное расписание на несколько дней."""
    blocks = []
    for d in days:
        weekday = (d.get("weekday") or "").strip()
        date_text = (d.get("date_text") or "").strip()
        header = _join_header(weekday, date_text)

        if not d.get("lessons"):
            if html:
                blocks.append("📅 <b>" + esc(header) + "</b>\n😴 Пар нет")
            else:
                blocks.append("📚 " + header + "\n😴 Пар нет")
            continue

        if html:
            blk = ["📅 <b>" + esc(header) + "</b>"]
            for L in d["lessons"]:
                if not L.get("subject"):
                    continue
                emoji, label, subject = _lesson_type(L["subject"])
                s = "🕘 <code>" + esc(L.get("time") or "—") + "</code> " + emoji + " " + esc(subject)
                if L.get("subgroup"):
                    s += " <i>(" + esc(L["subgroup"]) + ")</i>"
                blk.append(s)
                det = []
                if L.get("auditory"):
                    det.append("🚪 " + esc(L["auditory"]))
                if L.get("teacher"):
                    det.append("👤 " + esc(L["teacher"]))
                if det:
                    blk.append("    " + "  ·  ".join(det))
            blocks.append("\n".join(blk))
        else:
            blk = ["📚 " + header]
            for L in d["lessons"]:
                if not L.get("subject"):
                    continue
                s = (L.get("time") or "—") + " · " + L["subject"]
                if L.get("subgroup"):
                    s += " (" + L["subgroup"] + ")"
                detail = []
                if L.get("auditory"):
                    detail.append(L["auditory"])
                if L.get("teacher"):
                    detail.append(L["teacher"])
                if detail:
                    s += " — " + ", ".join(detail)
                blk.append("• " + s)
            blocks.append("\n".join(blk))
    sep = "\n\n━━━━━━━━━━━━━━━━━━\n\n" if html else "\n\n"
    return sep.join(blocks)


def split_text(text, limit=4000):
    if len(text) <= limit:
        return [text]
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts


# ------------------------- СПИСОК ГРУПП И ПОИСК ----------------------

_groups_cache = None


def get_groups():
    global _groups_cache
    if _groups_cache is None:
        _groups_cache = json.loads(http_get_text(LIST_URL))
    return _groups_cache


def find_group(query):
    q = (query or "").strip().upper()
    if not q:
        return [], None
    groups = get_groups()
    for g in groups:
        if g.upper() == q:
            return [g], g
    matches = [g for g in groups if q in g.upper()]
    return matches, None


# ----------------------- НАСТРОЙКИ ПОЛЬЗОВАТЕЛЯ ----------------------

def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def ensure_user(chat_id):
    s = load_settings()
    key = str(chat_id)
    if key not in s:
        s[key] = {"group": GROUP, "auto_send": True}
        save_settings(s)
    return s[key]


def get_user_settings(chat_id):
    s = load_settings()
    key = str(chat_id)
    return s.get(key, {"group": GROUP, "auto_send": True})


def set_user_group(chat_id, group):
    s = load_settings()
    key = str(chat_id)
    entry = s.get(key, {"group": GROUP, "auto_send": True})
    entry["group"] = group
    s[key] = entry
    save_settings(s)


def set_user_auto_send(chat_id, value):
    s = load_settings()
    key = str(chat_id)
    entry = s.get(key, {"group": GROUP, "auto_send": True})
    entry["auto_send"] = bool(value)
    s[key] = entry
    save_settings(s)


# ----------------------------- TELEGRAM ------------------------------

API = "https://api.telegram.org/bot" + TOKEN


def tg_call(method, **params):
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(
        API + "/" + method,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def send_message(chat_id, text, reply_markup=None, parse_mode=None):
    params = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    if parse_mode:
        params["parse_mode"] = parse_mode
    tg_call("sendMessage", **params)


def send_long(chat_id, text, reply_markup=None, parse_mode=None):
    """Отправляет текст частями (если он длиннее лимита Telegram)."""
    parts = split_text(text)
    for i, part in enumerate(parts):
        send_message(
            chat_id,
            part,
            reply_markup if i == len(parts) - 1 else None,
            parse_mode=parse_mode,
        )


def answer_cb(cb_id):
    try:
        tg_call("answerCallbackQuery", callback_query_id=cb_id)
    except Exception:
        pass


# ------------------------------ КНОПКИ -------------------------------

def main_keyboard():
    return {"inline_keyboard": [
        [{"text": "📅 Сегодня", "callback_data": "today"},
         {"text": "📅 Завтра", "callback_data": "tomorrow"}],
        [{"text": "📅 Неделя", "callback_data": "week"}],
        [{"text": "⚙️ Настройки", "callback_data": "settings"}],
    ]}


def settings_keyboard(chat_id):
    us = get_user_settings(chat_id)
    auto_label = "🔔 Авторассылка: ВКЛ" if us.get("auto_send", True) else "🔔 Авторассылка: ВЫКЛ"
    return {"inline_keyboard": [
        [{"text": "👥 Сменить группу", "callback_data": "change_group"}],
        [{"text": auto_label, "callback_data": "toggle_auto"}],
        [{"text": "⬅️ Назад", "callback_data": "back"}],
    ]}


def group_pick_keyboard(groups, prefix="pick_group"):
    """Список групп в виде кнопок (для выбора по нажатию)."""
    # максимум 30 кнопок, чтобы не упереться в лимиты Telegram
    return {"inline_keyboard": [
        [{"text": g, "callback_data": prefix + ":" + g}] for g in groups[:30]
    ]}


HELP_TEXT = (
    "🤖 Бот расписания ВГЛТУ\n\n"
    "Кнопки:\n"
    "📅 Сегодня / 📅 Завтра / 📅 Неделя — расписание\n"
    "⚙️ Настройки — сменить группу или отключить авторассылку\n\n"
    "Команды: /now, /tomorrow, /week, /find <группа>, /settings, /help\n\n"
    "Авторассылка — каждый день в %02d:%02d (МСК)."
) % (SEND_HOUR, SEND_MINUTE)


# Состояние «ожидаем ввод группы» (для кнопки «Сменить группу»).
awaiting_group = {}


def show_menu(chat_id, text="Выберите действие:"):
    send_message(chat_id, text, main_keyboard())


def show_settings(chat_id):
    us = get_user_settings(chat_id)
    auto = "включена ✅" if us.get("auto_send", True) else "выключена ❌"
    text = ("⚙️ Настройки\n\n"
            "Группа: " + us.get("group", GROUP) + "\n"
            "Авторассылка: " + auto)
    send_message(chat_id, text, settings_keyboard(chat_id))


def send_day(chat_id, date_obj, group=None):
    group = group or get_user_settings(chat_id).get("group", GROUP)
    day = fetch_day_schedule(date_obj, group)
    send_long(chat_id, format_day(day, html=True) if day else "Не удалось получить расписание.", main_keyboard(), parse_mode="HTML")


def send_week(chat_id, group=None):
    group = group or get_user_settings(chat_id).get("group", GROUP)
    days = fetch_days(group, now_msk().date())[:7]
    if not days:
        send_message(chat_id, "Не удалось получить расписание.", main_keyboard())
        return
    send_long(chat_id, format_week(days, html=True), main_keyboard(), parse_mode="HTML")


# ------------------------- ОБРАБОТКА СОБЫТИЙ -------------------------

def handle_callback(cb):
    chat_id = cb.get("message", {}).get("chat", {}).get("id")
    data = cb.get("data")
    answer_cb(cb.get("id"))
    if not chat_id:
        return

    ensure_user(chat_id)

    if data == "today":
        send_day(chat_id, now_msk().date())
    elif data == "tomorrow":
        send_day(chat_id, now_msk().date() + datetime.timedelta(days=1))
    elif data == "week":
        send_week(chat_id)
    elif data == "settings":
        show_settings(chat_id)
    elif data == "back":
        show_menu(chat_id)
    elif data == "change_group":
        awaiting_group[chat_id] = True
        send_message(chat_id, "Введи номер группы (например: ИС2-253-ОБ):")
    elif data == "toggle_auto":
        us = get_user_settings(chat_id)
        set_user_auto_send(chat_id, not us.get("auto_send", True))
        show_settings(chat_id)
    elif data.startswith("pick_group:"):
        group = data[len("pick_group:"):]
        set_user_group(chat_id, group)
        send_message(chat_id, "✅ Группа изменена на " + group, settings_keyboard(chat_id))


def handle_update(upd):
    cb = upd.get("callback_query")
    if cb:
        handle_callback(cb)
        return

    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id:
        return

    ensure_user(chat_id)

    if text == "/start":
        show_menu(chat_id, "Привет! Твой chat_id: " + str(chat_id) + "\n\n" + HELP_TEXT)
    elif text == "/now":
        send_day(chat_id, now_msk().date())
    elif text == "/tomorrow":
        send_day(chat_id, now_msk().date() + datetime.timedelta(days=1))
    elif text == "/week":
        send_week(chat_id)
    elif text == "/settings":
        show_settings(chat_id)
    elif text.startswith("/find"):
        query = text[len("/find"):].strip()
        if not query:
            send_message(chat_id, "Введи номер группы: /find ИС2-253-ОБ")
        else:
            search_and_reply(chat_id, query)
    elif text == "/help":
        show_menu(chat_id, HELP_TEXT)
    else:
        # если ожидали ввод группы для смены — сохраняем; иначе считаем поиском
        if awaiting_group.pop(chat_id, False):
            change_group(chat_id, text)
        else:
            search_and_reply(chat_id, text)


def change_group(chat_id, query):
    matches, exact = find_group(query)
    if exact or (len(matches) == 1):
        group = exact or matches[0]
        set_user_group(chat_id, group)
        send_message(chat_id, "✅ Группа изменена на " + group, settings_keyboard(chat_id))
    elif len(matches) > 1:
        send_message(
            chat_id,
            "Найдено несколько групп — выбери нужную кнопкой:",
            group_pick_keyboard(matches),
        )
    else:
        send_message(chat_id, "Группа «" + query + "» не найдена. Проверь номер и попробуй ещё раз.")


def search_and_reply(chat_id, query):
    matches, exact = find_group(query)
    try:
        if exact or (len(matches) == 1):
            group = exact or matches[0]
            day = fetch_day_schedule(now_msk().date(), group)
            send_long(chat_id, format_day(day, html=True) if day else "Не удалось получить расписание для группы " + group, main_keyboard(), parse_mode="HTML")
        elif len(matches) > 1:
            top = "\n".join("• " + g for g in matches[:15])
            send_message(chat_id, "Найдено несколько групп:\n" + top + "\n\nУточни номер (например: " + matches[0] + ")")
        else:
            send_message(chat_id, "Группа «" + query + "» не найдена. Проверь номер и попробуй ещё раз.")
    except Exception as e:
        send_message(chat_id, "Ошибка при получении расписания: " + str(e))


# ------------------------- ФОНОВАЯ ОТПРАВКА --------------------------

def scheduler_loop():
    last_sent = None
    while True:
        try:
            now = now_msk()
            today_str = now.strftime("%Y-%m-%d")
            if now.hour >= SEND_HOUR and last_sent != today_str:
                settings = load_settings()
                if not settings and CHAT_ID:
                    settings[str(CHAT_ID)] = {"group": GROUP, "auto_send": True}
                for key, entry in settings.items():
                    if entry.get("auto_send", True):
                        try:
                            day = fetch_day_schedule(now.date(), entry.get("group", GROUP))
                            send_message(int(key), format_day(day, html=True) if day else "Не удалось получить расписание.", parse_mode="HTML")
                            print("[bot] расписание отправлено", key, now.strftime("%Y-%m-%d %H:%M"))
                        except Exception as e:
                            print("[bot] ошибка отправки", key, ":", e)
                last_sent = today_str
        except Exception as e:
            print("[bot] ошибка планировщика:", e)
        time.sleep(20)


# ------------------------------ MAIN --------------------------------

def send_today_once():
    chat_id = CHAT_ID or load_chat_id()
    if not chat_id:
        print("[bot] ОШИБКА: chat_id не задан. Укажите CHAT_ID в vgltu_bot.py.")
        sys.exit(1)
    day = fetch_day_schedule(now_msk().date(), GROUP)
    text = format_day(day, html=True) if day else "Не удалось получить расписание."
    send_message(chat_id, text, parse_mode="HTML")
    print("[bot] расписание отправлено (--once) на chat_id", chat_id)


def load_chat_id():
    try:
        with open("chat_id.txt", "r", encoding="utf-8") as f:
            v = f.read().strip()
            return int(v) if v else None
    except Exception:
        return None


def run_bot():
    setup_proxy()
    print("[bot] старт. Группа по умолчанию:", GROUP, "| авторассылка в %02d:%02d МСК" % (SEND_HOUR, SEND_MINUTE))

    threading.Thread(target=scheduler_loop, daemon=True).start()

    offset = 0
    while True:
        try:
            res = tg_call("getUpdates", offset=offset, timeout=25)
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                handle_update(upd)
        except Exception as e:
            print("[bot] ошибка polling:", e)
            time.sleep(3)


if __name__ == "__main__":
    if "--test" in sys.argv:
        setup_proxy()
        try:
            day = fetch_day_schedule(now_msk().date(), GROUP)
            print(format_day(day) if day else "Пустой ответ от сервера.")
        except Exception as e:
            print("Ошибка:", e)
    elif "--once" in sys.argv:
        setup_proxy()
        send_today_once()
    else:
        run_bot()
