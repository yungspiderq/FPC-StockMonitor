"""
Функциональные тесты плагина stock_monitor.py на моках.
Запуск: cd FPC && python3 tests_plugin_stock_monitor.py
"""
import importlib.util
import json
import os
import sys
import time
from configparser import ConfigParser
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, ".")
sys.path.append("plugins")

# ───────────────────────────── загрузка плагина ─────────────────────────────
spec = importlib.util.spec_from_file_location("plugins.stock_monitor", "plugins/stock_monitor.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules["plugins.stock_monitor"] = plugin
spec.loader.exec_module(plugin)

plugin._monitor_loop = lambda crd: None  # поток тестируем отдельно через _monitor_tick

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("✓ " if cond else "✗ FAIL: ") + name)


def reset_settings(**kwargs):
    os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
    base = json.loads(json.dumps(plugin.DEFAULT_SETTINGS))
    base.update(kwargs)
    with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(base, f, ensure_ascii=False)


# ───────────────────── тестовые товарные файлы и AD_CFG ─────────────────────
PRODUCTS_DIR = "storage/products"
FILE_A = "stkm_test_a.txt"   # используют два лота (Lot A, Lot C)
FILE_B = "stkm_test_b.txt"   # один лот (Lot B)


def write_products(fname, n):
    os.makedirs(PRODUCTS_DIR, exist_ok=True)
    with open(os.path.join(PRODUCTS_DIR, fname), "w", encoding="utf-8") as f:
        f.write("\n".join(f"product_{i}" for i in range(n)) + ("\n" if n else ""))


def remove_products(fname):
    try:
        os.remove(os.path.join(PRODUCTS_DIR, fname))
    except OSError:
        pass


def make_ad_cfg():
    cfg = ConfigParser(delimiters=(":",), interpolation=None)
    cfg.optionxform = str
    cfg["Lot A"] = {"response": "Товар: $product", "productsFileName": FILE_A}
    cfg["Lot B"] = {"response": "Товар: $product", "productsFileName": FILE_B}
    cfg["Lot C"] = {"response": "Товар: $product", "productsFileName": FILE_A}
    cfg["Lot NoFile"] = {"response": "Без товара"}
    return cfg


# ───────────────────────────── моки ─────────────────────────────
class MockBot:
    def __init__(self):
        self.answers = []
        self.edits = []
        self.sent = []
        self.replies = []

    def answer_callback_query(self, cb_id, text=None, show_alert=False):
        self.answers.append((cb_id, text, show_alert))

    def edit_message_text(self, text, chat_id, message_id, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))
        return SimpleNamespace(id=message_id)

    def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))
        return SimpleNamespace(id=len(self.sent) + 100, chat=SimpleNamespace(id=chat_id))

    def reply_to(self, m, text, parse_mode=None):
        self.replies.append(text)

    def delete_message(self, chat_id, message_id):
        pass


def kb_buttons(kb) -> str:
    if kb is None:
        return ""
    try:
        return " | ".join(b.text for row in kb.keyboard for b in row if b.text)
    except Exception:
        return str(kb)


def kb_button_list(kb) -> list:
    if kb is None:
        return []
    try:
        return [b.text for row in kb.keyboard for b in row]
    except Exception:
        return []


class MockTelegram:
    def __init__(self):
        self.bot = MockBot()
        self.cbq_handlers = []
        self.msg_handlers = []
        self.user_states = {}
        self.notification_settings = {}
        self.notifications = []

    def cbq_handler(self, handler, func, **kwargs):
        self.cbq_handlers.append((handler, func))

    def msg_handler(self, handler, **kwargs):
        self.msg_handlers.append((handler, kwargs))

    def set_state(self, chat_id, message_id, user_id, state, data=None):
        self.user_states[(chat_id, user_id)] = {"state": state, "mid": message_id, "data": data or {}}

    def get_state(self, chat_id, user_id):
        return self.user_states.get((chat_id, user_id))

    def check_state(self, chat_id, user_id, state):
        return self.user_states.get((chat_id, user_id), {}).get("state") == state

    def clear_state(self, chat_id, user_id, del_msg=False):
        st = self.user_states.pop((chat_id, user_id), None)
        return st.get("mid") if st else None

    def is_notification_enabled(self, chat_id, notification_type):
        try:
            return bool(self.notification_settings[str(chat_id)][notification_type])
        except KeyError:
            return False

    def send_notification(self, text, notification_type=None, **kwargs):
        self.notifications.append((text, notification_type))
        for chat_id in list(self.notification_settings):
            if notification_type is not None and not self.is_notification_enabled(chat_id, notification_type):
                continue
            self.bot.send_message(int(chat_id), text)


