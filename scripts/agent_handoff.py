"""Атомарный инструмент координации агентов для .agent-handoff.json.

Зачем один общий скрипт. Правило команды: правки handoff должны быть
атомарными (json-load → modify → json-dump). Если каждый агент правит файл
своими Edit-вызовами, правки теряются в гонке. Этот скрипт — единственная
точка записи: он читает свежую версию, меняет только свои секции и
подменяет файл атомарно, оптимистично проверяя mtime.

Универсальный шаблон. Скрипт ничего не знает о предметной области: список
зон, гейтов, зарезервированных путей и границ берётся из .agent-protocol.json.
Проект настраивает ПРОТОКОЛ, а не этот код.

Подкоманды:
    status                      кто что держит, какие hints, owners
    claim    <agent> <task>     занять область (файлы через --files)
    release  <agent>            снять claim
    heartbeat <agent>           отметить, что агент жив
    log-entry                   добавить датированную запись в task_log
    hint "текст"                добавить однострочную подсказку коллегам
    backlog list|add|set        приоритеты P0–P4 в .agent-protocol.json
    who-owns <путь>...          занятость файлов: живой claim или зона
    health-check                сводка по команде: P0/P1, stale, кодировка
    validate                    проверка координационных файлов
    repair-encoding             починить mojibake в координатных файлах
    normalize-legacy            пометить старые записи как legacy
    protocol-set                обновить ключ в .agent-protocol.json
    status-set                  атомарно обновить docs/agent-status.json

Безопасность данных:
  • чтение всегда utf-8-sig (в файле может быть BOM);
  • перед подменой JSON перечитывается и валидируется, иначе запись
    отменяется и файл не трогается;
  • при repair-encoding создаётся бэкап в .agent-handoff.backups/;
  • повторный запуск идемпотентен: log-entry с тем же task не дублируется.

    python scripts/agent_handoff.py status
    python scripts/agent_handoff.py repair-encoding --dry-run
    python scripts/agent_handoff.py hint "To all: ..."
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
HANDOFF = ROOT / ".agent-handoff.json"
PROTOCOL = ROOT / ".agent-protocol.json"
STATUS = ROOT / "docs" / "agent-status.json"
BACKUP_DIR = ROOT / ".agent-handoff.backups"
BOM = b"\xef\xbb\xbf"

# Журнал статуса: путь в репозитории (для ownership) и канон команд гейтов,
# чтобы команда в journal.current_gate не зависела от того, кто её записал.
#
# Канон гейтов УНИВЕРСАЛЬНЫЙ и намеренно минимальный: «pytest» есть в каждом
# питоновском проекте, остальное добавляет владелец проекта. Тянуть сюда
# сквозные проверки конкретного пайплайна нельзя — шаблон переносится в любой
# репозиторий, и команда, которой там нет, окажется красной навсегда.
# Полный список гейтов живёт в scripts/dev.py (GATES) и в .agent-protocol.json
# -> verification.
STATUS_JOURNAL = "docs/agent-status.json"
GATE_COMMANDS = {
    "pytest": "{python} -m pytest -q",
    "coordination": "{python} scripts/agent_handoff.py validate",
}

#: Префиксы временных каталогов, которые health-check считает мусором в корне.
#: Читается из протокола (boundaries.scratch_prefixes) и дополняется этими.
DEFAULT_SCRATCH_PREFIXES = ("tmp", ".scratch", "_e2e_", "_harness_", "e2e_")


# Типичные «пары» mojibake: кириллица в UTF-8, прочитанная как cp1251.
MOJIBAKE_PAIRS = (
    "Р°", "Рµ", "СЂ", "С‚", "РЅ", "СЃ", "Рѕ", "Рё", "Рє", "Р»",
    "Сѓ", "РІ", "Р·", "Рј", "Рї", "С‡", "С€", "С‹", "СЊ", "Р”", "Р—",
)


def read_json(path: Path | str) -> dict:
    """Читает координатный файл. BOM допускается и снимается."""
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_bytes().decode("utf-8-sig"))

def write_json_atomic(path: Path, data: dict, expect_mtime: float | None = None) -> None:
    """Пишет JSON атомарно, сохраняя BOM.

    expect_mtime — оптимистичная блокировка: если файл изменился с момента
    чтения, запись отменяется (значит, коллега редактирует прямо сейчас).
    """
    if expect_mtime is not None and path.exists():
        current = path.stat().st_mtime
        if abs(current - expect_mtime) > 1e-6:
            raise RuntimeError(
                f"{path.name} changed since read (mtime {current} != {expect_mtime}); "
                "перечитайте и повторите"
            )
    text = json.dumps(data, ensure_ascii=False, indent=4)
    blob = (BOM if path.exists() and path.read_bytes().startswith(BOM) else BOM) + text.encode("utf-8")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(blob)
    json.loads(tmp.read_text(encoding="utf-8-sig"))  # валидация до подмены
    tmp.replace(path)


def repair_mojibake_in_place(obj):
    """Рекурсивно чинит mojibake в строках. Возвращает (объект, счётчик).

    Признак устойчивый: candidate = s.encode('cp1251').decode('utf-8') должен
    существовать, давать обратно ровно s при обратном преобразовании и убирать
    маркеры. Для корректного русского текста roundtrip не проходит, поэтому
    он не портится.
    """
    fixed = 0

    def fix_str(s: str) -> str:
        nonlocal fixed
        if not isinstance(s, str) or not s:
            return s
        if not any(pair in s for pair in MOJIBAKE_PAIRS):
            return s
        try:
            candidate = s.encode("cp1251").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return s
        if candidate == s:
            return s
        if candidate.encode("utf-8").decode("cp1251") != s:  # не идеальный roundtrip
            return s
        if not any("а" <= ch.lower() <= "я" for ch in candidate):
            return s
        before = sum(s.count(p) for p in MOJIBAKE_PAIRS)
        after = sum(candidate.count(p) for p in MOJIBAKE_PAIRS)
        if after >= before:  # стало не лучше — не трогаем
            return s
        fixed += 1
        return candidate

    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return fix_str(node)

    return walk(obj), fixed


def cmd_status(args) -> int:
    data = read_json(HANDOFF)
    tp = data.get("team_protocol", {})
    print(f"handoff: {HANDOFF.name}  ({HANDOFF.stat().st_size} Б)" if HANDOFF.exists() else "handoff: нет")
    print(f"updated_by={data.get('updated_by')}  task_log={len(data.get('task_log', []))}")
    print("\n== active_claims ==")
    claims = tp.get("active_claims", {})
    live = active_claim_count(claims)
    if not claims:
        print("  (нет активных claim)")
    for agent, c in claims.items():
        age = _minutes_since(c.get("heartbeat_at") or c.get("started_at", ""))
        age_txt = f", {age:.0f} мин назад" if age is not None else ""
        flag = " [УСТАРЕЛ — файлы свободны]" if is_stale_claim(c) else ""
        print(f"  {agent}: {c.get('task')} [{', '.join(c.get('files', []))}]{age_txt}{flag}")
    print(f"  живыми считаются {live} из {len(claims)} (лимит агентов: {max_agents()})")

    protocol = read_json(PROTOCOL)
    zones = {k: v for k, v in (protocol.get("zones") or {}).items() if not k.startswith("_")}
    print("\n== зоны (подсказка, не запрет) ==")
    for zone, area in zones.items():
        busy = sorted({
            f for c in claims.values() if not is_stale_claim(c)
            for f in c.get("files", []) if zone_of(f, protocol) == zone
        })
        mark = f"  <- занято: {', '.join(busy)}" if busy else ""
        print(f"  {zone}: {', '.join(area) if isinstance(area, list) else area}{mark}")
    print("\n== hints ==")
    for h in data.get("hints", []):
        print(f"  - {h}")
    return 0


def parse_files(raw: list[str] | str | None) -> list[str]:
    """Разбирает список файлов claim'а.

    Регламент пишет `--files <file1> <file2>` (через пробел), старые записи и
    подсказки используют запятую. Принимаем оба вида: запятая внутри элемента
    тоже разделяет.
    """
    if not raw:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out: list[str] = []
    for item in items:
        out.extend(p.strip() for p in str(item).split(",") if p.strip())
    return out


def cmd_claim(args) -> int:
    """Создаёт claim по схеме регламента.

    Схема: agent, task, files, started_at, heartbeat_at, status. Раньше не было
    heartbeat_at/status, поэтому нельзя было отличить живую задачу от stale и
    нельзя было выполнить требуемый heartbeat для задач дольше 30 минут.
    """
    mtime = HANDOFF.stat().st_mtime if HANDOFF.exists() else None
    data = read_json(HANDOFF)
    tp = data.setdefault("team_protocol", {})
    claims = tp.setdefault("active_claims", {})
    files = parse_files(args.files)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Живой claim с ТЕМ ЖЕ именем - не конфликт файлов, а конфликт сессий.
    # Именно он опаснее всего: раньше он молча заменялся, файлы первой
    # сессии исчезали из active_claims и становились «свободными», пока
    # первая сессия продолжала с ними работать. Никто этого не видел -
    # просто набор занятых файлов уменьшался.
    #   Так происходит, когда сессия перезапускается и берёт прежнее имя.
    # Правильный путь - отдельное имя вида <имя>/2, как и требует
    # claims.subagents для субагентов.
    previous = claims.get(args.agent)
    if previous and not is_stale_claim(previous) and not args.take_over:
        age = _minutes_since(previous.get("heartbeat_at") or previous.get("started_at"))
        age_txt = f"{age:.0f} мин назад" if age is not None else "неизвестно"
        print(f"[ERR] имя {args.agent!r} уже держит ЖИВОЙ claim "
              f"(heartbeat {age_txt}).", file=sys.stderr)
        print(f"      задача: {str(previous.get('task'))[:160]}", file=sys.stderr)
        print(f"      файлы : {previous.get('files')}", file=sys.stderr)
        print("[HINT] это почти всегда перезапущенная сессия. Возьмите отдельное "
              "имя вида '<ваше-имя>/2' - иначе claim первой сессии исчезнет, её "
              "файлы станут свободны, а release снимет уже ваш.", file=sys.stderr)
        print("       Если вы действительно забираете работу себе (первая сессия "
              "мертва) - добавьте --take-over и скажите об этом в coordination_note.",
              file=sys.stderr)
        return 4

    # Регламент: нельзя брать файл под чужим активным claim.
    conflicts: list[str] = []
    for other, c in claims.items():
        if other == args.agent:
            continue
        if is_stale_claim(c):
            # Брошенный claim не блокирует: иначе исчезнувший агент навсегда
            # оставляет свои файлы занятыми и команда ждёт того, кто не придёт.
            print(f"[NOTE] claim {other} устарел (без heartbeat) — его файлы свободны")
            continue
        claimed_files = parse_files(c.get("files", []))
        for f in files:
            if any(path_matches(pf, f) for pf in claimed_files):
                conflicts.append(f"{f} (занят: {other})")
    if conflicts and not args.force:
        print("\n".join(f"[ERR] файл под живым claim: {c}" for c in conflicts), file=sys.stderr)
        print("[HINT] выберите другую задачу, дождитесь release, или --force если владелец молчит "
              f"(порог устаревания: {stale_after_minutes()} мин)", file=sys.stderr)
        return 4

    protocol = read_json(PROTOCOL)

    # Зарезервированная зона — единственный настоящий запрет: здесь ошибка
    # ломает что-то за пределами репозитория (настройки ОС, миграции, деплой).
    # Проверка идёт ДО записи claim, иначе отказ успевал бы уже подменить файл.
    reserved = parse_files(protocol.get("zones", {}).get("reserved") or [])
    fenced = [f for f in files if any(path_matches(p, _norm_path(f)) for p in reserved)]
    if fenced and not args.force:
        print(f"[ERR] зона зарезервирована и требует явного --force: {', '.join(fenced)}",
              file=sys.stderr)
        print("[HINT] reserved-зона — единственная настоящая огороженная территория: "
              "ошибка здесь ломает то, что вне репозитория.",
              file=sys.stderr)
        print("       Сначала запишите своё имя в zones.reserved протокола "
              "и предупредите команду через hint, затем берите с --force.",
              file=sys.stderr)
        return 4

    previous = claims.get(args.agent)
    entry = {
        "agent": args.agent,
        "task": args.task,
        "files": files,
        "started_at": now,
        "heartbeat_at": now,
        "status": "IN_PROGRESS",
    }
    if previous:
        # Ничего из прежнего claim'а не теряется молча. Файлы первой сессии -
        # самое важное: если они исчезнут, конфликт-детектор перестанет их
        # защищать, и это заметят не сразу.
        entry["started_at"] = previous.get("started_at", now)
        entry["previous_task"] = previous.get("task")
        entry["previous_files"] = previous.get("files")
        entry["previous_started_at"] = previous.get("started_at")
        if args.take_over and not is_stale_claim(previous):
            entry["took_over_live_claim"] = True
            print(f"[WARN] ЗАБРАН ЖИВОЙ claim прежней сессии: "
                  f"{previous.get('task')}")
            print(f"[WARN] его файлы {previous.get('files')} больше не защищены "
                  f"конфликт-детектором - упомяните это в coordination_note.")
    claims[args.agent] = entry
    write_json_atomic(HANDOFF, data, mtime)
    print(f"claim добавлен: {args.agent} -> {args.task} {files}")

    zones: dict[str, list[str]] = {}
    for f in files:
        z = zone_of(f, protocol)
        if z:
            zones.setdefault(z, []).append(f)
    for z, fs in sorted(zones.items()):
        print(f"[ZONE] {z}: {', '.join(fs)}")
    print("[NOTE] зона — подсказка, не запрет: она показывает, где обычно работают, "
          "но блокирует только живой claim на файл")
    return 0


def cmd_heartbeat(args) -> int:
    """Отмечает, что агент жив (требование для задач дольше 30 минут)."""
    mtime = HANDOFF.stat().st_mtime if HANDOFF.exists() else None
    data = read_json(HANDOFF)
    claims = data.get("team_protocol", {}).get("active_claims", {})
    if args.agent not in claims:
        print(f"[ERR] нет активного claim у {args.agent}", file=sys.stderr)
        return 1
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    claims[args.agent]["heartbeat_at"] = now
    if args.note:
        claims[args.agent]["heartbeat_note"] = args.note
    write_json_atomic(HANDOFF, data, mtime)
    print(f"heartbeat: {args.agent} @ {now}" + (f" — {args.note}" if args.note else ""))
    return 0


def cmd_release(args) -> int:
    mtime = HANDOFF.stat().st_mtime if HANDOFF.exists() else None
    data = read_json(HANDOFF)
    claims = data.get("team_protocol", {}).get("active_claims", {})
    if args.agent in claims:
        del claims[args.agent]
        write_json_atomic(HANDOFF, data, mtime)
        print(f"claim снят: {args.agent}")
    else:
        print(f"claim не найден: {args.agent}")
    return 0


def cmd_log_entry(args) -> int:
    entry = json.loads(Path(args.file).read_text(encoding="utf-8-sig")) if args.file else json.loads(args.json)
    entry.setdefault("date", datetime.now().strftime("%Y-%m-%d"))
    entry.setdefault("author", args.agent)
    mtime = HANDOFF.stat().st_mtime if HANDOFF.exists() else None
    data = read_json(HANDOFF)
    log = data.setdefault("task_log", [])
    if any(e.get("task") == entry.get("task") for e in log):
        print(f"запись уже есть, пропуск (идемпотентно): {entry['task']}")
        return 0
    log.append(entry)
    data["updated_by"] = entry.get("author", data.get("updated_by"))
    data["updated_at"] = entry["date"]
    write_json_atomic(HANDOFF, data, mtime)
    print(f"запись добавлена: {entry['date']} | {entry['task']} | {entry['author']} (всего {len(log)})")
    return 0


def cmd_hint(args) -> int:
    mtime = HANDOFF.stat().st_mtime if HANDOFF.exists() else None
    data = read_json(HANDOFF)
    hints = data.setdefault("hints", [])
    if args.remove is not None:
        target = args.remove
        matches = [i for i, h in enumerate(hints) if h == target] if not target.isdigit() \
            else [int(target)] if 0 <= int(target) < len(hints) else []
        if not matches:
            print(f"подсказка не найдена: {target}")
            return 1
        for i in sorted(matches, reverse=True):
            print(f"удалено: {hints.pop(i)[:80]}")
        write_json_atomic(HANDOFF, data, mtime)
        print(f"осталось подсказок: {len(hints)}")
        return 0
    if not args.text:
        print("[ERR] нужен текст подсказки или --remove", file=sys.stderr)
        return 2
    if args.text in hints:
        print("такая подсказка уже есть, пропуск")
        return 0
    hints.append(args.text)
    write_json_atomic(HANDOFF, data, mtime)
    print(f"подсказка добавлена (всего {len(hints)}): {args.text}")
    return 0


def cmd_backlog(args) -> int:
    """Ведёт backlog в .agent-protocol.json (требование раздела 5 регламента).

    Регламент требует приоритеты P0–P4 и правило «есть P0 → нельзя начинать
    P3/P4». Без машинно-читаемого backlog это правило невозможно проверить.
    """
    mtime = PROTOCOL.stat().st_mtime if PROTOCOL.exists() else None
    data = read_json(PROTOCOL)
    backlog = data.setdefault("backlog", [])

    if args.action == "list":
        if not backlog:
            print("backlog пуст")
            return 0
        order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}
        for item in sorted(backlog, key=lambda x: (order.get(x.get("priority"), 9),
                                                   x.get("added_at", ""))):
            print(f"  [{item.get('priority')}] {item.get('id')} {item.get('status'):8s} "
                  f"{item.get('area'):22s} {item.get('description')}")
        open_p0 = [i for i in backlog if i.get("priority") == "P0" and i.get("status") != "done"]
        print(f"\nвсего: {len(backlog)}, открытых P0: {len(open_p0)}")
        return 1 if open_p0 else 0

    if args.action == "add":
        if not args.priority or not args.description:
            print("[ERR] add требует --priority и --description", file=sys.stderr)
            return 2
        prefix = args.priority
        existing = [i.get("id", "") for i in backlog if str(i.get("id", "")).startswith(prefix)]
        nums = [int(i.split("-")[1]) for i in existing if i.count("-") == 1 and i.split("-")[1].isdigit()]
        new_id = f"{prefix}-{(max(nums) + 1) if nums else 1:03d}"
        backlog.append({
            "id": new_id,
            "priority": args.priority,
            "area": args.area or "unspecified",
            "description": args.description,
            "added_by": args.agent,
            "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "status": "open",
        })
        write_json_atomic(PROTOCOL, data, mtime)
        print(f"backlog: добавлено {new_id} [{args.priority}] {args.description}")
        return 0

    if args.action == "set":
        # Ловушка P2-009: раньше set принимал --description и --priority и
        # МОЛЧА их игнорировал, попутно выставляя status по умолчанию (done).
        # Агент думал, что переписал описание задачи, а правил только статус -
        # и это повторилось дважды за одну сессию. Теперь любое переданное
        # поле действительно пишется, а неизвестный id - ошибка.
        wanted = {}
        if args.description is not None:
            wanted["description"] = args.description
        if args.priority is not None:
            wanted["priority"] = args.priority
        if args.area is not None:
            wanted["area"] = args.area

        for item in backlog:
            if item.get("id") == args.id:
                item.update(wanted)
                if args.status is not None:
                    item["status"] = args.status
                write_json_atomic(PROTOCOL, data, mtime)
                applied = list(wanted)
                if args.status is not None:
                    applied.append("status")
                print(
                    f"backlog: {args.id} обновлён"
                    + (f" ({', '.join(applied)})" if applied else " (ничего не задано)")
                )
                if not applied:
                    print("[NOTE] не передано ни --description, ни --priority, "
                          "ни --status - запись осталась без изменений")
                return 0
        print(f"[ERR] нет записи {args.id}", file=sys.stderr)
        return 1

    return 2


def _minutes_since(stamp: str) -> float | None:
    if not stamp:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return (datetime.now() - datetime.strptime(stamp, fmt)).total_seconds() / 60
        except ValueError:
            continue
    return None


def stale_after_minutes() -> int:
    """Порог устаревания claim'а (протокол claims.stale_after_minutes)."""
    protocol = read_json(PROTOCOL)
    value = (protocol.get("claims") or {}).get("stale_after_minutes")
    try:
        return int(value)
    except (TypeError, ValueError):
        return 90


