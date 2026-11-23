# -*- coding: utf-8 -*-
"""
Telegram-бот расписания ВГЛТУ.

Возможности:
  1. Ежедневно в 07:00 (МСК) присылает расписание группы (можно отключить в настройках).
  2. Кнопки внизу: Сегодня / Завтра / Неделя / Любой день / Настройки.
  3. Настройки: смена группы, подгруппа (1/2/все), включение/выключение авторассылки.
  4. Поиск расписания любой группы по названию.
  5. В расписании явно указан тип занятия: Лекция / Практика / Лабораторная.

Команды: /start, /now, /tomorrow, /week, /find <группа>, /settings, /help

Запуск:  python main.py
Тест:    python main.py --test    (расписание группы GROUP на сегодня)
Разово:  python main.py --once    (отправить сегодняшнее расписание и выйти)
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

def _get_token():
    """Токен берётся ТОЛЬКО из переменных окружения сервера (не хранится в коде)."""
    for name in ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN", "API_TOKEN", "TOKEN"):
        v = os.environ.get(name)
        if v and v.strip():
            return v.strip()
    return None


TOKEN = _get_token()

GROUP = "ИС2-252-ОБ"                                      # группа по умолчанию
SEND_HOUR = 7                                             # час авторассылки (МСК)
SEND_MINUTE = 0                                           # минута авторассылки
SEND_WINDOW_MIN = 60                                      # окно досылки, минут (после 07:00)

# Для режима --once (GitHub Actions / планировщик).
CHAT_ID = 8181215522

LIST_URL = "https://kis.vgltu.ru/list?type=group"
SCHEDULE_URL = "https://kis.vgltu.ru/schedule"

# Прокси для Telegram (если запускаете из РФ и Telegram недоступен напрямую).
PROXY = ""

SETTINGS_FILE = "settings.json"
LAST_SENT_FILE = "last_sent.txt"

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


def _subgroup_num(lesson):
    """Номер подгруппы занятия (1/2) или None, если занятие общее для всех."""
    m = re.match(r"^(\d+)", (lesson.get("subgroup") or "").strip())
    return int(m.group(1)) if m else None


def filter_lessons(lessons, subgroup=0):
    """Оставляет общие занятия + занятия выбранной подгруппы."""
    if not subgroup:
        return list(lessons)
    out = []
    for L in lessons:
        n = _subgroup_num(L)
        if n is None or n == subgroup:
            out.append(L)
    return out


def subgroup_label(subgroup):
    return ("подгруппа " + str(subgroup)) if subgroup else "все подгруппы"


def format_day(day, html=False, subgroup=0, group_name=None):
    weekday = (day.get("weekday") or "").strip()
    date_text = (day.get("date_text") or "").strip()
    header = _join_header(weekday, date_text)
    group_name = group_name or GROUP
    lessons = filter_lessons(day.get("lessons") or [], subgroup)

    if not lessons:
        if html:
            sub = ("  ·  <i>" + esc(subgroup_label(subgroup)) + "</i>") if subgroup else ""
            return ("📅 <b>" + esc(header) + "</b>\n"
                    "👥 <i>" + esc(group_name) + "</i>" + sub + "\n"
                    "━━━━━━━━━━━━━━━━━━\n\n"
                    "😴 <b>Пар нет — можно отдыхать!</b>")
        return "📚 " + header + "\n\n😴 Пар нет — можно отдыхать!"

    if html:
        sub = ("  ·  <i>" + esc(subgroup_label(subgroup)) + "</i>") if subgroup else ""
        out = [
            "📅 <b>" + esc(header) + "</b>",
            "👥 <i>" + esc(group_name) + "</i>" + sub,
            "━━━━━━━━━━━━━━━━━━",
        ]
        for L in lessons:
            if not L.get("subject"):
                continue
            emoji, label, subject = _lesson_type(L["subject"])
            out.append("")
            out.append("🕘 <code>" + esc(L.get("time") or "—") + "</code>")
            inner = [emoji + " " + esc(label) + " — <b>" + esc(subject) + "</b>"]
            if L.get("subgroup") and not subgroup:
                inner[0] += "  <i>(" + esc(L["subgroup"]) + ")</i>"
            details = []
            if L.get("auditory"):
                details.append("🚪 " + esc(L["auditory"]))
            if L.get("teacher"):
                details.append("👤 " + esc(L["teacher"]))
            if details:
                inner.append("  ·  ".join(details))
            out.append("<blockquote>" + "\n".join(inner) + "</blockquote>")
        return "\n".join(out)

    # обычный текст (для консоли / --test)
    lines = ["📚 " + header, ""]
    for L in lessons:
        if not L.get("subject"):
            continue
        emoji, label, subject = _lesson_type(L["subject"])
        s = "🕘 " + (L.get("time") or "—") + "\n• " + label + " — " + subject
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


def format_week(days, html=False, subgroup=0, group_name=None):
    """Расписание на неделю — список блоков, по одному на день (как «Сегодня»)."""
    return [format_day(d, html=html, subgroup=subgroup, group_name=group_name) for d in days]


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


def _default_entry():
    return {"group": GROUP, "auto_send": True, "subgroup": 0, "subgroup_chosen": False}


def ensure_user(chat_id):
    s = load_settings()
    key = str(chat_id)
    if key not in s:
        s[key] = _default_entry()
        save_settings(s)
    return s[key]


def get_user_settings(chat_id):
    s = load_settings()
    return s.get(str(chat_id), _default_entry())


def is_subgroup_chosen(chat_id):
    return bool(get_user_settings(chat_id).get("subgroup_chosen"))


def update_user(chat_id, **kw):
    s = load_settings()
    key = str(chat_id)
    entry = s.get(key, _default_entry())
    entry.update(kw)
    s[key] = entry
    save_settings(s)


def load_last_sent():
    try:
        with open(LAST_SENT_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


def save_last_sent(date_str):
    try:
        with open(LAST_SENT_FILE, "w", encoding="utf-8") as f:
            f.write(date_str)
    except Exception:
        pass


# ----------------------------- TELEGRAM ------------------------------

API = "https://api.telegram.org/bot" + (TOKEN or "")


def tg_call(method, **params):
    if not TOKEN:
        raise RuntimeError("Токен не задан. Добавьте переменную окружения BOT_TOKEN на сервере.")
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


# ---------------------- КНОПКИ ВНИЗУ ЭКРАНА -------------------------

MAIN_KB = {"keyboard": [
    ["📅 Сегодня", "📅 Завтра"],
    ["📅 Неделя", "🗓 Любой день"],
    ["⚙️ Настройки"],
], "resize_keyboard": True, "is_persistent": True}

SETTINGS_KB = {"keyboard": [
    ["📚 Сменить группу"],
    ["👥 Подгруппа", "🔔 Авторассылка"],
    ["⬅️ Назад"],
], "resize_keyboard": True, "is_persistent": True}

SUBGROUP_KB = {"keyboard": [
    ["1 подгруппа", "2 подгруппа"],
    ["Все подгруппы"],
    ["⬅️ Назад"],
], "resize_keyboard": True, "is_persistent": True}

DAY_KB = {"keyboard": [
    ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
    ["◀️ Прошлая неделя", "Следующая неделя ▶️"],
    ["⬅️ Назад"],
], "resize_keyboard": True, "is_persistent": True}

WEEKDAY_BUTTONS = {"Пн": 0, "Вт": 1, "Ср": 2, "Чт": 3, "Пт": 4, "Сб": 5, "Вс": 6}

HELP_TEXT = (
    "🤖 Бот расписания ВГЛТУ\n\n"
    "Кнопки внизу экрана:\n"
    "📅 Сегодня / 📅 Завтра / 📅 Неделя — расписание\n"
    "🗓 Любой день — выбрать конкретный день недели\n"
    "⚙️ Настройки — группа, подгруппа, авторассылка\n\n"
    "В расписании у каждой пары указан тип: Лекция, Практика, Лабораторная.\n\n"
    "Команды: /now, /tomorrow, /week, /find <группа>, /settings, /help\n"
    "Авторассылка — каждый день в %02d:%02d (МСК)."
) % (SEND_HOUR, SEND_MINUTE)


# Состояние диалога по чатам
screen = {}          # chat_id -> main | settings | subgroup | days
day_week = {}        # chat_id -> понедельник выбранной недели
awaiting_group = {}  # chat_id -> True, если ждём ввод названия группы

# Кто может делать рассылку всем пользователям (твой chat_id)
ADMIN_IDS = {CHAT_ID} if CHAT_ID else set()


def _monday_of(date_obj):
    return date_obj - datetime.timedelta(days=date_obj.weekday())


def show_main(chat_id, text="Выберите действие:"):
    screen[chat_id] = "main"
    send_message(chat_id, text, MAIN_KB)


def show_settings(chat_id):
    us = get_user_settings(chat_id)
    auto = "включена ✅" if us.get("auto_send", True) else "выключена ❌"
    sg = us.get("subgroup", 0)
    sg_text = ("подгруппа " + str(sg)) if sg else "все подгруппы"
    text = ("⚙️ Настройки\n\n"
            "📚 Группа: " + us.get("group", GROUP) + "\n"
            "👥 Подгруппа: " + sg_text + "\n"
            "🔔 Авторассылка: " + auto)
    screen[chat_id] = "settings"
    send_message(chat_id, text, SETTINGS_KB)


def show_subgroup(chat_id, intro="Выбери свою подгруппу:"):
    us = get_user_settings(chat_id)
    cur = us.get("subgroup", 0)
    cur_text = ("сейчас: подгруппа " + str(cur)) if cur else "сейчас: все подгруппы"
    screen[chat_id] = "subgroup"
    send_message(chat_id, intro + "\n" + cur_text, SUBGROUP_KB)


def show_days(chat_id, monday=None):
    if monday is None:
        monday = day_week.get(chat_id) or _monday_of(now_msk().date())
    day_week[chat_id] = monday
    screen[chat_id] = "days"
    end = monday + datetime.timedelta(days=6)
    text = ("🗓 Неделя " + monday.strftime("%d.%m") + " – " + end.strftime("%d.%m") +
            "\nВыбери день недели:")
    send_message(chat_id, text, DAY_KB)


def send_day(chat_id, date_obj, group=None):
    us = get_user_settings(chat_id)
    group = group or us.get("group", GROUP)
    subgroup = us.get("subgroup", 0)
    day = fetch_day_schedule(date_obj, group)
    send_long(chat_id,
              format_day(day, html=True, subgroup=subgroup, group_name=group) if day else "Не удалось получить расписание.",
              MAIN_KB, parse_mode="HTML")


def send_week(chat_id, group=None):
    us = get_user_settings(chat_id)
    group = group or us.get("group", GROUP)
    subgroup = us.get("subgroup", 0)
    days = fetch_days(group, now_msk().date())[:7]
    if not days:
        send_message(chat_id, "Не удалось получить расписание.", MAIN_KB)
        return
    blocks = format_week(days, html=True, subgroup=subgroup, group_name=group)
    for i, text in enumerate(blocks):
        send_long(chat_id, text, MAIN_KB if i == len(blocks) - 1 else None, parse_mode="HTML")


# ------------------------- ОБРАБОТКА СОБЫТИЙ -------------------------

def handle_callback(cb):
    """Старые инлайн-кнопки: просто отвечаем и показываем меню."""
    chat_id = cb.get("message", {}).get("chat", {}).get("id")
    answer_cb(cb.get("id"))
    if chat_id:
        show_main(chat_id, "Кнопки теперь внизу экрана 👇")


def change_group(chat_id, query):
    matches, exact = find_group(query)
    if exact or (len(matches) == 1):
        group = exact or matches[0]
        update_user(chat_id, group=group)
        show_subgroup(chat_id, "✅ Группа изменена на " + group + "\nТеперь выбери свою подгруппу:")
    elif len(matches) > 1:
        top = "\n".join("• " + g for g in matches[:15])
        send_message(chat_id, "Найдено несколько групп:\n" + top +
                     "\n\nНапиши точнее (например: " + matches[0] + ")", MAIN_KB)
    else:
        send_message(chat_id, "Группа «" + query + "» не найдена. Проверь номер и попробуй ещё раз.", MAIN_KB)


def search_and_reply(chat_id, query):
    matches, exact = find_group(query)
    try:
        if exact or (len(matches) == 1):
            group = exact or matches[0]
            us = get_user_settings(chat_id)
            subgroup = us.get("subgroup", 0)
            day = fetch_day_schedule(now_msk().date(), group)
            send_long(chat_id, format_day(day, html=True, subgroup=subgroup, group_name=group) if day
                      else "Не удалось получить расписание для группы " + group, MAIN_KB, parse_mode="HTML")
        elif len(matches) > 1:
            top = "\n".join("• " + g for g in matches[:15])
            send_message(chat_id, "Найдено несколько групп:\n" + top +
                         "\n\nУточни номер (например: " + matches[0] + ")", MAIN_KB)
        else:
            send_message(chat_id, "Группа «" + query + "» не найдена. Проверь номер и попробуй ещё раз.", MAIN_KB)
    except Exception as e:
        send_message(chat_id, "Ошибка при получении расписания: " + str(e), MAIN_KB)


def broadcast_all(chat_id, text):
    """Рассылает сообщение всем пользователям бота (только для админа)."""
    settings = load_settings()
    if CHAT_ID and str(CHAT_ID) not in settings:
        settings[str(CHAT_ID)] = _default_entry()
    recipients = [int(k) for k in settings.keys()]
    ok = fail = 0
    for r in recipients:
        try:
            send_message(r, text, MAIN_KB)
            ok += 1
        except Exception as e:
            fail += 1
            print("[bot] broadcast fail", r, ":", e)
        time.sleep(0.05)  # не превышаем лимиты Telegram (~30 сообщений/сек)
    send_message(chat_id,
                 "📢 Рассылка завершена.\n"
                 "Всего пользователей: " + str(len(recipients)) + "\n"
                 "✅ Доставлено: " + str(ok) + "\n"
                 "❌ Ошибок: " + str(fail),
                 MAIN_KB)


def handle_text(chat_id, text):
    ensure_user(chat_id)

    # первый вход — опрос подгруппы
    if not is_subgroup_chosen(chat_id) and text not in ("1 подгруппа", "2 подгруппа", "Все подгруппы"):
        show_subgroup(chat_id, "👋 Привет! Я бот расписания ВГЛТУ.\nУкажи, в какой ты подгруппе:")
        return

    # команды
    if text == "/start":
        show_main(chat_id, "Привет! Твой chat_id: " + str(chat_id) + "\n\n" + HELP_TEXT)
        return
    if text == "/help":
        show_main(chat_id, HELP_TEXT)
        return
    if text == "/now":
        send_day(chat_id, now_msk().date())
        return
    if text == "/tomorrow":
        send_day(chat_id, now_msk().date() + datetime.timedelta(days=1))
        return
    if text == "/week":
        send_week(chat_id)
        return
    if text == "/settings":
        show_settings(chat_id)
        return
    if text.startswith("/find"):
        q = text[len("/find"):].strip()
        if q:
            search_and_reply(chat_id, q)
        else:
            send_message(chat_id, "Введи номер группы: /find ИС2-253-ОБ", MAIN_KB)
        return
    if text.startswith("/broadcast"):
        if chat_id not in ADMIN_IDS:
            send_message(chat_id, "Команда доступна только администратору.", MAIN_KB)
            return
        msg = text[len("/broadcast"):].strip()
        if not msg:
            send_message(chat_id, "Напиши текст так:\n/broadcast Вышло обновление бота!", MAIN_KB)
            return
        broadcast_all(chat_id, msg)
        return
    if text == "/users":
        if chat_id not in ADMIN_IDS:
            send_message(chat_id, "Команда доступна только администратору.", MAIN_KB)
            return
        send_message(chat_id, "👥 Пользователей в базе: " + str(len(load_settings())), MAIN_KB)
        return

    scr = screen.get(chat_id, "main")

    # ---------- главные кнопки работают всегда, с любого экрана ----------
    MAIN_LABELS = ("📅 Сегодня", "📅 Завтра", "📅 Неделя", "🗓 Любой день", "⚙️ Настройки")
    if text in MAIN_LABELS:
        screen[chat_id] = "main"
        if text == "📅 Сегодня":
            send_day(chat_id, now_msk().date())
        elif text == "📅 Завтра":
            send_day(chat_id, now_msk().date() + datetime.timedelta(days=1))
        elif text == "📅 Неделя":
            send_week(chat_id)
        elif text == "🗓 Любой день":
            show_days(chat_id)
        else:
            show_settings(chat_id)
        return

    # ---------- ожидание ввода названия группы ----------
    if awaiting_group.get(chat_id) and text != "⬅️ Назад":
        awaiting_group.pop(chat_id, False)
        change_group(chat_id, text)
        return

    # ---------- экран «настройки» ----------
    if scr == "settings":
        if text == "📚 Сменить группу":
            awaiting_group[chat_id] = True
            send_message(chat_id, "Введи номер группы (например: ИС2-253-ОБ):", SETTINGS_KB)
        elif text == "👥 Подгруппа":
            show_subgroup(chat_id)
        elif text == "🔔 Авторассылка":
            us = get_user_settings(chat_id)
            new_val = not us.get("auto_send", True)
            update_user(chat_id, auto_send=new_val)
            show_settings(chat_id)
        elif text == "⬅️ Назад":
            awaiting_group.pop(chat_id, False)
            show_main(chat_id)
        else:
            show_settings(chat_id)
        return

    # ---------- экран «подгруппа» ----------
    if scr == "subgroup":
        if text in ("1 подгруппа", "2 подгруппа", "Все подгруппы"):
            n = {"1 подгруппа": 1, "2 подгруппа": 2, "Все подгруппы": 0}[text]
            was_chosen = is_subgroup_chosen(chat_id)
            update_user(chat_id, subgroup=n, subgroup_chosen=True)
            if was_chosen:
                show_settings(chat_id)
            else:
                show_main(chat_id, "✅ Отлично! Твоя подгруппа: " + subgroup_label(n) +
                          "\n\nТеперь буду присылать только твои пары.\n\n" + HELP_TEXT)
        elif text == "⬅️ Назад":
            if is_subgroup_chosen(chat_id):
                show_settings(chat_id)
            else:
                show_subgroup(chat_id)
        else:
            show_subgroup(chat_id)
        return

    # ---------- экран «дни недели» ----------
    if scr == "days":
        if text in WEEKDAY_BUTTONS:
            monday = day_week.get(chat_id) or _monday_of(now_msk().date())
            send_day(chat_id, monday + datetime.timedelta(days=WEEKDAY_BUTTONS[text]))
        elif text == "◀️ Прошлая неделя":
            show_days(chat_id, (day_week.get(chat_id) or _monday_of(now_msk().date())) - datetime.timedelta(days=7))
        elif text == "Следующая неделя ▶️":
            show_days(chat_id, (day_week.get(chat_id) or _monday_of(now_msk().date())) + datetime.timedelta(days=7))
        elif text == "⬅️ Назад":
            show_main(chat_id)
        else:
            show_days(chat_id)
        return

    # ---------- всё остальное ----------
    if awaiting_group.pop(chat_id, False):
        change_group(chat_id, text)
    else:
        search_and_reply(chat_id, text)


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
    if not chat_id or not text:
        return
    handle_text(chat_id, text)


# ------------------------- ФОНОВАЯ ОТПРАВКА --------------------------

_last_sent_memory = None


def scheduler_loop():
    global _last_sent_memory
    print("[bot] планировщик запущен. Рассылка в %02d:%02d МСК" % (SEND_HOUR, SEND_MINUTE))
    while True:
        try:
            now = now_msk()
            today_str = now.strftime("%Y-%m-%d")

            # окно отправки: [07:00, 08:00) — чтобы не слать в случайное время
            start = now.replace(hour=SEND_HOUR, minute=SEND_MINUTE, second=0, microsecond=0)
            end = start + datetime.timedelta(minutes=SEND_WINDOW_MIN)

            last = _last_sent_memory or load_last_sent()
            if start <= now < end and last != today_str:
                sent_any = False
                settings = load_settings()
                if not settings and CHAT_ID:
                    settings[str(CHAT_ID)] = _default_entry()
                for key, entry in settings.items():
                    if not entry.get("auto_send", True):
                        continue
                    try:
                        group = entry.get("group", GROUP)
                        day = fetch_day_schedule(now.date(), group)
                        text = format_day(day, html=True,
                                          subgroup=entry.get("subgroup", 0),
                                          group_name=group) if day else "Не удалось получить расписание."
                        send_message(int(key), text, MAIN_KB, parse_mode="HTML")
                        sent_any = True
                        print("[bot] отправлено", key, now.strftime("%Y-%m-%d %H:%M"))
                    except Exception as e:
                        print("[bot] ошибка отправки", key, ":", e)
                if sent_any:
                    save_last_sent(today_str)
                    _last_sent_memory = today_str
                else:
                    print("[bot] получателей нет — отметку о рассылке не ставим")
        except Exception as e:
            print("[bot] ошибка планировщика:", e)
        time.sleep(20)


# ------------------------------ MAIN --------------------------------

def send_today_once():
    if not TOKEN:
        print("[bot] ОШИБКА: токен не задан. Добавьте переменную окружения BOT_TOKEN на сервере.")
        sys.exit(1)
    chat_id = CHAT_ID
    if not chat_id:
        print("[bot] ОШИБКА: chat_id не задан. Укажите CHAT_ID в main.py.")
        sys.exit(1)
    us = get_user_settings(chat_id)
    day = fetch_day_schedule(now_msk().date(), GROUP)
    text = format_day(day, html=True, subgroup=us.get("subgroup", 0), group_name=GROUP) if day else "Не удалось получить расписание."
    send_message(chat_id, text, MAIN_KB, parse_mode="HTML")
    print("[bot] расписание отправлено (--once) на chat_id", chat_id)


def run_bot():
    if not TOKEN:
        print("[bot] ОШИБКА: токен не задан. Добавьте переменную окружения BOT_TOKEN на сервере "
              "(или в панели хостинга в поле «Bot Token»).")
        sys.exit(1)
    setup_proxy()
    print("[bot] старт. Группа по умолчанию:", GROUP, "| рассылка %02d:%02d МСК" % (SEND_HOUR, SEND_MINUTE))

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
    elif "--broadcast" in sys.argv:
        setup_proxy()
        idx = sys.argv.index("--broadcast")
        msg = " ".join(sys.argv[idx + 1:]).strip()
        if not TOKEN:
            print("[bot] ОШИБКА: токен не задан (BOT_TOKEN).")
            sys.exit(1)
        if not msg:
            print('Укажи текст: python main.py --broadcast "Вышло обновление!"')
            sys.exit(1)
        broadcast_all(CHAT_ID, msg)
    else:
        run_bot()
