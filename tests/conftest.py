"""Общая настройка и фикстуры для тестов шаблона.

Зачем фикстура здесь, а не в test_agent_handoff.py: её используют несколько
модулей тестов (проверки координации И проверки универсальности). Дублировать
фикстуру нельзя — две копии разъедутся, и тесты начнут проверять разное;
импортировать её из тест-модуля тоже плохо: pytest увидит `fake_repo` и в
импорте, и в сигнатуре функции и сочтёт это переопределением.

Единственная задача `sys.path` — сделать `scripts/` и корень репозитория
импортируемыми. Тесты в чужом репозитории запускаются как `python -m pytest -q`
из корня, и `scripts` там не пакет: импорт требовал бы либо установки
пакета, либо ручной правки `sys.path` в каждом тесте.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import scripts.agent_handoff as handoff  # noqa: E402


def run(*argv):
    """Разбирает argv НАСТОЯЩИМ CLI и вызывает соответствующую подкоманду.

    Через настоящий `build_parser`, а не прямым вызовом функции: иначе тесты
    проверяли бы логику подкоманд, но не разбор аргументов. Ошибка в
    определении флага (например `--take-over` без ведущих дефисов) при таком
    подходе прошла бы все тесты и сломала бы работу агента.
    """
    from scripts.agent_handoff import build_parser

    args = build_parser().parse_args(list(argv))
    return args.func(args)


def read(path):
    """Читает JSON координационного файла. BOM допускается, как в проде."""
    return json.loads(path.read_text(encoding="utf-8-sig"))


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """Изолированные координационные файлы.

    Реальные `.agent-*.json` и `docs/agent-status.json` НЕ затрагиваются.
    Это не аккуратность, а условие работоспособности тестов: проверка,
    создающая настоящий claim ради проверки, оставляет его в журнале, и
    следующий же прогон `health-check` краснеет по собственной вине —
    то есть шаблон проверяет сам себя и портит собственное состояние.

    Содержимое протокола минимально и нейтрально: никаких путей исходного
    проекта, потому что шаблон переносится в любой репозиторий.
    """
    protocol = tmp_path / ".agent-protocol.json"
    handoff_file = tmp_path / ".agent-handoff.json"
    status = tmp_path / "docs" / "agent-status.json"
    status.parent.mkdir(parents=True, exist_ok=True)

    protocol.write_text(
        json.dumps(
            {
                "protocol_version": "2.0",
                "zones": {
                    "coordination": [
                        "scripts/agent_handoff.py",
                        ".agent-protocol.json",
                        ".agent-handoff.json",
                        "docs/agent-status.json",
                    ],
                    "pipeline": ["src/analyzer/"],
                    "reserved": ["deploy/"],
                },
                "claims": {"stale_after_minutes": 90, "max_agents": 5},
                "backlog": [],
                # Схема записи нужна фикстуре не для красоты: без неё
                # `validate` не проверяет ничего, и тесты на обязательные
                # поля проходили бы на пустом списке required — то есть
                # проверяли бы несуществующую проверку.
                "handoff_entry_schema": {
                    "date": "YYYY-MM-DD",
                    "task": "краткое имя задачи",
                    "author": "имя агента",
                    "status": "DONE | IN_PROGRESS | BLOCKED | ABANDONED",
                    "description": "что изменилось и почему",
                    "changed_files": ["..."],
                    "verification": "точные команды",
                    "result": "измеренные числа",
                    "next_agent_hint": "обязательно, если status != DONE",
                    "coordination_note": "владение и договорённости",
                    "_comment": "служебный ключ: не поле записи",
                },
            }
        ),
        encoding="utf-8",
    )
    handoff_file.write_text(
        json.dumps({"team_protocol": {"active_claims": {}}, "task_log": [],
                    "hints": []}),
        encoding="utf-8",
    )
    status.write_text(
        json.dumps(
            {
                "updated_by": "someone",
                "updated_at": "2026-01-01 00:00",
                "priorities": {"P0": [], "P1": [], "P2": []},
                "agents": {"someone": {"status": "AVAILABLE"}},
                "hints": [],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(handoff, "PROTOCOL", protocol)
    monkeypatch.setattr(handoff, "STATUS", status)
    monkeypatch.setattr(handoff, "HANDOFF", handoff_file)
    monkeypatch.setattr(handoff, "ROOT", tmp_path)
    return protocol, handoff_file, status