def is_stale_claim(claim: dict) -> bool:
    """Claim без heartbeat дольше порога считается брошенным.

    Это то, что разрывает deadlock: агент может исчезнуть навсегда, и его
    файлы не должны оставаться заблокированными. Возвращаем False для claim'а
    только что обновлённого.
    """
    stamp = claim.get("heartbeat_at") or claim.get("started_at")
    age = _minutes_since(stamp) if stamp else None
    if age is None:
        return False  # нет метки времени — считаем свежим, чтобы не отнять файл
    return age > stale_after_minutes()


#: Состояния, в которых слот статуса осмыслен. Всё остальное - не
#: состояние, а свободный текст, и тогда журнал нельзя читать машинно:
#: агент, читающий docs/agent-status.json, не может отличить «работает»
#: от «написал абзац о том, что закончил».
SLOT_STATES = frozenset({
    "IN_PROGRESS", "WIP", "WORKING", "IDLE", "AVAILABLE",
    "DONE", "BLOCKED", "ABANDONED",
})

#: Состояния, в которых агент не может одновременно держать живой claim.
SLOT_CLAIM_CONFLICT = frozenset({"DONE", "IDLE", "AVAILABLE", "ABANDONED"})


def status_journal_consistency(claims: dict) -> tuple[list[str], list[str], list[str]]:
    """Сверяет журнал статуса с активными claim'ами.

    Health check до этого читала .agent-handoff.json и .agent-protocol.json,
    но docs/agent-status.json не открывала вообще. А именно по журналу
    статуса агент решает, кто сейчас работает, - то есть это единственное
    место, где расхождение с реальностью обманывает непосредственно.

    Три класса расхождений:

    * ПРОТИВОРЕЧИЕ - живой claim, а слот того же агента говорит, что он
      закончил или свободен. Это не косметика: именно из таких слотов
      новый агент берёт работу, и «DONE» при живом claim'е делает вид,
      что можно начинать с нуля, пока файлы заняты.
    * НЕЧИТАЕМЫЙ СТАТУС - в слоте свободный текст вместо состояния.
      Такой слот невозможно разобрать машинно, и это молча ломает всё,
      что читает журнал.
    * МОЖНО ЗАБРАТЬ - слот без живого claim и без свежего heartbeat, но с
      текстом next_step. Не проблема, а приглашение: там может лежать
      инструкция, которую все продолжают читать как текущую.
    """
    if not STATUS.exists():
        return [], [], []
    try:
        status = read_json(STATUS)
    except (OSError, ValueError):
        return [], [], []
    slots = (status or {}).get("agents") or {}

    contradictions: list[str] = []
    unreadable: list[str] = []
    reclaimable: list[str] = []

    for agent, slot in slots.items():
        if not isinstance(slot, dict):
            continue
        state = str(slot.get("status") or "").strip().upper()
        claim = claims.get(agent)
        live = bool(claim) and not is_stale_claim(claim)

        if state and state not in SLOT_STATES:
            unreadable.append(
                f"{agent}: status={str(slot.get('status'))[:60]!r} — не состояние "
                f"из {sorted(SLOT_STATES)}, журнал не читается машинно"
            )
        elif live and state in SLOT_CLAIM_CONFLICT:
            contradictions.append(
                f"{agent}: слот говорит {state}, а claim живой "
                f"({str(claim.get('task'))[:70]})"
            )
        if not live:
            still, why = status_slot_is_live(agent, status or {}, {"team_protocol": {}})
            if not still and slot.get("next_step"):
                reclaimable.append(f"{agent}: claim'а нет ({why}), но next_step остался")

    for agent, claim in claims.items():
        if is_stale_claim(claim):
            continue
        if agent not in slots:
            contradictions.append(
                f"{agent}: живой claim, но слота в журнале статуса нет вовсе"
            )

    return contradictions, unreadable, reclaimable