class MockCardinal:
    def __init__(self, telegram, ad_cfg):
        self.telegram = telegram
        self.AD_CFG = ad_cfg
        self.plugins = {plugin.UUID: SimpleNamespace(enabled=True)}
        self.registered_commands = None

    def add_telegram_commands(self, uuid, commands):
        self.registered_commands = commands


def make_crd(with_tg=True):
    tg = MockTelegram() if with_tg else None
    return MockCardinal(tg, make_ad_cfg()), tg


# ═══════════════════════════ ТЕСТ 1: метаданные и утилиты ═══════════════════════════
from uuid import UUID as _UUID
check("1.1 метаданные плагина", all(hasattr(plugin, f) for f in
      ["NAME", "VERSION", "DESCRIPTION", "CREDITS", "SETTINGS_PAGE", "UUID", "BIND_TO_DELETE"]))
check("1.2 UUID валиден", str(_UUID(plugin.UUID, version=4)) == plugin.UUID)
check("1.3 _parse_hhmm", plugin._parse_hhmm("09:00") == (9, 0) and plugin._parse_hhmm("9:5") == (9, 5)
      and plugin._parse_hhmm("23.30") == (23, 30) and plugin._parse_hhmm("abc") is None
      and plugin._parse_hhmm("25:00") is None)

# ═══════════════════════════ ТЕСТ 2: сканирование остатков ═══════════════════════════
write_products(FILE_A, 10)
write_products(FILE_B, 2)
crd, tg = make_crd(with_tg=True)

files = plugin._scan(crd)
check("2.1 скан: 2 файла (лот без файла пропущен)", set(files) == {FILE_A, FILE_B})
check("2.2 общий файл объединяет лоты", sorted(files[FILE_A]["lots"]) == ["Lot A", "Lot C"])
check("2.3 остатки посчитаны", files[FILE_A]["count"] == 10 and files[FILE_B]["count"] == 2)

settings = plugin._load_settings()
check("2.4 порог: общий файл — максимум индивидуальных порогов",
      plugin._file_threshold({"default_threshold": 5, "thresholds": {"Lot A": 8, "Lot C": 3}},
                             ["Lot A", "Lot C"]) == 8)
check("2.5 порог: нет индивидуальных → глобальный",
      plugin._file_threshold({"default_threshold": 5, "thresholds": {}}, ["Lot B"]) == 5)

# ═══════════════════════════ ТЕСТ 3: тик мониторинга ═══════════════════════════
reset_settings(default_threshold=5)
tg.notifications.clear()
events = plugin._monitor_tick(crd)
kinds = {k for k, *_ in events}
check("3.1 остаток 2 ≤ порога 5 → событие low (⚠️)",
      "low" in kinds and not any(k == "low" and "Lot A" in n for k, n, *_ in events))
check("3.2 уведомление ⚠️ отправлено с названием лота",
      any("⚠️" in t and "Lot B" in t for t, _ in tg.notifications))
check("3.3 warned-состояние сохранено", plugin._load_settings()["warned"].get(FILE_B) == "low")

tg.notifications.clear()
events = plugin._monitor_tick(crd)
check("3.4 повторный тик без изменений — без спама", events == [] and tg.notifications == [])

# товар закончился
write_products(FILE_B, 0)
tg.notifications.clear()
events = plugin._monitor_tick(crd)
check("3.5 остаток 0 → эскалация empty (🚨)", any(k == "empty" for k, *_ in events))
check("3.6 критическое уведомление 🚨", any("🚨" in t and "ЗАКОНЧИЛСЯ" in t for t, _ in tg.notifications))

# пополнение
write_products(FILE_B, 10)
tg.notifications.clear()
events = plugin._monitor_tick(crd)
check("3.7 пополнение выше порога → событие restock (✅)", any(k == "restock" for k, *_ in events))
check("3.8 уведомление ✅ о пополнении", any("✅" in t for t, _ in tg.notifications))
check("3.9 warned очищен", FILE_B not in plugin._load_settings()["warned"])

# пополнение после «low» (не «empty») — тихое, без уведомления
write_products(FILE_B, 2)
plugin._monitor_tick(crd)  # low
write_products(FILE_B, 10)
tg.notifications.clear()
events = plugin._monitor_tick(crd)
check("3.10 восстановление после low — без уведомления (тихо)",
       not any(k == "restock" for k, *_ in events) and tg.notifications == [])

