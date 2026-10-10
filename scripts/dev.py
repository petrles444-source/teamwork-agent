"""Оркестратор проверок — логика, стоящая за `make gates` и `make test`.

Зачем отдельный скрипт, а не Makefile: `make` есть не везде (на Windows его
часто нет), а непроверяемый Makefile нельзя исполнять в тестах. Логика живёт
здесь и покрыта тестами, Makefile — тонкая обёртка над ней.

Универсальный шаблон. Скрипт НЕ знает предметной области проекта: список
гейтов задаётся либо здесь (минимальный набор, работающий в любом
Python-репозитории), либо переопределяется файлом `scripts/gates.json`
списком объектов вида:

    {"name": "my_check", "args": ["-u", "scripts/my_check.py"],
     "note": "что он защищает", "requires": "data/*.csv"}

`requires` — glob-шаблон входных данных относительно корня репозитория.
Если шаблон не матчится, гейт ПРОПУСКАЕТСЯ, а не падает: отсутствие данных
означает «окружение не готово», а не «регрессия». Но пропуск показывается
в сводке явно и никогда не выдаётся за успех — иначе «прогнал гейты» снова
станет неотличимо от «прогнал половину гейтов».

Подкоманды:
    doctor     показать, что готово, а что нет (ничего не чинит)
    test       прогнать pytest
    gates      прогнать ВСЕ гейты и показать сводку
    status     короткая картина команды через agent_handoff.py
    clean      убрать сгенерированное

Все команды идемпотентны и не требуют make.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

VENV = ROOT / ".venv"
GATES_JSON = Path(__file__).resolve().parent / "gates.json"


def py() -> str:
    """Путь к интерпретатору venv; если venv нет — системный.

    Шаблон не требует venv: если `.venv` отсутствует, работает системный
    интерпретатор. Требование «обязательно venv» было верно для проекта с
    тяжёлым стеком и стало ловушкой здесь — шаблон ставят в репозиторий,
    где venv может и не быть, и требование превращалось в блокировку.
    """
    candidate = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(candidate) if candidate.exists() else sys.executable


def _has(module: str) -> bool:
    """Установлен ли модуль. Импорт проверяется безопасно: отсутствие
    необязательного инструмента — это диагностический факт, а не повод
    ронять `doctor`, который как раз объясняет, чего не хватает."""
    try:
        __import__(module)
        return True
    except ImportError:
        return False


#: (имя для импорта, как называть в выводе, зачем он нужен)
TOOLS = [
    ("pytest", "pytest", "тесты"),
    ("ruff", "ruff", "линт"),
    ("fastapi", "fastapi", "дашборд"),
    ("uvicorn", "uvicorn", "дашборд"),
]


def run(args: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(args)}", flush=True)
    return subprocess.run(args, **kw)


def banner(text: str) -> None:
    print(f"\n{'=' * 60}\n{text}\n{'=' * 60}", flush=True)


# ── doctor ──────────────────────────────────────────────────────────────
def doctor() -> int:
    """Диагностика готовности. Ничего не чинит и не запускает."""
    banner("DOCTOR")
    problems = 0

    print(f"python:      {py()}")
    print(f"  venv:      {'да' if VENV.exists() else 'НЕТ (используется системный)'}")

    print("\nкоординационные файлы:")
    required = [
        (ROOT / ".agent-protocol.json", "правила: зоны, гейты, приоритеты"),
        (ROOT / ".agent-handoff.json", "состояние: claims, task_log, hints"),
        (ROOT / "docs" / "agent-status.json", "журнал статуса"),
    ]
    for path, why in required:
        ok = path.exists()
        print(f"  {'[ok]  ' if ok else '[MISS]'} {path.relative_to(ROOT)} — {why}")
        if not ok:
            problems += 1

    print("\nинструменты (каждый нужен только своему гейту):")
    for module, label, why in TOOLS:
        mark = '[ok]  ' if _has(module) else '[MISS]'
        print(f'  {mark} {label:10s} {why}')

    # Отсутствие инструмента и отсутствие данных — РАЗНЫЕ вещи, и
    # различать их обязательно. Первое чинится установкой пакета,
    # второе — созданием данных. Показывать оба как SKIP значило бы
    # научить агента читать «проверка не выполнена» как «проверка не
    # нужна», и он начнёт пропускать настоящие падения.
    if not _has("pytest") or not _has("ruff"):
        print()
        print('  -> pip install -e "[dev]" (pytest, ruff). '
              "Без ruff гейт lint КРАСНЫЙ: это не пропуск.")

    print("\nгейты:")
    # Три разные причины, по которым гейт не пройдёт, и каждая лечится
    # своим способом. Смешивать их нельзя: отсутствие данных — это «окружение
    # не готово», отсутствие скрипта — «гейт настроен не на тот проект», а
    # отсутствие инструмента — «не установлено». Показывать всё одним
    # словом значит не сказать ничего.
    broken_gates = []
    for gate in load_gates():
        missing_scripts = gate.missing_scripts()
        if missing_scripts:
            state = "НЕТ СКРИПТА"
            broken_gates.append(f"{gate.name}: нет {', '.join(missing_scripts)}")
        elif gate.missing_input():
            state = "SKIP (нет данных)"
        elif gate.needs_missing_tool():
            state = "НЕТ ИНСТРУМЕНТА"
            broken_gates.append(f"{gate.name}: не установлен {gate.needs}")
        else:
            state = "ok"
        print(f"  [{state:16s}] {gate.name} — {gate.note}")

    if broken_gates:
        print("\n  ЭТИ ГЕЙТЫ НЕ ПРОЙДУТ:")
        for item in broken_gates:
            print(f"    - {item}")
        print("    Напишите скрипт, укажите другой путь или уберите гейт из "
              "scripts/gates.json.")
        print("    Пока они в списке, каждый прогон `gates` будет красным — "
              "а красный гейт, который нельзя починить, приучает не читать "
              "остальные.")

    if GATES_JSON.exists():
        print(f"\nсписок гейтов переопределён: {GATES_JSON.relative_to(ROOT)}")
    else:
        print("\nсписок гейтов: встроенный минимальный набор.")
        print("  Свой проект: скопируйте scripts/gates.example.json "
              "в scripts/gates.json и допишите гейты.")

    docker = shutil.which("docker")
    print(f"\ndocker: {docker or 'НЕТ (обычно не требуется)'}")

    banner(f"ИТОГ: проблем {problems}")
    return 0 if problems == 0 else 1


# ── гейты ───────────────────────────────────────────────────────────────
@dataclass
class Gate:
    """Один гейт: имя, команда и объяснение, что он защищает.

    `requires` — glob-шаблон входных данных относительно корня
    репозитория. Если он не матчится, гейт ПРОПУСКАЕТСЯ, а не падает:
    отсутствие данных означает «окружение не готово», а не «регрессия».
    Но пропуск показывается в сводке явно и никогда не выдаётся за
    успех — иначе «прогнал гейты» снова станет неотличимо от
    «прогнал половину гейтов».
    """

    name: str
    args: list[str]
    note: str = ""
    requires: str = ""
    #: Python-модуль, без которого гейт не запустится. Используется только
    #: doctor'ом для предупреждения; сам гейт в этом случае честно падает,
    #: потому что отсутствие линтера — это не «нет данных», а сломанное
    #: окружение, и путать их нельзя.
    needs: str = ""

    def missing_input(self) -> bool:
        return bool(self.requires) and not any(ROOT.glob(self.requires))

    def needs_missing_tool(self) -> bool:
        return bool(self.needs) and not _has(self.needs)

    def missing_scripts(self) -> list[str]:
        """Файлы из args, которых нет в репозитории.

        Отдельно от `needs` (не установлен пакет) и от `requires` (нет
        данных). Три причины, почему гейт не пройдёт, и агент должен видеть
        их все ДО прогона: отсутствующий инструмент лечится установкой,
        отсутствующие данные — их созданием, отсутствующий скрипт — его
        написанием или удалением гейта.

        Без этой проверки doctor показывал гейт со ссылкой на несуществующий
        скрипт как исправный, и человек узнавал о проблеме только из
        красного прогона — то есть уже после того, как потратил время.
        """
        missing = []
        for arg in self.args:
            if arg.startswith("-"):
                continue
            # Путь внутри репозитория: есть разделитель и нет пробелов.
            if "/" not in arg or " " in arg:
                continue
            if not (ROOT / arg).exists():
                missing.append(arg)
        return missing

    def needs_missing_script(self) -> bool:
        return bool(self.missing_scripts())


FLAKY = "МЕРЦАНИЕ"


#: Минимальный набор, работающий в ЛЮБОМ Python-репозитории.
#: Каждый гейт либо уже есть у каждого (pytest), либо проверяет целостность
#: самого шаблона координации (lint, coordination). Предметные гейты
#: добавляются проектом в scripts/gates.json.
DEFAULT_GATES: list[Gate] = [
    Gate("pytest", ["-m", "pytest", "-q"],
         note="все тесты", needs="pytest"),
    Gate("lint", ["-m", "ruff", "check", "scripts", "tests"],
         note="стиль и типовые ошибки", needs="ruff"),
    Gate("coordination", ["scripts/agent_handoff.py", "validate"],
         note="целостность coordination-файлов"),
]


def load_gates() -> list[Gate]:
    """Читает scripts/gates.json, если он есть; иначе — минимальный набор.

    Список гейтов — конфигурация проекта, а не кода: он меняется чаще, чем
    сам раннер, и не должен требовать правки кода. Файл читается при каждом
    запуске, поэтому агент может добавить гейт и сразу им проверить.
    """
    if not GATES_JSON.exists():
        return list(DEFAULT_GATES)
    try:
        raw = json.loads(GATES_JSON.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        print(f"[WARN] {GATES_JSON.name} не читается ({exc}); беру встроенный набор",
              file=sys.stderr)
        return list(DEFAULT_GATES)
    gates = [Gate(g["name"], list(g.get("args", [])), g.get("note", ""),
                  g.get("requires", ""), g.get("needs", "")) for g in raw]
    return gates or list(DEFAULT_GATES)


def _failing_names(output: str, limit: int = 10) -> list[str]:
    """Имена тестов, которые упали, из вывода pytest.

    Нужны не для красоты, а для координации: упавший тест сразу указывает
    файл, а файл указывает, чей это claim. Иначе агент, увидевший красный
    pytest, вынужден перезапускать гейт целиком, чтобы понять, его ли это.
    """
    names: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(("FAILED ", "ERROR ")):
            # Формат pytest: 'FAILED tests/test_x.py::test_y - причина'.
            # Имя - второй элемент, всё после ' - ' уже сообщение.
            parts = stripped.split(" ", 2)
            name = parts[1] if len(parts) > 1 else ""
            name = name.split(" - ")[0].strip()
            if name and name not in names:
                names.append(name)
    return names[:limit]


def gates() -> int:
    """Прогнать ВСЕ гейты разом и показать сводку.

    Зачем это нужно. За одну сессию каждый инцидент свёлся к одному
    виду: красный гейт или непроверенное число, которое никто не заметил.
    Гейты запускались по отдельности, и неполный набор выглядит точно так
    же, как полный, пока не проверишь.

    Принципиально: выполнение НЕ останавливается на первом падении.
    Иначе после первого красного гейта остальные остались бы
    непроверенными, и «прогнал гейты» означало бы «увидел первое
    падение и ушёл».

    Режима `--fast` здесь нет намеренно. Он существовал, когда
    необязательные гейты считались медленными; замер показал, что все
    лёгкие гейты укладываются в единицы секунд, и флаг пропускал бы почти
    ничего, назывался «быстрым», и агент, его запустивший, был бы уверен,
    что прогнал все гейты.
    """
    banner("GATES")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    selected = load_gates()

    print(f"гейтов к прогону: {len(selected)}\n")

    results: list[tuple[Gate, str, float, list[str]]] = []
    for index, gate in enumerate(selected, 1):
        label = f"[{index}/{len(selected)}] {gate.name}"
        print(f"{label} — {gate.note}")
        print("-" * 70, flush=True)

        if gate.missing_input():
            print("-" * 70, flush=True)
            results.append((gate, "SKIP", 0.0, []))
            print(f"{label}: ПРОПУЩЕН (нет {gate.requires})\n", flush=True)
            continue

        started = time.monotonic()
        code = run([py(), *gate.args], cwd=ROOT, env=env).returncode
        failing: list[str] = []

        if code == 0:
            status = "OK"
        else:
            # Красный гейт перезапускается ОДИН раз.
            #
            # Зачем. Гейт, который «врёт», хуже отсутствующего: агент, у
            # которого гейт красный три раза подряд, начинает считать его
            # фоном. Самая частая причина: файл правит другой агент, пока
            # pytest его читает, а отдельный `pytest -q` сразу после даёт
            # полный зелёный прогон. Отличить «сломан» от «тронут прямо
            # сейчас» можно только повтором, и стоит это одного запуска —
            # причём только при падении, когда зелёный прогон дорогого гейта
            # не повторяется.
            print(f"  [повтор] {label} красный. Перезапускаю один раз, "
                  "чтобы отличить устойчивое падение от правки на лету:",
                  flush=True)
            again = run([py(), *gate.args], cwd=ROOT, env=env,
                        capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
            retry_out = ((getattr(again, "stdout", "") or "")
                         + (getattr(again, "stderr", "") or ""))
            if retry_out.strip():
                print(retry_out.rstrip()[-4000:], flush=True)
            failing = _failing_names(retry_out)
            code = again.returncode
            status = FLAKY if code == 0 else "FAIL"

        elapsed = time.monotonic() - started
        results.append((gate, status, elapsed, failing))
        verdict = {"OK": "OK", "FAIL": f"ПАДЕНИЕ (код {code})",
                   FLAKY: "БЫЛ КРАСНЫМ, ПОВТОР ЗЕЛЁНЫЙ"}[status]
        print("-" * 70)
        print(f"{label}: {verdict}, {elapsed:.1f} с\n", flush=True)

    banner("ИТОГ")
    width = max(len(g.name) for g in selected)
    for gate, status, elapsed, _failing in results:
        mark = {"OK": "OK  ", "FAIL": "FAIL", "SKIP": "SKIP",
                FLAKY: "МЕРЦ"}[status]
        print(f"  {mark}  {gate.name:<{width}}  {elapsed:>7.1f} с")

    failed = [g.name for g, s, _, _ in results if s == "FAIL"]
    flaky = [g.name for g, s, _, _ in results if s == FLAKY]
    skipped = [g.name for g, s, _, _ in results if s == "SKIP"]
    ok = [g.name for g, s, _, _ in results if s == "OK"]

    print(f"\n  зелёных: {len(ok)}, красных: {len(failed)}, "
          f"мерцаний: {len(flaky)}, пропущено: {len(skipped)}")
    if skipped:
        print(f"пропущены (нет входных данных): {', '.join(skipped)} — "
              f"это НЕ успех. Если ты трогал соответствующую часть проекта, "
              f"проверка фактически не выполнена.", file=sys.stderr)
    for gate, status, _elapsed, failing in results:
        if status == "FAIL" and failing:
            print(f"упавшие тесты в {gate.name}: {', '.join(failing)}",
                  file=sys.stderr)
    if flaky:
        print(f"МЕРЦАНИЕ: {', '.join(flaky)} — первый прогон красный, "
              "повтор зелёный, поэтому код возврата 0. Почти всегда это "
              "правка файла другим агентом прямо во время прогона. Но это "
              "НЕ «всё хорошо, можно сдавать»: если тест падает у тебя "
              "постоянно, это регрессия, а не мерцание.", file=sys.stderr)
    if failed:
        print(f"КРАСНЫЕ ГЕЙТЫ: {', '.join(failed)}", file=sys.stderr)
        print("Работа не сдана, пока красное.", file=sys.stderr)
        return 1
    if skipped:
        # Замыкающий кейс: зелёных 0, красных 0, пропущено N. Печатать
        # здесь «красных гейтов нет» было бы враньём — ни одна проверка
        # не выполнилась, а строка выглядит как чистый лист.
        print(f"красных нет, но {len(skipped)} гейтов пропущено — "
              f"проверка НЕПОЛНАЯ")
        return 0
    print("красных гейтов нет")
    return 0


# ── прочее ──────────────────────────────────────────────────────────────
def test() -> int:
    banner("TEST")
    return run([py(), "-m", "pytest", "-q"], cwd=ROOT,
               env={**os.environ, "PYTHONPATH": str(ROOT)}).returncode


def status() -> int:
    """Короткая картина команды: делегирует единственному инструменту записи."""
    banner("STATUS")
    return run([py(), "scripts/agent_handoff.py", "status"], cwd=ROOT,
               env={**os.environ, "PYTHONPATH": str(ROOT)}).returncode


def clean() -> int:
    """Убрать сгенерированное. Данные проекта не трогает."""
    banner("CLEAN")
    removed = 0
    for pattern in (".pytest_cache", ".ruff_cache", ".mypy_cache",
                    ".benchmarks", "__pycache__", ".agent-handoff.backups"):
        for path in ROOT.glob(pattern):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
                print(f"удалён каталог {pattern}")
                removed += 1
            elif path.is_file():
                path.unlink()
                print(f"удалён файл {pattern}")
                removed += 1

    # Временные каталоги агентов: их появление — ловушка temp_trap из
    # протокола (tempfile.gettempdir() иногда указывает на корень копии).
    for path in ROOT.glob("tmp*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            print(f"удалён каталог {path.name}")
            removed += 1

    print(f"\nготово, удалено {removed}. Данные проекта (data/, артефакты "
          f"прогона) НЕ трогаю — это данные.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Оркестратор проверок (шаблон teamwork-agent)")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="диагностика готовности")
    sub.add_parser("test", help="pytest")
    sub.add_parser("status", help="картина команды (delegates в agent_handoff.py)")
    sub.add_parser("clean", help="убрать сгенерированное")

    p_gates = sub.add_parser("gates", help="прогнать ВСЕ гейты разом и показать сводку")
    p_gates.add_argument("--list", action="store_true",
                         help="только показать список гейтов, не запускать")

    args = ap.parse_args()
    if args.command == "doctor":
        return doctor()
    if args.command == "test":
        return test()
    if args.command == "status":
        return status()
    if args.command == "clean":
        return clean()
    if args.command == "gates":
        if args.list:
            for gate in load_gates():
                state = "SKIP (нет " + gate.requires + ")" if gate.missing_input() else "ok"
                print(f"  {gate.name:16s} {state:40s} {gate.note}")
            return 0
        return gates()
    return 1


if __name__ == "__main__":
    sys.exit(main())