def cmd_health_check(args) -> int:
    """Health check команды (раздел 15 регламента).

    Сводит в одну картину то, что регламент требует проверять: DONE/ABANDONED,
    открытые P0/P1, stale claims, размер backlog, дубли hints, кодировку,
    временные каталоги и незакрытые процессы.
    """
    handoff = read_json(HANDOFF)
    protocol = read_json(PROTOCOL)
    log = handoff.get("task_log", [])
    backlog = protocol.get("backlog", [])
    claims = handoff.get("team_protocol", {}).get("active_claims", {})

    done = [t for t in log if (t.get("status") or "DONE").upper() == "DONE"]
    abandoned = [t for t in log if (t.get("status") or "").upper() == "ABANDONED"]
    p0 = [i for i in backlog if i.get("priority") == "P0" and i.get("status") != "done"]
    p1 = [i for i in backlog if i.get("priority") == "P1" and i.get("status") != "done"]

    stale: list[str] = []
    for agent, c in claims.items():
        age = _minutes_since(c.get("heartbeat_at") or c.get("started_at", ""))
        if age is not None and age > 60:
            stale.append(f"{agent} ({age:.0f} мин без heartbeat, задача: {c.get('task')})")

    print(f"DONE-задач: {len(done)}   ABANDONED: {len(abandoned)}")
    print(f"открытых P0: {len(p0)}   открытых P1: {len(p1)}   backlog: {len(backlog)}")
    print(f"активных claims: {len(claims)}")
    for agent, c in claims.items():
        age = _minutes_since(c.get("heartbeat_at") or c.get("started_at", ""))
        age_txt = f", {age:.0f} мин назад" if age is not None else ""
        print(f"  {agent}: [{c.get('status', 'n/a')}] {c.get('task')}{age_txt}")

    print("stale claims:", ", ".join(stale) if stale else "нет")
    for item in p0:
        print(f"  P0-БЛОКЕР: {item.get('id')} {item.get('description')}")

    # повреждение кодировки
    mojibake = 0
    for p in (HANDOFF, PROTOCOL):
        if p.exists():
            text = p.read_bytes().decode("utf-8-sig")
            mojibake += sum(text.count(m) for m in MOJIBAKE_PAIRS)
    print(f"mojibake-маркеров: {mojibake}")

    # Временные каталоги в корне репозитория. Их появление почти всегда означает
    # ловушку из boundaries.temp_trap: tempfile.gettempdir() в некоторых
    # окружениях указывает на корень рабочей копии, и mkdtemp() без dir= создаёт
    # папки прямо в репозитории. Проект дописывает свои префиксы в
    # boundaries.scratch_prefixes протокола.
    scratch_prefixes = list(DEFAULT_SCRATCH_PREFIXES)
    protocol_scratch = ((protocol.get("boundaries") or {}).get("scratch_prefixes") or [])
    scratch_prefixes.extend(str(p) for p in protocol_scratch)

    def _is_scratch(name: str) -> bool:
        return any(name.startswith(prefix) for prefix in scratch_prefixes)

    leftover = [d.name for d in ROOT.iterdir() if d.is_dir() and _is_scratch(d.name)]
    print(f"временные каталоги в корне: {leftover or 'нет'}")

    contradictions, unreadable, reclaimable = status_journal_consistency(claims)

    print("\nсверка журнала статуса с claim'ами:")
    if contradictions:
        for item in contradictions:
            print(f"  ПРОТИВОРЕЧИЕ: {item}")
    if unreadable:
        for item in unreadable:
            print(f"  НЕЧИТАЕМЫЙ СТАТУС: {item}")
    if reclaimable:
        for item in reclaimable:
            print(f"  можно забрать: {item}")
    if not (contradictions or unreadable or reclaimable):
        print("  противоречий нет")

    print("\n=== ИТОГ HEALTH CHECK ===")
    problems = (len(p0) + len(stale) + mojibake + len(leftover)
                + len(contradictions) + len(unreadable))
    print(f"проблем: {problems} (P0={len(p0)}, stale={len(stale)}, "
          f"mojibake={mojibake}, временные каталоги={len(leftover)}, "
          f"противоречий={len(contradictions)}, нечитаемых статусов={len(unreadable)})")
    return 1 if problems else 0