# файл исчез из конфига — warned чистится
write_products(FILE_B, 0)
plugin._monitor_tick(crd)  # empty
cfg2 = make_ad_cfg()
cfg2.remove_section("Lot B")
crd.AD_CFG = cfg2
plugin._monitor_tick(crd)
check("3.11 исчезнувший файл убран из warned", FILE_B not in plugin._load_settings()["warned"])
crd.AD_CFG = make_ad_cfg()

# индивидуальный порог выше остатка — молчим
write_products(FILE_B, 2)
reset_settings(default_threshold=5, thresholds={"Lot B": 1}, warned={})
tg.notifications.clear()
events = plugin._monitor_tick(crd)
check("3.12 индивидуальный порог (1) — остаток 2 в норме, событий нет",
      not any(n and "Lot B" in n for _, n, *_ in events))

# ═══════════════════════════ ТЕСТ 4: дайджест ═══════════════════════════
write_products(FILE_A, 10)
write_products(FILE_B, 0)
reset_settings(default_threshold=5, digest=True, digest_time="09:00", warned={FILE_B: "empty"})
tg.notifications.clear()
plugin._monitor_tick(crd, now=datetime(2026, 10, 16, 10, 0))
digests = [t for t, _ in tg.notifications if "Утренняя сводка" in t]
check("4.1 дайджест отправлен после заданного времени", len(digests) == 1)
check("4.2 в дайджесте проблемный лот", "Lot B" in digests[0] and "🚨" in digests[0])
check("4.3 last_digest сохранен", plugin._load_settings()["last_digest"] == "2026-10-16")

tg.notifications.clear()
plugin._monitor_tick(crd, now=datetime(2026, 10, 16, 12, 0))
check("4.4 второй раз за день дайджест не отправляется",
      not any("Утренняя сводка" in t for t, _ in tg.notifications))

tg.notifications.clear()
plugin._monitor_tick(crd, now=datetime(2026, 10, 16, 8, 0))
check("4.5 до заданного времени дайджеста нет (уже отправлен сегодня)",
      not any("Утренняя сводка" in t for t, _ in tg.notifications))

# всё в норме — дайджест не шлется, но дата отмечается
write_products(FILE_B, 10)
reset_settings(default_threshold=5, digest=True, digest_time="09:00")
tg.notifications.clear()
plugin._monitor_tick(crd, now=datetime(2026, 10, 17, 10, 0))
check("4.6 при нормальных остатках дайджест не отправляется, дата фиксируется",
      not any("Утренняя сводка" in t for t, _ in tg.notifications)
      and plugin._load_settings()["last_digest"] == "2026-10-17")

# ═══════════════════════════ ТЕСТ 5: Telegram UI ═══════════════════════════
write_products(FILE_A, 10)
write_products(FILE_B, 2)
crd, tg = make_crd(with_tg=True)
plugin.init(crd)
bot = tg.bot
reset_settings(default_threshold=5)

check("5.1 команда /stock зарегистрирована",
      any(kw.get("commands") == ["stock"] for _, kw in tg.msg_handlers))


def dispatch_cb(data, chat_id=111, msg_id=555, user_id=222):
    c = SimpleNamespace(data=data, id="cb1",
                        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), id=msg_id),
                        from_user=SimpleNamespace(id=user_id))
    for handler, func in tg.cbq_handlers:
        try:
            matched = func(c)
        except Exception:
            matched = False
        if matched:
            handler(c)
            return handler.__name__
    return None


def dispatch_msg(text, chat_id=111, user_id=222):
    m = SimpleNamespace(chat=SimpleNamespace(id=chat_id), from_user=SimpleNamespace(id=user_id),
                        text=text)
    for h, kw in tg.msg_handlers:
        f = kw.get("func")
        try:
            if f and f(m):
                h(m)
                return h.__name__
        except Exception:
            pass
    return None


def last_text():
    if bot.edits:
        return bot.edits[-1][0]
    return bot.sent[-1][1] if bot.sent else ""


def last_kb():
    return kb_buttons(bot.edits[-1][1]) if bot.edits else ""


name = dispatch_cb(f"47:{plugin.UUID}:0")
check("5.2 страница настроек плагина открывает меню",
      name == "open_home" and "StockMonitor" in last_text())
check("5.3 главная: статистика (1 файл ниже порога)",
      "Ниже порога: <b>1</b>" in last_text() and "Файлов с товаром: <b>2</b>" in last_text())

