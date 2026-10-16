"""
Монитор остатков товаров (StockMonitor) — плагин для FunPay Cardinal v0.1.17+

📦 Следит за остатками товаров в автовыдаче и предупреждает, когда товар заканчивается.

Возможности:
 • Мониторит товарные файлы всех лотов из конфига автовыдачи (storage/products/*.txt).
 • ⚠️ Предупреждение, когда остаток опускается до порога (по умолчанию 5, настраивается
   глобально и отдельно для каждого лота).
 • 🚨 Критическое уведомление, когда товар КОНЧИЛСЯ (0 шт.) — лот скоро будет
   деактивирован автовыключением Кардинала.
 • ✅ Уведомление о пополнении (когда товар закончился и его добавили снова).
 • Без спама: предупреждение отправляется один раз, повторно — только если товар
   поднялся выше порога и снова опустился.
 • 📊 Утренний дайджест (опционально): раз в день сводка «какие лоты пора пополнить».
 • Меню в Telegram (/stock): остатки по всем лотам, пороги, интервал проверки,
   кнопка «Проверить сейчас».

Команды Telegram:
 • /stock — открыть меню плагина.

Установка: положить этот файл в папку plugins/ и перезапустить Кардинал.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B, CallbackQuery, Message
from telebot.apihelper import ApiTelegramException

try:
    from tg_bot import CBT, static_keyboards as skb, utils as tg_utils
except Exception:  # загрузка вне Кардинала
    CBT = None
    skb = None
    tg_utils = None

if TYPE_CHECKING:
    from cardinal import Cardinal

from Utils import cardinal_tools

logger = logging.getLogger("FPC.StockMonitor")

# ══════════════════════════════ МЕТАДАННЫЕ ПЛАГИНА ══════════════════════════════
NAME = "Монитор остатков товаров"
VERSION = "1.0.0"
DESCRIPTION = ("Следит за остатками товаров в автовыдаче: предупреждает, когда товар "
               "заканчивается (порог настраивается для каждого лота), шлет критическое "
               "уведомление при нуле и утренний дайджест. Команда: /stock")
CREDITS = "Qwen (для FunPay Cardinal)"
UUID = "9210cf03-d65c-473b-b155-5da0c3f9e211"
SETTINGS_PAGE = True

# ══════════════════════════════ НАСТРОЙКИ ПЛАГИНА ═══════════════════════════════
PLUGIN_DIR = os.path.join("storage", "plugins", "stock_monitor")
SETTINGS_PATH = os.path.join(PLUGIN_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "default_threshold": 5,   # глобальный порог предупреждения
    "thresholds": {},         # {название лота: порог} — индивидуальные пороги
    "check_interval": 300,    # интервал проверки, сек
    "digest": False,          # ежедневный дайджест вкл/выкл
    "digest_time": "09:00",   # время дайджеста (по часам сервера)
    "last_digest": "",        # дата последнего дайджеста (ГГГГ-ММ-ДД)
    "warned": {},             # {имя файла: "low"|"empty"} — состояние предупреждений (анти-спам)
}
THRESHOLD_RANGE = (1, 100000)
INTERVAL_RANGE = (60, 3600)
PER_PAGE = 10

# ══════════════════════════════ CALLBACK-КОДЫ ═══════════════════════════════════
CB_HOME = "stkm:home"
CB_LOTS = "stkm:lots"          # stkm:lots:{offset}
CB_LOT = "stkm:lot"            # stkm:lot:{hash}:{offset}
CB_CHECK = "stkm:check"        # проверить сейчас
CB_DTHRESH = "stkm:dthresh"    # глобальный порог
CB_LTHRESH = "stkm:lthresh"    # stkm:lthresh:{hash} — порог лота
CB_LTHRESET = "stkm:lthreset"  # stkm:lthreset:{hash} — сброс порога лота
CB_INT = "stkm:int"            # интервал
CB_DIGTOG = "stkm:digtog"      # дайджест вкл/выкл
CB_DIGTIME = "stkm:digtime"    # время дайджеста
CB_NOOP = "stkm:noop"

ST_DTHRESH = "stkm:st:dthresh"
ST_LTHRESH = "stkm:st:lthresh"  # + :{hash}
ST_INT = "stkm:st:int"
ST_DIGTIME = "stkm:st:digtime"

_stop_event = threading.Event()
_settings_lock = threading.Lock()
_cardinal: "Cardinal | None" = None


# ══════════════════════════════ УТИЛИТЫ ═════════════════════════════════════════
def _clamp(value, min_v, max_v):
    return max(min_v, min(max_v, value))


def _load_settings() -> dict:
    with _settings_lock:
        data = {}
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                logger.error("StockMonitor: не удалось прочитать настройки, использую значения по умолчанию.")
                logger.debug("TRACEBACK", exc_info=True)
                data = {}
        settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        if isinstance(data, dict):
            settings.update({k: v for k, v in data.items() if k in settings})
        for key, rng in [("default_threshold", THRESHOLD_RANGE), ("check_interval", INTERVAL_RANGE)]:
            try:
                settings[key] = _clamp(int(settings[key]), *rng)
            except (TypeError, ValueError):
                settings[key] = DEFAULT_SETTINGS[key]
        for key in ("thresholds", "warned"):
            if not isinstance(settings[key], dict):
                settings[key] = {}
        settings["digest"] = bool(settings["digest"])
        if _parse_hhmm(settings["digest_time"]) is None:
            settings["digest_time"] = "09:00"
        settings["last_digest"] = str(settings.get("last_digest") or "")
        return settings


def _save_settings(settings: dict) -> None:
    with _settings_lock:
        try:
            os.makedirs(PLUGIN_DIR, exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error("StockMonitor: не удалось сохранить настройки.")
            logger.debug("TRACEBACK", exc_info=True)


def _parse_hhmm(text: str) -> tuple[int, int] | None:
    text = str(text or "").strip()
    parts = text.replace(".", ":").split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 and parts[1] else 0
    except (ValueError, IndexError):
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


def _short(text: str, limit: int = 40) -> str:
    text = str(text or "").replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _escape(text) -> str:
    if tg_utils:
        return tg_utils.escape(str(text))
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plugin_enabled(crd: "Cardinal") -> bool:
    try:
        return crd.plugins[UUID].enabled
    except Exception:
        return False


def _lot_hash(title: str) -> str:
    return hashlib.md5(str(title).encode("utf-8")).hexdigest()[:10]


def _notify(crd: "Cardinal", text: str) -> None:
    if not crd.telegram:
        return
    kwargs = {}
    if tg_utils:
        kwargs["notification_type"] = tg_utils.NotificationTypes.other
    try:
        crd.telegram.send_notification(text, **kwargs)
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)


# ══════════════════════════════ СКАНИРОВАНИЕ ОСТАТКОВ ═══════════════════════════
def _scan(crd: "Cardinal") -> dict:
    """
    Сканирует конфиг автовыдачи и считает товары в файлах.

    :return: {имя файла: {"count": int, "lots": [названия лотов], "path": str}}
    """
    result: dict = {}
    try:
        sections = crd.AD_CFG.sections()
    except Exception:
        logger.debug("StockMonitor: AD_CFG недоступен.")
        return {}
    for title in sections:
        try:
            fname = crd.AD_CFG[title].get("productsFileName")
        except Exception:
            continue
        if not fname:
            continue
        entry = result.setdefault(fname, {"lots": [], "path": f"storage/products/{fname}", "count": -1})
        if title not in entry["lots"]:
            entry["lots"].append(title)
    for fname, entry in result.items():
        try:
            entry["count"] = cardinal_tools.count_products(entry["path"])
        except Exception:
            logger.error(f"StockMonitor: не удалось посчитать товары в файле {fname}.")
            logger.debug("TRACEBACK", exc_info=True)
            entry["count"] = -1
    return result


def _file_threshold(settings: dict, lots: list) -> int:
    """Порог для файла: максимум из индивидуальных порогов лотов, иначе глобальный."""
    vals = []
    for lot in lots:
        t = settings["thresholds"].get(lot)
        if t is not None:
            try:
                vals.append(int(t))
            except (TypeError, ValueError):
                pass
    return max(vals) if vals else settings["default_threshold"]


def _monitor_tick(crd: "Cardinal", now: datetime | None = None) -> list:
    """
    Один проход мониторинга: предупреждения + дайджест.
    Возвращает список событий [("empty"|"low"|"restock", имена лотов, count, threshold), ...].
    """
    settings = _load_settings()
    now = now or datetime.now()
    files = _scan(crd)
    warned = dict(settings["warned"])
    events = []

    for fname, entry in files.items():
        count = entry["count"]
        if count < 0:  # ошибка чтения
            continue
        th = _file_threshold(settings, entry["lots"])
        names = ", ".join(_escape(i) for i in entry["lots"])
        state = warned.get(fname)
        if count == 0 and state != "empty":
            warned[fname] = "empty"
            events.append(("empty", names, count, th, entry["path"]))
        elif 0 < count <= th and state not in ("low", "empty"):
            warned[fname] = "low"
            events.append(("low", names, count, th, entry["path"]))
        elif count > th and state:
            del warned[fname]
            if state == "empty":
                events.append(("restock", names, count, th, entry["path"]))

    # файлы исчезли из конфига — чистим состояния
    for fname in list(warned):
        if fname not in files:
            del warned[fname]

    settings["warned"] = warned
    _save_settings(settings)

    for kind, names, count, th, path in events:
        if kind == "empty":
            text = (f"🚨 <b>ТОВАР ЗАКОНЧИЛСЯ!</b>\n\n"
                    f"Лот(ы): <code>{names}</code>\n"
                    f"Файл: <code>{_escape(path)}</code>\n"
                    f"Остаток: <b>0</b> шт.\n\n"
                    f"Лот будет деактивирован автовыключением. Срочно пополните товар!")
        elif kind == "low":
            text = (f"⚠️ <b>Товар заканчивается.</b>\n\n"
                    f"Лот(ы): <code>{names}</code>\n"
                    f"Файл: <code>{_escape(path)}</code>\n"
                    f"Остаток: <b>{count}</b> шт. (порог: {th})")
        else:
            text = (f"✅ <b>Товар пополнен.</b>\n\n"
                    f"Лот(ы): <code>{names}</code>\n"
                    f"Файл: <code>{_escape(path)}</code>\n"
                    f"Остаток: <b>{count}</b> шт.")
        logger.info(f"StockMonitor: {kind} — {path} ({count} шт.)")
        _notify(crd, text)

    # ежедневный дайджест
    if settings["digest"]:
        today = now.strftime("%Y-%m-%d")
        hhmm = _parse_hhmm(settings["digest_time"])
        if settings["last_digest"] != today and hhmm and (now.hour, now.minute) >= hhmm:
            low_lines = []
            for fname, entry in files.items():
                if entry["count"] < 0:
                    continue
                th = _file_threshold(settings, entry["lots"])
                if entry["count"] <= th:
                    mark = "🚨" if entry["count"] == 0 else "⚠️"
                    low_lines.append(f"{mark} <code>{_escape(_short(', '.join(entry['lots']), 60))}</code>"
                                     f" — {entry['count']} шт. (порог {th})")
            if low_lines:
                text = (f"📊 <b>Утренняя сводка остатков</b> ({now.strftime('%d.%m.%Y')})\n\n"
                        f"Пора пополнить {len(low_lines)} файл(ов):\n\n" + "\n".join(sorted(low_lines)))
                _notify(crd, text)
                settings = _load_settings()
                settings["last_digest"] = today
                _save_settings(settings)
            else:
                settings = _load_settings()
                settings["last_digest"] = today
                _save_settings(settings)
    return events


def _monitor_loop(crd: "Cardinal") -> None:
    logger.info("StockMonitor: мониторинг остатков запущен.")
    last = 0.0
    while not _stop_event.wait(5):
        try:
            if not _plugin_enabled(crd):
                last = time.time()
                continue
            interval = _load_settings()["check_interval"]
            if time.time() - last < interval:
                continue
            last = time.time()
            _monitor_tick(crd)
        except Exception:
            logger.error("StockMonitor: ошибка в цикле мониторинга.")
            logger.debug("TRACEBACK", exc_info=True)


# ══════════════════════════ TELEGRAM-ИНТЕРФЕЙС ══════════════════════════════════
def _stats(crd: "Cardinal", files: dict | None = None) -> tuple:
    settings = _load_settings()
    files = files if files is not None else _scan(crd)
    total_lots = sum(len(e["lots"]) for e in files.values())
    empty = low = 0
    for fname, entry in files.items():
        if entry["count"] < 0:
            continue
        th = _file_threshold(settings, entry["lots"])
        if entry["count"] == 0:
            empty += 1
        elif entry["count"] <= th:
            low += 1
    return files, total_lots, empty, low


def _home_text(crd: "Cardinal") -> str:
    settings = _load_settings()
    files, total_lots, empty, low = _stats(crd)
    digest = (f"ВКЛ 🟢 ({settings['digest_time']})" if settings["digest"] else "ВЫКЛ 🔴")
    return (f"📦 <b>{_escape(NAME)}</b> <i>v{VERSION}</i>\n\n"
            f"Файлов с товаром: <b>{len(files)}</b> | Лотов: <b>{total_lots}</b>\n"
            f"🚨 Закончился товар: <b>{empty}</b>\n"
            f"⚠️ Ниже порога: <b>{low}</b>\n\n"
            f"Порог по умолчанию: <b>{settings['default_threshold']}</b> шт.\n"
            f"Интервал проверки: <b>{settings['check_interval']}</b> сек.\n"
            f"📊 Утренний дайджест: <b>{digest}</b>")


def _home_kb() -> K:
    settings = _load_settings()
    kb = K(row_width=2)
    kb.add(B("📋 Лоты и остатки", None, f"{CB_LOTS}:0"), B("🔄 Проверить сейчас", None, CB_CHECK))
    kb.add(B(f"⚠️ Порог: {settings['default_threshold']} шт.", None, CB_DTHRESH))
    kb.add(B(f"⏱ Интервал: {settings['check_interval']}с", None, CB_INT))
    kb.add(B(f"📊 Дайджест: {'ВКЛ' if settings['digest'] else 'ВЫКЛ'}", None, CB_DIGTOG))
    kb.add(B(f"🕐 Время дайджеста: {settings['digest_time']}", None, CB_DIGTIME))
    if CBT:
        kb.add(B("🔙 Назад к плагину", None, f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
    return kb


def _sorted_files(crd: "Cardinal", settings: dict) -> list:
    """[(fname, entry, threshold), ...] отсортировано по остатку (меньше — выше)."""
    files = _scan(crd)
    items = []
    for fname, entry in files.items():
        th = _file_threshold(settings, entry["lots"])
        items.append((fname, entry, th))
    items.sort(key=lambda x: (x[1]["count"] if x[1]["count"] >= 0 else 10 ** 9))
    return items


def _lots_kb(crd: "Cardinal", settings: dict, offset: int, items: list | None = None) -> K:
    items = items if items is not None else _sorted_files(crd, settings)
    kb = K(row_width=1)
    page = items[offset:offset + PER_PAGE]
    for fname, entry, th in page:
        count = entry["count"]
        if count < 0:
            mark = "❓"
        elif count == 0:
            mark = "🚨"
        elif count <= th:
            mark = "⚠️"
        else:
            mark = "✅"
        title = _short(entry["lots"][0], 38) if entry["lots"] else fname
        suffix = f" +{len(entry['lots']) - 1}" if len(entry["lots"]) > 1 else ""
        kb.add(B(f"{mark} {title}{suffix} ({count})", None, f"{CB_LOT}:{_lot_hash(fname)}:{offset}"))
    pages = max(1, -(-len(items) // PER_PAGE))
    if pages > 1:
        row = []
        if offset > 0:
            row.append(B("⬅️", None, f"{CB_LOTS}:{offset - PER_PAGE}"))
        row.append(B(f"{offset // PER_PAGE + 1}/{pages}", None, CB_NOOP))
        if offset + PER_PAGE < len(items):
            row.append(B("➡️", None, f"{CB_LOTS}:{offset + PER_PAGE}"))
        kb.row(*row)
    kb.add(B("🏠 Главное меню", None, CB_HOME))
    return kb


def _find_file_by_hash(crd: "Cardinal", h: str):
    for fname, entry in _scan(crd).items():
        if _lot_hash(fname) == h:
            return fname, entry
    return None, None


def _lot_text(fname: str, entry: dict, settings: dict) -> str:
    count = entry["count"]
    th = _file_threshold(settings, entry["lots"])
    mark = "🚨" if count == 0 else ("⚠️" if count <= th else "✅")
    lots = "\n".join(f"• <code>{_escape(i)}</code>" for i in entry["lots"])
    per_lot = ""
    for lot in entry["lots"]:
        t = settings["thresholds"].get(lot)
        if t is not None:
            per_lot += f"\nИндивидуальный порог «{_escape(_short(lot, 40))}»: <b>{t}</b>"
    return (f"{mark} <b>{_escape(_short(entry['lots'][0] if entry['lots'] else fname, 60))}</b>\n\n"
            f"Файл: <code>{_escape(entry['path'])}</code>\n"
            f"Остаток: <b>{count}</b> шт.\n"
            f"Порог предупреждения: <b>{th}</b> шт.\n\n"
            f"Лоты, использующие файл:\n{lots}{per_lot}")


def _lot_kb(fname: str, entry: dict, settings: dict, offset: int) -> K:
    h = _lot_hash(fname)
    kb = K(row_width=2)
    kb.add(B("✏️ Порог для лота", None, f"{CB_LTHRESH}:{h}"))
    if any(settings["thresholds"].get(lot) is not None for lot in entry["lots"]):
        kb.add(B("🗑 Сбросить порог лота", None, f"{CB_LTHRESET}:{h}"))
    kb.add(B("🔙 К списку", None, f"{CB_LOTS}:{offset}"))
    return kb


def _answer(bot, c: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    try:
        bot.answer_callback_query(c.id, text, show_alert=alert)
    except Exception:
        pass


def _edit_or_send(bot, c: CallbackQuery, text: str, kb: K | None) -> None:
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.id, parse_mode="HTML", reply_markup=kb)
    except ApiTelegramException as e:
        if "message is not modified" not in str(e):
            try:
                bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                logger.debug("TRACEBACK", exc_info=True)
    except Exception:
        try:
            bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)


def _state_eq(tg, m: Message, state: str) -> bool:
    try:
        return tg.check_state(m.chat.id, m.from_user.id, state)
    except Exception:
        return False


def _state_starts(tg, m: Message, prefix: str) -> bool:
    try:
        st = tg.get_state(m.chat.id, m.from_user.id)
        return bool(st and str(st.get("state", "")).startswith(prefix))
    except Exception:
        return False


def init(crd: "Cardinal", *args) -> None:
    """Инициализация плагина (BIND_TO_PRE_INIT)."""
    global _cardinal
    _cardinal = crd
    _load_settings()

    tg = crd.telegram
    if tg is not None:
        bot = tg.bot

        # ---------- главное меню ----------
        def open_home(c: CallbackQuery) -> None:
            _answer(bot, c)
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        def check_now(c: CallbackQuery) -> None:
            _answer(bot, c, "🔄 Проверяю остатки...")
            _monitor_tick(crd)
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        # ---------- список лотов ----------
        def open_lots(c: CallbackQuery) -> None:
            _answer(bot, c)
            offset = 0
            parts = str(c.data).split(":")
            if len(parts) > 2 and parts[2].isdigit():
                offset = int(parts[2])
            settings = _load_settings()
            items = _sorted_files(crd, settings)
            text = (f"📋 <b>Лоты и остатки</b> ({len(items)} файл(ов))\n\n"
                    f"Отсортировано по остатку — самые пустые сверху:\n"
                    f"🚨 0 шт. | ⚠️ ниже порога | ✅ в норме")
            _edit_or_send(bot, c, text, _lots_kb(crd, settings, offset, items))

        def open_lot(c: CallbackQuery) -> None:
            _answer(bot, c)
            parts = c.data.split(":")
            h = parts[2]
            offset = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            settings = _load_settings()
            fname, entry = _find_file_by_hash(crd, h)
            if entry is None:
                open_lots(c)
                return
            _edit_or_send(bot, c, _lot_text(fname, entry, settings), _lot_kb(fname, entry, settings, offset))

        # ---------- настройки ----------
        def act_edit(c: CallbackQuery, state: str, prompt: str) -> None:
            result = bot.send_message(c.message.chat.id, prompt, parse_mode="HTML",
                                      reply_markup=skb.CLEAR_STATE_BTN() if skb else None)
            tg.set_state(c.message.chat.id, result.id, c.from_user.id, state)
            _answer(bot, c)

        def act_dthresh(c: CallbackQuery) -> None:
            s = _load_settings()
            act_edit(c, ST_DTHRESH, f"⚠️ Введите порог предупреждения по умолчанию (штук, "
                                    f"<code>{THRESHOLD_RANGE[0]}</code> - <code>{THRESHOLD_RANGE[1]}</code>).\n\n"
                                    f"Текущее значение: <b>{s['default_threshold']}</b>.")

        def act_lthresh(c: CallbackQuery) -> None:
            h = c.data.split(":")[2]
            fname, entry = _find_file_by_hash(crd, h)
            if entry is None:
                _answer(bot, c, "Файл не найден. Обновите список.", alert=True)
                return
            settings = _load_settings()
            curr = _file_threshold(settings, entry["lots"])
            lots_txt = _escape(_short(", ".join(entry["lots"]), 100))
            act_edit(c, f"{ST_LTHRESH}:{h}",
                     f"✏️ Введите порог предупреждения для лота(ов):\n<code>{lots_txt}</code>\n\n"
                     f"Текущее значение: <b>{curr}</b>.\n"
                     f"<i>Если файл используют несколько лотов, применится ко всем.</i>")

        def act_lthreset(c: CallbackQuery) -> None:
            h = c.data.split(":")[2]
            fname, entry = _find_file_by_hash(crd, h)
            if entry is None:
                _answer(bot, c, "Файл не найден.", alert=True)
                return
            settings = _load_settings()
            for lot in entry["lots"]:
                settings["thresholds"].pop(lot, None)
            _save_settings(settings)
            _answer(bot, c, "🗑 Индивидуальный порог сброшен (действует глобальный).")
            settings = _load_settings()
            _edit_or_send(bot, c, _lot_text(fname, entry, settings), _lot_kb(fname, entry, settings, 0))

        def act_interval(c: CallbackQuery) -> None:
            s = _load_settings()
            act_edit(c, ST_INT, f"⏱ Введите интервал проверки остатков в секундах "
                                f"(<code>{INTERVAL_RANGE[0]}</code> - <code>{INTERVAL_RANGE[1]}</code>).\n\n"
                                f"Текущее значение: <b>{s['check_interval']}</b> сек.")

        def toggle_digest(c: CallbackQuery) -> None:
            settings = _load_settings()
            settings["digest"] = not settings["digest"]
            _save_settings(settings)
            _answer(bot, c, f"📊 Дайджест: {'ВКЛ' if settings['digest'] else 'ВЫКЛ'}")
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        def act_digtime(c: CallbackQuery) -> None:
            s = _load_settings()
            act_edit(c, ST_DIGTIME, f"🕐 Введите время ежедневного дайджеста в формате ЧЧ:ММ "
                                    f"(по часам сервера).\n\nТекущее значение: <b>{s['digest_time']}</b>.")

        # ---------- обработчики ввода ----------
        def edit_dthresh(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            _edit_int(m, "default_threshold", THRESHOLD_RANGE, "⚠️ Порог по умолчанию")

        def edit_lthresh(m: Message) -> None:
            st = tg.get_state(m.chat.id, m.from_user.id) or {}
            h = str(st.get("state", "")).rsplit(":", 1)[-1]
            tg.clear_state(m.chat.id, m.from_user.id, True)
            try:
                value = _clamp(int(str(m.text).strip()), *THRESHOLD_RANGE)
            except (ValueError, AttributeError):
                bot.reply_to(m, "❌ Нужно ввести число.")
                return
            fname, entry = _find_file_by_hash(crd, h)
            if entry is None:
                bot.reply_to(m, "❌ Файл не найден (конфиг автовыдачи изменился?).")
                return
            settings = _load_settings()
            for lot in entry["lots"]:
                settings["thresholds"][lot] = value
            _save_settings(settings)
            bot.reply_to(m, f"✅ Порог <b>{value}</b> шт. установлен для: "
                            f"<code>{_escape(_short(', '.join(entry['lots']), 80))}</code>",
                         parse_mode="HTML")

        def edit_interval(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            _edit_int(m, "check_interval", INTERVAL_RANGE, "⏱ Интервал проверки")

        def _edit_int(m: Message, key: str, rng: tuple, label: str) -> None:
            try:
                value = _clamp(int(str(m.text).strip()), *rng)
            except (ValueError, AttributeError):
                bot.reply_to(m, "❌ Нужно ввести число. Попробуйте еще раз.")
                return
            settings = _load_settings()
            settings[key] = value
            _save_settings(settings)
            bot.reply_to(m, f"✅ {label}: <b>{value}</b>", parse_mode="HTML")

        def edit_digtime(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            hhmm = _parse_hhmm(str(m.text or ""))
            if hhmm is None:
                bot.reply_to(m, "❌ Не понял время. Введите в формате ЧЧ:ММ, например <code>09:00</code>.",
                             parse_mode="HTML")
                return
            settings = _load_settings()
            settings["digest_time"] = f"{hhmm[0]:02d}:{hhmm[1]:02d}"
            settings["last_digest"] = ""  # даем дайджесту сработать сегодня в новое время
            _save_settings(settings)
            bot.reply_to(m, f"🕐 Время дайджеста: <b>{settings['digest_time']}</b>.", parse_mode="HTML")

        # ---------- команда ----------
        def cmd_open(m: Message) -> None:
            bot.send_message(m.chat.id, _home_text(crd), parse_mode="HTML", reply_markup=_home_kb())

        def noop(c: CallbackQuery) -> None:
            _answer(bot, c)

        # ---------- регистрация ----------
        tg.cbq_handler(open_home, lambda c: c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
        tg.cbq_handler(open_home, lambda c: c.data == CB_HOME)
        tg.cbq_handler(check_now, lambda c: c.data == CB_CHECK)
        tg.cbq_handler(open_lots, lambda c: c.data.startswith(f"{CB_LOTS}:"))
        tg.cbq_handler(open_lot, lambda c: c.data.startswith(f"{CB_LOT}:"))
        tg.cbq_handler(act_dthresh, lambda c: c.data == CB_DTHRESH)
        tg.cbq_handler(act_lthresh, lambda c: c.data.startswith(f"{CB_LTHRESH}:"))
        tg.cbq_handler(act_lthreset, lambda c: c.data.startswith(f"{CB_LTHRESET}:"))
        tg.cbq_handler(act_interval, lambda c: c.data == CB_INT)
        tg.cbq_handler(toggle_digest, lambda c: c.data == CB_DIGTOG)
        tg.cbq_handler(act_digtime, lambda c: c.data == CB_DIGTIME)
        tg.cbq_handler(noop, lambda c: c.data == CB_NOOP)
        tg.msg_handler(edit_dthresh, func=lambda m: _state_eq(tg, m, ST_DTHRESH))
        tg.msg_handler(edit_lthresh, func=lambda m: _state_starts(tg, m, f"{ST_LTHRESH}:"))
        tg.msg_handler(edit_interval, func=lambda m: _state_eq(tg, m, ST_INT))
        tg.msg_handler(edit_digtime, func=lambda m: _state_eq(tg, m, ST_DIGTIME))
        tg.msg_handler(cmd_open, commands=["stock"])

        try:
            crd.add_telegram_commands(UUID, [("stock", "Монитор остатков товаров автовыдачи.", True)])
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)

        logger.info(f"StockMonitor v{VERSION}: Telegram-меню зарегистрировано (команда /stock).")
    else:
        logger.info(f"StockMonitor v{VERSION}: Telegram выключен — работает только мониторинг.")

    threading.Thread(target=_monitor_loop, args=(crd,), daemon=True).start()


def on_delete(crd: "Cardinal", c=None) -> None:
    """Вызывается при удалении плагина через ПУ (BIND_TO_DELETE)."""
    _stop_event.set()


# ══════════════════════════ ПРИВЯЗКА К СОБЫТИЯМ КАРДИНАЛА ═══════════════════════
BIND_TO_PRE_INIT = [init]
BIND_TO_DELETE = on_delete