# Разделы журнала статуса, по которым опознаётся настоящий docs/agent-status.json
# (защита от записи поверх постороннего файла без --force).
STATUS_SECTIONS = ("priorities", "agents", "hints", "history", "current_gate", "reservations", "dashboard")


def max_agents() -> int:
    """Максимум одновременно работающих агентов (протокол claims.max_agents)."""
    protocol = read_json(PROTOCOL)
    value = (protocol.get("claims") or {}).get("max_agents")
    try:
        return int(value)
    except (TypeError, ValueError):
        return 5


def active_claim_count(claims: dict) -> int:
    """Сколько claim'ов реально живых (устаревшие не считаются)."""
    return sum(1 for c in claims.values() if not is_stale_claim(c))


def status_slot_is_live(owner: str, status_data: dict, handoff_data: dict) -> tuple[bool, str]:
    """Жив ли ещё агент, которому принадлежит слот в журнале статуса.

    Два независимых свидетельства жизни, и оба нужны: claim может быть
    забыт при нормальном ходе работы, а heartbeat_at в слоте журнала
    обновляется далеко не всегда.

    Сознательное отличие от is_stale_claim, который при отсутствии
    метки времени считает claim СВЕЖИМ - там верно, ведь речь о
    потере файлов, и лучше не отнять чужую работу. Здесь речь о
    записи в чужую заметку, и исходить надо из противоположного: слот
    без claim и без свежего heartbeat не доказывает, что кто-то
    работает. Ровно такие слоты и остаются после завершения сессии.
    """
    claims = (handoff_data.get("team_protocol") or {}).get("active_claims") or {}
    for claim in claims.values():
        if not isinstance(claim, dict):
            continue
        if claim.get("agent") == owner and not is_stale_claim(claim):
            return True, "живой claim"

    slot = ((status_data.get("agents") or {}).get(owner) or {})
    stamp = slot.get("heartbeat_at")
    if stamp:
        age = _minutes_since(stamp)
        if age is not None and age <= stale_after_minutes():
            return True, f"свежий heartbeat ({age:.0f} мин)"
    return False, "нет живого claim и нет свежего heartbeat"