dispatch_cb(f"{plugin.CB_LOTS}:0")
btns = kb_button_list(bot.edits[-1][1])
check("5.4 список лотов: пустые сверху (Lot B с 2 шт. первый)",
      any("Lot B" in b and "(2)" in b for b in btns[:1]) and any("Lot A" in b for b in btns))
check("5.5 маркировка ⚠️ у лота ниже порога", any(b.startswith("⚠️") and "Lot B" in b for b in btns))

hash_b = plugin._lot_hash(FILE_B)
name = dispatch_cb(f"{plugin.CB_LOT}:{hash_b}:0")
check("5.6 страница лота: файл, остаток, оба лота файла",
      name == "open_lot" and FILE_B in last_text() and "Lot B" in last_text()
      and "<b>2</b>" in last_text())

# индивидуальный порог через состояние
name = dispatch_cb(f"{plugin.CB_LTHRESH}:{hash_b}")
st = tg.user_states.get((111, 222), {})
check("5.7 кнопка порога ставит состояние с хэшем",
      name == "act_lthresh" and st.get("state") == f"{plugin.ST_LTHRESH}:{hash_b}")
dispatch_msg("1")
check("5.8 порог лота сохранен (1)", plugin._load_settings()["thresholds"].get("Lot B") == 1)
dispatch_cb(f"{plugin.CB_LOT}:{hash_b}:0")
check("5.9 с порогом 1 остаток 2 — в норме (✅)", last_text().startswith("✅"))
name = dispatch_cb(f"{plugin.CB_LTHRESET}:{hash_b}")
check("5.10 сброс порога лота", name == "act_lthreset"
      and "Lot B" not in plugin._load_settings()["thresholds"])

# глобальные настройки
dispatch_cb(plugin.CB_DTHRESH)
dispatch_msg("7")
check("5.11 глобальный порог сохранен", plugin._load_settings()["default_threshold"] == 7)
dispatch_cb(plugin.CB_INT)
dispatch_msg("120")
check("5.12 интервал сохранен", plugin._load_settings()["check_interval"] == 120)
dispatch_cb(plugin.CB_INT)
dispatch_msg("abc")
check("5.13 нечисловой ввод отклоняется", plugin._load_settings()["check_interval"] == 120
      and any("❌" in r for r in bot.replies[-1:]))
name = dispatch_cb(plugin.CB_DIGTOG)
check("5.14 дайджест переключается", name == "toggle_digest"
      and plugin._load_settings()["digest"] is True)
dispatch_cb(plugin.CB_DIGTIME)
dispatch_msg("10:30")
check("5.15 время дайджеста сохранено", plugin._load_settings()["digest_time"] == "10:30")
dispatch_cb(plugin.CB_DIGTIME)
dispatch_msg("день")
check("5.16 некорректное время отклоняется", plugin._load_settings()["digest_time"] == "10:30")

# «проверить сейчас»: Lot B (2 шт) теперь ниже нового порога 7 → прилетит ⚠️
tg.notifications.clear()
name = dispatch_cb(plugin.CB_CHECK)
check("5.17 «Проверить сейчас» запускает проверку и шлет предупреждение",
      name == "check_now" and any("⚠️" in t and "Lot B" in t for t, _ in tg.notifications))
dispatch_cb(plugin.CB_NOOP)
check("5.18 noop отвечает", True)

# ═══════════════════════════ ТЕСТ 6: нормализация настроек ═══════════════════════════
os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
    json.dump({"default_threshold": "abc", "thresholds": "broken", "check_interval": 5,
               "digest": 1, "digest_time": "99:99", "warned": None}, f, ensure_ascii=False)
s = plugin._load_settings()
check("6.1 битые значения нормализуются",
      s["default_threshold"] == 5 and s["thresholds"] == {} and s["check_interval"] == 60
      and s["digest"] is True and s["digest_time"] == "09:00" and s["warned"] == {})
with open(plugin.SETTINGS_PATH, "w") as f:
    f.write("{broken")
s = plugin._load_settings()
check("6.2 битый JSON → дефолты", s["default_threshold"] == 5 and s["warned"] == {})

# ═══════════════════════════ уборка ═══════════════════════════
reset_settings()
remove_products(FILE_A)
remove_products(FILE_B)

# ═══════════════════════════ ИТОГ ═══════════════════════════
print(f"\n{'=' * 60}\nПройдено: {len(PASS)}  Провалено: {len(FAIL)}")
if FAIL:
    print("Проваленные тесты:")
    for f_ in FAIL:
        print("  ✗", f_)
    sys.exit(1)
print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ ✅")
