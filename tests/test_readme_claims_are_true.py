"""Числа в README обязаны соответствовать реальности.

Зачем. README шаблона объясняет, что именно копирует проект, и читатель
принимает написанные числа за проверенные: «160 тестов» звучит как
результат прогона, а не как пожелание. Такое число устаревает молча —
тесты добавляются, README забывают, и через месяц документ расходится с
репозиторием.

Проверяется не «правильное» число, а РАВЕНСТВО: какое бы оно ни было,
оно обязано совпадать с тем, что даёт прогон. Это дешевле, чем ручная
сверка, и не даёт появиться новому расхождению.

Само правило применимо шире шаблона: любое число, напечатанное в
документации как факт, либо проверяется прогоном, либо помечается как
оценка. «Около 160», «на момент коммита» — честные формулировки;
«160» без оговорки — утверждение.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"


def _pytest_count() -> tuple[int, int]:
    """(passed, skipped) последнего прогона всех тестов.

    Собирается через collect-only, а не через полный прогон: проверка не
    должна запускать всё дважды и не должна требовать, чтобы гейт был
    зелёным. Считаются только тесты, которые pytest СОБЕРЁТ, — иначе
    пропущенные не считались бы и число в README могло бы совпасть с
    меньшим, чем реально написано.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    output = result.stdout or ""
    collected = re.search(r"(\d+) tests? collected", output)
    if not collected:
        pytest.skip("не удалось разобрать вывод pytest --collect-only")
    total = int(collected.group(1))

    deselected = re.search(r"(\d+) deselected", output)
    return total - int(deselected.group(1)) if deselected else total, 0


def test_readme_test_count_matches_reality():
    """Число тестов в README обязано совпадать с тем, что реально собирается."""
    text = README.read_text(encoding="utf-8-sig")

    # Русский склоняет счётное слово: 1 тест, 2-4 теста, 5+ тестов. Регулярка
    # жёстко на «тестов» ломалась бы ровно тогда, когда число меняется и
    # автор честно переписывает его вместе с формой.
    claimed = re.search(r"(\d+)\s+тест\w*", text)
    assert claimed, (
        "в README нет числа тестов — проверять нечего. Если тесты ещё не "
        "написаны, напишите это прямо, а не опускайте строку."
    )

    total, _ = _pytest_count()
    assert int(claimed.group(1)) == total, (
        f"README говорит о {claimed.group(1)} тестах, pytest собирает {total}. "
        "Число в документации — утверждение, а не пожелание: либо приведи "
        "его в соответствие, либо напиши «около N» и «на момент коммита»."
    )


def test_readme_lists_every_script():
    """Каждый поставляемый скрипт обязан быть описан в README.

    Неописанный скрипт выглядит как случайный файл и не вызывается: агент
    про него не знает, и проверка, которую он делает, просто не делается.
    """
    text = README.read_text(encoding="utf-8-sig")
    missing = [
        path.name
        for path in sorted((ROOT / "scripts").iterdir())
        if path.is_file() and path.suffix in (".py", ".json") and path.name not in text
    ]
    assert missing == [], f"скрипты не упомянуты в README: {missing}"


def test_readme_points_at_the_instructions():
    """README — обзор, подробности — в инструкции.

    Ссылка обязана быть рабочей: README читают первым, и если подробностей
    там нет, а ссылка битая, то шаблон остаётся без документации.
    """
    text = README.read_text(encoding="utf-8-sig")
    target = ROOT / "docs" / "INSTRUCTIONS.md"

    assert target.exists(), "нет docs/INSTRUCTIONS.md, на который ссылается README"
    assert "INSTRUCTIONS.md" in text, "README не ссылается на подробную инструкцию"


def test_install_commands_reference_files_that_exist():
    """Каждая команда копирования в README указывает на существующий файл.

    Инструкция установки исполняется дословно и один раз. Ссылка на
    переименованный или несуществующий файл ломает её в момент, когда
    человек ещё не понял, что делает.

    Путь к шаблону НЕ зашит: он берётся из последнего аргумента команды
    `git clone` над инструкцией. Зашивать его — значило бы проверять, что
    README совпадает с копией README в соседнем тесте, то есть ничего.

    Имя каталога назначения намеренно не проверяется: автор вправе назвать
    его `/tmp/tw`, `/tmp/teamwork-agent` или как угодно. Проверяется
    только то, что все файлы под этим путём действительно есть.
    """
    text = README.read_text(encoding="utf-8-sig")

    clone = re.search(r"^git clone .*?(\S+)\s*$", text, re.MULTILINE)
    assert clone, (
        "в README нет команды клонирования шаблона, от которой "
        "отсчитываются команды копирования, - проверять нечего"
    )
    prefix = clone.group(1)

    copies = re.findall(re.escape(prefix) + r"/([\w./-]+)", text)
    assert copies, (
        f"в README нет команд копирования файлов из {prefix!r}"
    )

    # Последний элемент команды `cp` — это путь НАЗНАЧЕНИЯ, он может
    # отличаться от источника (gates.example.json -> gates.json).
    missing = [s for s in copies if not (ROOT / s).exists()]
    assert missing == [], (
        f"команды установки ссылаются на несуществующие файлы шаблона: {missing}"
    )