def cmd_status_set(args) -> int:
    """Атомарно обновляет docs/agent-status.json — журнал под single writer'ом.

    Зачем: правила требовали обновлять журнал, но инструмента записи не было, и
    каждый агент правил файл руками — тот же класс гонки, что уже был с
    handoff (тихое перетирание правок). Теперь у журнала один writer — скрипт.
    Разрешены замена верхнеуровневого ключа, вставка/удаление элемента списка
    и вложенный путь вида agents.<имя>.status; вложенные dict создаются по пути.
    """
    # Self-service: каждый агент пишет СВОЙ раздел (agents.<имя>.*) и общие
    # списки (hints, history, priorities). Раньше здесь стоял single-writer, и
    # журнал замерзал наглухо, как только его владелец отключался: агент не мог
    # отметить claim, а остальные не видели, кто работает. Никто не должен ждать
    # чужого разрешения на запись собственного состояния.
    key = args.key
    parts = [p for p in key.split(".") if p]
    owner_section = len(parts) >= 2 and parts[0] == "agents"

    # Перезапись чужого слота — единственное место, где инструмент может
    # стереть чужое состояние. Раньше --force снимал проверку целиком, без
    # всякой сверки: можно было молча затереть слот агента, который прямо
    # сейчас работает. И наоборот - без --force слот завершившейся сессии
    # не мог исправить никто, и он навсегда оставался с инструкцией,
    # которую все читают как текущую. Теперь --force означает не «без
    # вопросов», а «я проверил, что агент завершился»: у живого слота
    # нужен отдельный --override-live, потому что цена ошибки другая.
    if owner_section and parts[1] != args.agent:
        if not args.force:
            print(f"[ERR] раздел agents.{parts[1]} принадлежит агенту {parts[1]}, "
                  f"а запись идёт от {args.agent}", file=sys.stderr)
            print("[HINT] пишите свой раздел: status-set agents.<ваше-имя>.status DONE",
                  file=sys.stderr)
            return 4

        live, why = status_slot_is_live(parts[1], read_json(STATUS),
                                        read_json(HANDOFF))
        if live and not getattr(args, "override_live", False):
            print(f"[ERR] agents.{parts[1]} выглядит ЖИВЫМ ({why}). "
                  f"Его слот нельзя перезаписать молча.", file=sys.stderr)
            print("[HINT] если вы всё же правы, добавьте --override-live "
                  "и скажите об этом в coordination_note: прежнее значение "
                  "будет выведено перед записью.", file=sys.stderr)
            return 4

        previous = ((read_json(STATUS).get("agents") or {}).get(parts[1]) or {})
        overwritten = previous.get(parts[2] if len(parts) > 2 else "") \
            if len(parts) > 2 else previous
        print(f"[FORCE] перезапись чужого слота agents.{parts[1]} "
              f"({why if not live else 'явный --override-live'})")
        print(f"[FORCE] прежнее значение {key}: "
              f"{json.dumps(overwritten, ensure_ascii=False)[:400]}")
        print("[FORCE] внесите это в coordination_note — иначе правка "
              "останется неотличимой от обычной записи.")

    mtime = STATUS.stat().st_mtime if STATUS.exists() else None
    data = read_json(STATUS)
    if not isinstance(data, dict):
        print(f"[ERR] {STATUS.name}: верхний уровень не объект", file=sys.stderr)
        return 2
    if not data and not args.force:
        print(f"[ERR] {STATUS.name} пуст или отсутствует; --force для создания", file=sys.stderr)
        return 2
    if data and not any(k in data for k in STATUS_SECTIONS) and not args.force:
        print(f"[ERR] {STATUS.name} не похож на журнал статуса; --force для осознанной записи",
              file=sys.stderr)
        return 2

    if args.file:
        value = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
    elif args.json is not None:
        raw = args.json
        # Строку, не являющуюся JSON (обычная подсказка или имя агента),
        # принимаем как есть: в списки писать строки удобнее, чем экранировать
        # кавычки, и раньше это заставляло оборачивать всё в json.dumps.
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
    else:
        print("[ERR] нужен JSON-аргумент или --file", file=sys.stderr)
        return 2

    node = data
    for part in parts[:-1]:
        nxt = node.get(part) if isinstance(node, dict) else None
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    leaf = parts[-1]

    action = args.action
    if action == "auto":
        current = node.get(leaf)
        action = "append" if (isinstance(current, list) and not isinstance(value, list)) else "replace"

    if action == "append":
        current = node.get(leaf)
        if current is None:
            current = []
            node[leaf] = current
        if not isinstance(current, list):
            print(f"[ERR] {'.'.join(parts)} — не список, append невозможен", file=sys.stderr)
            return 2
        if value in current:
            print(f"{STATUS.name}: значение уже есть в {'.'.join(parts)} — пропуск")
            return 0
        current.append(value)
        result = f"{'.'.join(parts)}: +1 (всего {len(current)})"
    elif action == "remove":
        current = node.get(leaf)
        if not isinstance(current, list) or value not in current:
            print(f"{STATUS.name}: значение не найдено в {'.'.join(parts)}")
            return 1
        current.remove(value)
        result = f"{'.'.join(parts)}: -1 (осталось {len(current)})"
    else:
        node[leaf] = value
        result = f"{'.'.join(parts)}: заменено"

    # updated_by/updated_at всегда про последнюю запись: иначе журнал доказывал бы
    # авторство давно ушедшего агента и правка выглядела бы чужой.
    data["updated_by"] = args.updated_by or args.agent
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    data.setdefault("protocol_version", "1.0")

    write_json_atomic(STATUS, data, mtime)
    print(f"{STATUS.name}: {result}")
    return 0


def cmd_protocol_set(args) -> int:
    """Добавляет/обновляет один top-level ключ в корневом протоколе.

    Нужен, чтобы правка протокола тоже шла одним атомарным writer’ом, а не
    ручными Edit-вызовами: в команде параллельно работают три агента.
    """
    mtime = PROTOCOL.stat().st_mtime if PROTOCOL.exists() else None
    data = read_json(PROTOCOL)
    if args.file:
        value = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
    else:
        value = json.loads(args.json)
    data[args.key] = value
    write_json_atomic(PROTOCOL, data, mtime)
    print(f"{PROTOCOL.name}: ключ '{args.key}' обновлён")
    return 0


PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_./*\-]+")


def _norm_path(raw: str) -> str:
    """Нормализует путь к виду без './' и с прямыми слэшами.

    lstrip('./') здесь использовал бы ЛЮБЫЕ ведущие точки и срезал бы точку
    в '.agent-handoff.json' -> 'agent-handoff.json'. Поэтому снимаем только
    именно префикс './'.
    """
    text = raw.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def iter_path_tokens(area_text) -> list[str]:
    """Вытаскивает из ownership только пути.

    Значения ownership бывают двух видов: свободный текст («app shell: ...;
    tests: ...») и список путей. Наивный split по пробелам давал мусорные
    элементы и неверных владельцев, поэтому берём только path-подобные строки:
    с разделителем, с glob или с расширением файла (иначе '.agent-handoff.json'
    без слэша терялся, и координационные файлы оставались «ничьими»).
    """
    items = area_text if isinstance(area_text, list) else [area_text]
    out: list[str] = []
    for item in items:
        for raw in PATH_TOKEN_RE.findall(str(item)):
            raw = raw.rstrip(".;,)")
            has_ext = re.search(r"\.[A-Za-z0-9]{1,6}$", raw) is not None
            if "/" not in raw and "*" not in raw and not has_ext:
                continue
            out.append(_norm_path(raw))
    return out


def path_matches(pattern: str, path: str) -> bool:
    """Сопоставляет один ownership-шаблон и конкретный путь.

    Порядок важен: сначала точное совпадение и basename, потом glob, потом
    префикс каталога. Наивное подстроковое сравнение даёт ложные срабатывания:
    шаблон 'src' захватил бы 'src/app/service.py', хотя имелся в виду ровно
    каталог 'src'.
    """
    p = _norm_path(path)
    base = p.rsplit("/", 1)[-1]
    if "*" in pattern:
        return fnmatch.fnmatch(p, pattern) or fnmatch.fnmatch(base, pattern)
    if pattern.endswith("/"):
        return p.startswith(pattern)
    if "/" in pattern:
        return p == pattern
    return base == pattern


def token_specificity(pattern: str) -> int:
    """Насколько шаблон конкретен (больше = точнее).

    Нужно, чтобы выбор владельца не зависел от порядка ключей в JSON: точная
    запись 'scripts/agent_handoff.py' должна побеждать общий glob 'scripts/*'
    у другого владельца, иначе who-owns отдаёт неверного владельца.
    """
    if "*" in pattern:
        return 1
    return 3 if "/" in pattern else 2


def zone_of(path: str, protocol: dict) -> str | None:
    """Возвращает ИМЯ ЗОНЫ для пути (не имя агента).

    Зоны описывают области работы, а не людей: агент может прийти и уйти, и
    таблица зон от этого не меняется. Возвращается самая специфичная зона, иначе
    результат зависел бы от порядка ключей в JSON.
    """
    norm = _norm_path(path)
    best_zone = None
    best_score = 0
    for zone, area in (protocol.get("zones") or {}).items():
        if zone.startswith("_"):
            continue
        for pat in iter_path_tokens(area):
            if path_matches(pat, norm):
                score = token_specificity(pat)
                if score > best_score:
                    best_score, best_zone = score, zone
    return best_zone


def owner_of(path: str, protocol: dict, claims: dict) -> tuple[str | None, str | None]:
    """Возвращает (активный claim, зону) для пути.

    Зона — Advisory: она подсказывает, где обычно работают, но НЕ блокирует.
    Единственное, что действительно мешает, это живой claim на файл.
    """
    norm = _norm_path(path)
    claimed = None
    for agent, c in claims.items():
        if is_stale_claim(c):
            continue  # мёртвый claim не держит файл
        for f in c.get("files", []):
            pat = _norm_path(f)
            if path_matches(pat, norm) or f == path:
                claimed = f"{agent} (claim: {c.get('task')})"
                break
    return claimed, zone_of(path, protocol)


def cmd_who_owns(args) -> int:
    """Показывает занятость файла: живой claim или зона-подсказка.

    Зона НЕ означает «занято» и не означает «владелец»: она только подсказывает,
    где обычно работают. Единственный признак занятости — живой claim.
    """
    protocol = read_json(PROTOCOL)
    claims = read_json(HANDOFF).get("team_protocol", {}).get("active_claims", {})
    rc = 0
    for path in args.paths:
        claimed, zone = owner_of(path, protocol, claims)
        if claimed:
            print(f"{path}: ЗАНЯТО — {claimed}")
            rc = 1
        elif zone:
            print(f"{path}: СВОБОДНО — зона «{zone}» (зона не блокирует)")
        else:
            print(f"{path}: СВОБОДНО — зона не указана (можно работать)")
    return rc


def cmd_validate(args) -> int:
    """Проверяет координатные файлы на исполняемость протокола.

    Ловит именно те сбои, которые уже случались в команде: mojibake,
    записи task_log без обязательных полей, claim на несуществующий файл,
    висячие ссылки протокола на отсутствующие каталоги.
    """
    problems: list[str] = []
    warnings: list[str] = []

    handoff = read_json(HANDOFF)
    protocol = read_json(PROTOCOL)
    if not handoff or not protocol:
        print("[ERR] не хватает одного из координатных файлов", file=sys.stderr)
        return 2

    # 1. кодировка
    for p in (HANDOFF, PROTOCOL):
        text = p.read_bytes().decode("utf-8-sig")
        hits = sum(text.count(m) for m in MOJIBAKE_PAIRS)
        if hits:
            problems.append(f"{p.name}: mojibake-маркеров {hits} (запустите repair-encoding)")

    # 2. схема записей task_log
    #
    # Ключи, начинающиеся с подчёркивания, — служебные пояснения к самой
    # схеме, а не поля записи. Раньше они попадали в список обязательных, и
    # validate объявлял БЛОКИРУЮЩИМ отсутствие поля, которого в записи быть
    # не может по построению: агент не мог сделать validate зелёным, не
    # удалив пояснение из протокола. Аналогично исключены date/author/
    # coordination_note — старые записи законно их не имеют.
    schema = protocol.get("handoff_entry_schema", {})
    optional = ("date", "author", "coordination_note", "next_agent_hint")
    required = [k for k in schema
                if k not in optional and not k.startswith("_")]

    # next_agent_hint обязателен НЕ ВСЕГДА, а только когда работа не доделана.
    # Объявлять его обязательным всегда значило, что завершённая задача без
    # подсказки следующему агенту (а таких большинство) роняла validate.
    # Правило «обязателен, если status != DONE» записано в документации,
    # и код обязан ему следовать, иначе инструкция врёт.
    #
    # ABANDONED группируется с незавершёнными, а не с DONE: брошенная работа
    # — это ровно тот случай, ради которого подсказка и нужна.
    def _missing_fields(entry: dict) -> list[str]:
        missing = [k for k in required if not entry.get(k)]
        status = str(entry.get("status") or "DONE").upper()
        if status != "DONE" and not entry.get("next_agent_hint"):
            missing.append("next_agent_hint")
        return missing

    for t in handoff.get("task_log", []):
        name = (t.get("task") or "(без имени)")[:55]
        missing = _missing_fields(t)
        if missing:
            if t.get("legacy_entry"):
                warnings.append(f"legacy-запись '{name}': нет {missing}")
            else:
                problems.append(f"запись '{name}': нет {missing}")
        if not t.get("author"):
            warnings.append(f"запись '{name}': не указан автор")

    # 3. claim'ы на существующие файлы
    for agent, c in (handoff.get("team_protocol", {}).get("active_claims") or {}).items():
        if is_stale_claim(c):
            warnings.append(f"claim {agent} устарел (без heartbeat > {stale_after_minutes()} мин) — файлы свободны")
            continue
        for f in c.get("files", []):
            if not (ROOT / f).exists():
                warnings.append(f"claim {agent}: файл не существует — {f}")

    # 4. висячие ссылки протокола на каталоги/файлы
    for zone, area in (protocol.get("zones") or {}).items():
        if zone.startswith("_"):
            continue
        for pat in iter_path_tokens(area):
            if "*" in pat or pat.startswith("future"):
                continue
            # dot-файлы (.agent-handoff.json) и голые имена должны проверяться
            # относительно корня как есть; iter_path_tokens срезает "./", поэтому
            # кандидат берётся и с исходным именем.
            candidates = [ROOT / pat]
            if not any(c.exists() for c in candidates):
                warnings.append(f"зона {zone}: путь не существует — {pat}")

    # 5. дубли подсказок
    hints = handoff.get("hints", [])
    seen: dict[str, int] = {}
    for h in hints:
        seen[h] = seen.get(h, 0) + 1
    for h, n in seen.items():
        if n > 1:
            warnings.append(f"дубль подсказки x{n}: {h[:70]}")

    # 6. журнал статуса на месте и пригоден для записи
    #    Раньше здесь стояло правило single-writer: владельцем журнала был один
    #    назначенный агент. Правило было проверяемым и работало — и именно
    #    поэтому было проблемой: как только этот агент отключался, никто не мог
    #    отметить свой claim, и журнал замирал. Теперь журнал self-service, и
    #    единственное требование — он существует и пригоден для записи.
    journal_path = STATUS_JOURNAL
    if not (ROOT / journal_path).exists():
        problems.append(f"журнал статуса {journal_path} не существует")
    else:
        jd = read_json(STATUS_JOURNAL)
        if not any(k in jd for k in STATUS_SECTIONS):
            problems.append(f"журнал статуса {journal_path} пуст или не похож на журнал")
        leftover = [k for k in (protocol.get("zones") or {}) if k.startswith("_")]
        if leftover:
            warnings.append(f"служебные подразделы в zones: {leftover}")

    print(f"task_log: {len(handoff.get('task_log', []))}  hints: {len(hints)}  "
          f"claims: {list(handoff.get('team_protocol', {}).get('active_claims') or {})}")
    print(f"\nпроблемы ({len(problems)}):")
    for p in problems:
        print(f"  [FAIL] {p}")
    print(f"\nпредупреждения ({len(warnings)}):")
    for w in warnings[:20]:
        print(f"  [WARN] {w}")
    if len(warnings) > 20:
        print(f"  ... и ещё {len(warnings) - 20}")
    return 1 if problems else 0


def author_markers(handoff_data: dict) -> list[str]:
    """Имена, по которым можно опознать автора внутри текста записи.

    Порядок источников:
      1. protocol.normalisation.author_markers — явное задание проекта;
      2. имена авторов, уже присутствующие в этом же task_log.

    Второй источник не выдумывает ничего: он лишь переиспользует то, что уже
    записано в этом репозитории, и нужен, чтобы восстановление автора работало
    на новом шаблоне без правки кода.
    """
    protocol = read_json(PROTOCOL)
    explicit = ((protocol.get("normalisation") or {}).get("author_markers") or [])
    markers = [str(name) for name in explicit if str(name).strip()]
    if markers:
        return markers
    seen: list[str] = []
    for entry in handoff_data.get("task_log", []):
        author = entry.get("author")
        if author and author not in seen:
            seen.append(str(author))
    return seen


def cmd_normalize_legacy(args) -> int:
    """Приводит старые записи и claim'ы к актуальной схеме.

    Ничего не выдумывает: 
      • task_log: author ставится только если прямо написан в названии; записи
        без обязательных полей помечаются флагом legacy_entry;
      • active_claims: недостающие started_at/heartbeat_at/status заполняются,
        но heartbeat_at = started_at (реальное значение неизвестно, и мы это не
        маскируем — Heartbeat обновится при следующем heartbeat/claim).
    """
    mtime = HANDOFF.stat().st_mtime
    data = read_json(HANDOFF)
    schema = read_json(PROTOCOL).get("handoff_entry_schema", {})
    required = [k for k in schema if k not in ("date", "author", "coordination_note")]
    tagged = inferred = 0
    for t in data.get("task_log", []):
        missing = [k for k in required if not t.get(k)]
        if missing and not t.get("legacy_entry"):
            t["legacy_entry"] = True
            t["legacy_note"] = "Запись старше согласованной схемы task_log; поля восстановлены не были."
            tagged += 1
        # Автор восстанавливается независимо от полноты остальных полей:
        # иначе записи с полным телом, но без автора оставались безымянными.
        #
        # Список имён НЕ зашит в код: он живёт в протоколе
        # (normalisation.author_markers), иначе шаблон уехал бы в новый проект
        # вместе с чужими именами. Если маркеры не заданы, используются имена,
        # уже встречающиеся в этом же журнале, — восстановить автора можно
        # только из текста самой записи, и это честнее, чем угадывать.
        if not t.get("author"):
            name = t.get("task", "")
            for candidate in author_markers(data):
                if f"by {candidate}" in name:
                    t["author"] = candidate
                    inferred += 1
                    break

    claims = data.get("team_protocol", {}).get("active_claims", {})
    claim_fields_added = 0
    for agent, c in claims.items():
        if not c.get("agent"):
            c["agent"] = agent
            claim_fields_added += 1
        if not c.get("started_at"):
            c["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            claim_fields_added += 1
        if not c.get("heartbeat_at"):
            c["heartbeat_at"] = c["started_at"]
            claim_fields_added += 1
        if not c.get("status"):
            c["status"] = "IN_PROGRESS"
            claim_fields_added += 1

    if tagged == 0 and inferred == 0 and claim_fields_added == 0:
        print("нечего нормализовать")
        return 0
    write_json_atomic(HANDOFF, data, mtime)
    print(f"task_log: помечено legacy: {tagged}; автор восстановлен: {inferred}")
    print(f"claims: добавлено недостающих полей: {claim_fields_added}")
    return 0


def cmd_repair_encoding(args) -> int:
    targets = [HANDOFF, PROTOCOL]
    total = 0
    for path in targets:
        if not path.exists():
            continue
        mtime = path.stat().st_mtime
        data = read_json(path)
        repaired, fixed = repair_mojibake_in_place(data)
        print(f"{path.name}: исправлено строк — {fixed}")
        if fixed == 0:
            continue
        if args.dry_run:
            print("  (dry-run: файл не изменён)")
            total += fixed
            continue
        BACKUP_DIR.mkdir(exist_ok=True)
        backup = BACKUP_DIR / f"{path.stem}-{int(time.time())}.json"
        shutil.copy2(path, backup)
        write_json_atomic(path, repaired, mtime)
        after = read_json(path)
        remaining = sum(
            1 for v in json.dumps(after, ensure_ascii=False).split('"') if any(p in v for p in MOJIBAKE_PAIRS)
        )
        print(f"  записано; бэкап: {backup.name}; остаточных mojibake-строк: {remaining}")
        total += fixed
    print(f"\nитого исправлено строк: {total}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="agent_handoff", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="показать claim/owners/hints").set_defaults(func=cmd_status)

    c = sub.add_parser("claim", help="занять область")
    c.add_argument("agent")
    c.add_argument("task")
    c.add_argument("--files", nargs="+", default=None,
                   help="файлы через пробел или запятую (обязательно при коллизии имён)")
    c.add_argument("--force", action="store_true",
                   help="занять файл, даже если он под чужим claim (осознанный приём)")
    c.add_argument("--take-over", action="store_true",
                   help="забрать claim, который держит ЖИВАЯ сессия с тем же именем. "
                        "Обычно это ошибка: перезапущенная сессия должна взять "
                        "имя вида '<имя>/2'")
    c.set_defaults(func=cmd_claim)

    hb = sub.add_parser("heartbeat", help="отметить, что агент жив (задачи > 30 мин)")
    hb.add_argument("agent")
    hb.add_argument("--note", default=None, help="короткий статус прогресса")
    hb.set_defaults(func=cmd_heartbeat)

    bl = sub.add_parser("backlog", help="приоритеты P0-P4 в .agent-protocol.json")
    bl.add_argument("action", choices=["list", "add", "set"])
    bl.add_argument("--priority", default=None, choices=["P0", "P1", "P2", "P3", "P4"])
    bl.add_argument("--description", default=None)
    bl.add_argument("--area", default=None)
    bl.add_argument("--id", default=None, help="для set")
    bl.add_argument("--status", default=None,
                    choices=["open", "claimed", "done", "blocked"],
                    help="новый статус; по умолчанию НЕ меняется (P2-009)")
    bl.add_argument("--agent", default="unknown")
    bl.set_defaults(func=cmd_backlog)

    sub.add_parser("health-check", help="health check команды (раздел 15)").set_defaults(
        func=cmd_health_check
    )

    r = sub.add_parser("release", help="снять claim")
    r.add_argument("agent")
    r.set_defaults(func=cmd_release)

    le = sub.add_parser("log-entry", help="добавить запись в task_log")
    le.add_argument("--agent", required=True)
    le.add_argument("--file", default=None, help="JSON-файл с записью")
    le.add_argument("--json", default=None, help="JSON-строка с записью")
    le.set_defaults(func=cmd_log_entry)

    h = sub.add_parser("hint", help="добавить или удалить подсказку коллегам")
    h.add_argument("text", nargs="?", default=None)
    h.add_argument("--remove", default=None, help="текст подсказки или её индекс")
    h.set_defaults(func=cmd_hint)

    sub.add_parser("validate", help="проверить координатные файлы").set_defaults(func=cmd_validate)

    vo = sub.add_parser("who-owns", help="кто владеет файлом и не занят ли он")
    vo.add_argument("paths", nargs="+")
    vo.set_defaults(func=cmd_who_owns)

    sub.add_parser("normalize-legacy", help="пометить старые записи как legacy").set_defaults(
        func=cmd_normalize_legacy
    )

    rp = sub.add_parser("repair-encoding", help="починить mojibake")
    rp.add_argument("--dry-run", action="store_true")
    rp.set_defaults(func=cmd_repair_encoding)

    ps = sub.add_parser("protocol-set", help="обновить ключ в .agent-protocol.json")
    ps.add_argument("key")
    ps.add_argument("json", nargs="?", default=None, help="JSON-значение ключа")
    ps.add_argument("--file", default=None, help="взять JSON-значение из файла")
    ps.set_defaults(func=cmd_protocol_set)

    ss = sub.add_parser("status-set", help="атомарно обновить docs/agent-status.json")
    ss.add_argument("key", help="верхнеуровневый ключ или путь agents.<имя>.status")
    ss.add_argument("json", nargs="?", default=None, help="JSON-значение (для append/remove — элемент)")
    ss.add_argument("--file", default=None, help="взять JSON-значение из файла")
    ss.add_argument("--action", choices=["auto", "replace", "append", "remove"], default="auto",
                    help="auto: список + скаляр = append, иначе replace")
    ss.add_argument("--agent", required=True, help="ваше имя агента (пишете свой раздел)")
    ss.add_argument("--updated-by", default=None, help="явно указать updated_by в журнале")
    ss.add_argument("--force", action="store_true",
                    help="перезаписать слот ДРУГОГО агента (осознанный приём)")
    ss.add_argument("--override-live", action="store_true",
                    help="перезаписать слот агента, который выглядит живым: "
                         "у него есть свежий heartbeat или активный claim. "
                         "Действует только вместе с --force")
    ss.set_defaults(func=cmd_status_set)

    return ap


def main() -> int:
    args = build_parser().parse_args()
    if args.cmd == "log-entry" and not args.file and not args.json:
        print("[ERR] нужен --file или --json", file=sys.stderr)
        return 2
    if args.cmd == "protocol-set" and not args.file and not args.json:
        print("[ERR] нужен JSON-аргумент или --file", file=sys.stderr)
        return 2
    if args.cmd == "status-set" and not args.file and args.json is None:
        print("[ERR] нужен JSON-аргумент или --file", file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except RuntimeError as exc:  # оптимистичная блокировка сработала
        print(f"[WARN] {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
