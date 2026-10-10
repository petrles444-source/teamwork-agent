"""Тесты универсальности шаблона.

Зачем этот файл. Шаблон существует, чтобы быть перенесённым в чужой
репозиторий. Самая вероятная его смерть — не поломка логики, а незаметное
приращение проектных хвостов: в прошлый раз код и протокол наросли
именами, путями и командами конкретного проекта, и шаблон перестал быть
шаблоном, оставаясь внешне похожим.

Поэтому здесь проверяется не «работает ли», а «осталось ли что-то чужое»:

* в координационных файлах и в коде нет имён конкретных агентов и путей
  конкретного проекта;
* имена для normalize-legacy берутся из протокола, а не зашиты в код;
* проектные части протокола (зоны, гейты, контракты, границы) помечены как
  требующие заполнения — иначе агент примет заглушку за проектные знания;
* минимальный набор гейтов запускается в репозитории, где нет ни одного
  гейта проекта.

ГДЕ ЭТИ ТЕСТЫ ИМЕЮТ СМЫСЛ
------------------------

Часть проверок ниже смотрит на СОСТОЯНИЕ репозитория и имеет смысл только
в чистом шаблоне: пустой `task_log`, пустой список агентов, зоны без чужих
путей. В установленном проекте они ломаются по прямому назначению — в журнале
появилась первая запись, и слот агента тоже.

Поэтому весь модуль пропускается, если репозиторий перестал быть шаблоном.
Признак один и намеренно грубый: в корне нет `README.md` шаблона, который
начинается со строки «# teamwork-agent». Если файла нет или он переписан под
проект — проверять тут нечего, и модуль молча уходит вместо того, чтобы
кричать на каждом прогоне.

Проверки, не зависящие от состояния репозитория (чистота кода, чтение
списка гейтов, маркеры схемы), от этого флага не зависят и работают везде.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

# Хелперы run/read и фикстура fake_repo живут в tests/conftest.py: ими
# пользуются и проверки координации, и проверки универсальности шаблона.
# Дублировать их нельзя — две копии разъедутся, и тесты начнут проверять
# разное. Шаблон проверяет себя теми же инструментами, которыми пользуются
# агенты, — иначе тесты шаблона и тесты координации проверяли бы разные вещи.
from conftest import read, run  # noqa: F401

import scripts.agent_handoff as handoff  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _is_pristine_template() -> bool:
    """Репозиторий всё ещё чистый шаблон, а не установленная копия.

    Признаков два, и оба нужны:

    * заголовок README — `# teamwork-agent`. Он переписывается первым же;
    * пустой `task_log` и пустой список агентов в журнале.

    Одного заголовка мало: инструкция по установке велит скопировать
    `README.md` как есть, поэтому в первые минуты работы в установленной
    копии заголовок ещё шаблонный. Одного журнала тоже мало — скопированный
    журнал пуст, и проверка решит, что это шаблон, пока агент не напишет
    первую запись.

    Логика «или-и»: как только выполнено хоть одно из двух, шаблон считается
    освоенным.
    """
    readme = ROOT / "README.md"
    if readme.exists():
        try:
            if not readme.read_text(encoding="utf-8-sig").startswith("# teamwork-agent"):
                return False
        except (OSError, UnicodeDecodeError):
            return False

    def _load(name: str) -> dict:
        try:
            return json.loads((ROOT / name).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}

    handoff = _load(".agent-handoff.json")
    status = _load("docs/agent-status.json")
    if handoff.get("task_log") or handoff.get("hints"):
        return False
    if (status.get("agents") or {}) or (status.get("current_gate") or {}):
        return False
    if handoff.get("team_protocol", {}).get("active_claims"):
        return False
    return True


#: Метка для тестов, смотрящих на состояние репозитория. Такие проверки
#: осмысленны только в чистом шаблоне — см. докстринг модуля.
pristine_only = pytest.mark.skipif(
    not _is_pristine_template(),
    reason="репозиторий уже не чистый шаблон: проверки состояния здесь "
           "неприменимы (см. докстринг модуля)",
)

#: Проект, из которого выделен шаблон. Любое упоминание этих имён в коде
#: шаблона — это хвост, который уехал вместе с нами.
SOURCE_PROJECT_TOKENS = (
    "proxyhub",
    "ProxyHub",
    "parquet",
    "dnsbl",
    "socks5",
    "ranker",
    "maxmind",
    "GeoLite",
    "torch",
    "scraper",
)

#: Имена агентов из исходного проекта. Попали в шаблон однажды — в списке
#: кандидатов normalize-legacy, — и были единственным хвостом, который
#: выглядел безобидно.
SOURCE_AGENT_NAMES = (
    "Gordon",
    "ZCode",
    "Space Bunny",
    "AutoClaw",
    "helios",
    "claude-opus",
)


def _coord_files() -> list[Path]:
    return [
        ROOT / ".agent-protocol.json",
        ROOT / ".agent-handoff.json",
        ROOT / "docs" / "agent-status.json",
    ]


def _template_code() -> list[Path]:
    """Код шаблона: скрипты, которые копируются в чужой репозиторий.

    docs/ и README сюда НЕ входят: они читаются человеком, который уже знает
    свой проект, и упоминание примера там полезно. Проверяется то, что
    уезжает в чужой репозиторий и начинает там исполняться.
    """
    return [
        ROOT / "scripts" / "agent_handoff.py",
        ROOT / "scripts" / "agent_dashboard.py",
        ROOT / "scripts" / "dev.py",
    ]


# ── код не знает про чужой проект ─────────────────────────────────────────

def test_no_source_project_tokens_in_shipped_code():
    """Код шаблона исполняется в чужом репозитории.

    Упоминание исходного проекта в комментарии не всегда безобидно: читатель
    ищет, почему здесь такое имя, и не находит ответа, потому что ответа
    больше нет. Допускается только перечисление самих токенов в этом тесте.
    """
    offenders = []
    for path in _template_code():
        text = path.read_text(encoding="utf-8-sig")
        for token in SOURCE_PROJECT_TOKENS:
            if token.lower() in text.lower():
                offenders.append(f"{path.name}: {token}")
    assert offenders == [], f"в коде шаблона остались хвосты исходного проекта: {offenders}"


def test_no_source_agent_names_in_shipped_code():
    """Имена агентов — самая частая и самая невинная на вид утечка.

    Список вида ("Alice", "Bob") в normalize-legacy выглядит как «просто
    пример», но агент, читающий код, считает его правилом и дописывает туда
    своё имя. Дальше список становится местным мусором и перестаёт работать.
    """
    offenders = []
    for path in _template_code():
        text = path.read_text(encoding="utf-8-sig")
        for name in SOURCE_AGENT_NAMES:
            if name in text:
                offenders.append(f"{path.name}: {name}")
    assert offenders == [], f"имена агентов исходного проекта попали в код: {offenders}"


def test_author_markers_are_not_hardcoded():
    """normalize-legacy обязан брать имена из протокола, а не из константы."""
    source = (ROOT / "scripts" / "agent_handoff.py").read_text(encoding="utf-8-sig")

    assert "def author_markers(" in source, "нет функции author_markers"
    # Список-константа с именами — это ровно то, что делает шаблон проектным.
    assert not re.search(r"for candidate in \(", source), (
        "имена кандидатов заданы литералом в коде; они должны приходить из "
        "протокола (normalisation.author_markers) или из самого журнала"
    )


def test_author_markers_fall_back_to_journal_authors(fake_repo):
    """Без маркеров в протоколе восстановление идёт по именам из журнала.

    Именно это делает шаблон рабочим сразу после копирования: новый проект
    ничего не настраивал, но первая же реальная запись задаёт словарь имён.

    Восстановление ищет маркер вида "by <имя>" В НАЗВАНИИ задачи, поэтому у
    второй записи маркер есть, а у первой — нет: первая задаёт словарь, вторая
    его проверяет.
    """
    _, handoff_file, _ = fake_repo
    data = read(handoff_file)
    data["task_log"] = [
        {"task": "первая задача", "author": "Nina",
         "status": "DONE", "description": "d", "changed_files": ["a.py"],
         "verification": "pytest", "result": "1 passed"},
        {"task": "вторая задача by Nina", "status": "DONE", "description": "d",
         "changed_files": ["a.py"], "verification": "pytest", "result": "1 passed"},
    ]
    handoff_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    assert handoff.author_markers(data) == ["Nina"], (
        "имена авторов из этого же журнала обязаны быть словарём восстановления"
    )

    run("normalize-legacy")
    restored = read(handoff_file)["task_log"][1]
    assert restored["author"] == "Nina", (
        "автор не восстановлен из уже известных имён журнала"
    )


def test_explicit_author_markers_win_over_journal(fake_repo):
    """Явное задание проекта приоритетнее: журнал мог накопить мусор."""
    protocol, handoff_file, _ = fake_repo
    data = json.loads(protocol.read_text(encoding="utf-8-sig"))
    data["normalisation"] = {"author_markers": ["Auditor"]}
    protocol.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    journal = read(handoff_file)
    journal["task_log"] = [{"task": "ревью by Someone", "author": "Someone"}]
    handoff_file.write_text(json.dumps(journal, ensure_ascii=False), encoding="utf-8")

    assert handoff.author_markers(journal) == ["Auditor"]


# ── координационные файлы чисты ───────────────────────────────────────────

@pristine_only
def test_coordination_files_have_no_mojibake():
    """Проверяется тем же набором маркеров, что и в продовом validate."""
    pairs = ("Р°", "Рµ", "СЂ", "С‚", "РЅ", "Сѓ", "РІ")
    for path in _coord_files():
        text = path.read_bytes().decode("utf-8-sig")
        hits = sum(text.count(p) for p in pairs)
        assert hits == 0, f"{path.name}: {hits} маркеров mojibake"


@pristine_only
def test_coordination_files_are_valid_json():
    for path in _coord_files():
        json.loads(path.read_bytes().decode("utf-8-sig"))  # падает с ValueError


@pristine_only
def test_protocol_carries_no_source_project_zones():
    """Зоны шаблона описывают сам шаблон, а не проект-источник."""
    protocol = json.loads(
        (ROOT / ".agent-protocol.json").read_text(encoding="utf-8-sig")
    )
    zones = json.dumps(protocol["zones"], ensure_ascii=False).lower()

    for token in ("src/analyzer", "src/ml", "src/scraper", "data/raw",
                  "proxyhub", "models/"):
        assert token not in zones, (
            f"в зонах остался путь исходного проекта: {token}"
        )


@pristine_only
def test_protocol_marks_project_specific_sections_for_filling():
    """Проектные разделы обязаны кричать «я пустой».

    Молчаливо пустой `contracts_easy_to_break` читается как «контрактов нет».
    Агент идёт ломать то, что нельзя, и узнаёт об этом из теста.
    """
    protocol = json.loads(
        (ROOT / ".agent-protocol.json").read_text(encoding="utf-8-sig")
    )

    contracts = protocol["contracts_easy_to_break"]
    assert any(k.startswith("_") for k in contracts), (
        "contracts_easy_to_break должен содержать служебный ключ-заглушку, "
        "иначе пустой раздел выглядит как «контрактов нет»"
    )
    assert any("FILL" in str(v) for v in contracts.values()), (
        "в contracts_easy_to_break нет явного указания, что его надо заполнить"
    )

    boundaries = protocol["boundaries"]
    assert any(k.startswith("_") for k in boundaries)
    assert "temp_trap" in boundaries, (
        "temp_trap — единственная ловушка, о которой шаблон обязан "
        "предупредить сам: проверка gettempdir() стоит копейки, а провал "
        "стоит мусорных каталогов в репозитории"
    )


@pristine_only
def test_placeholder_zones_warn_on_purpose():
    """Единственное предупреждение чистого шаблона — намеренное.

    `validate` ругается на ключи зон, начинающиеся с подчёркивания, и в
    свежем шаблоне такие ключи есть: `_comment`, `_how_to_adapt`,
    `_example`. Это и есть сигнал «заполни проектные части».

    Проверка зафиксирована, чтобы этот сигнал не вычистили как «лишний
    шум»: без него шаблон уезжает в проект с протоколом, который молча
    выглядит заполненным, и агент считает зоны настоящими.
    """
    import io
    from contextlib import redirect_stderr, redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer), redirect_stderr(buffer):
        code = run("validate")
    out = buffer.getvalue()

    assert code == 0, "служебные ключи зон — предупреждение, а не блокировка"
    assert "служебные подразделы в zones" in out, (
        "шаблон перестал подсказывать, что протокол нужно заполнить"
    )
    for key in ("_comment", "_how_to_adapt", "_example"):
        assert key in out, (
            f"служебный ключ {key} пропал из протокола: либо его удалили "
            "забыв переписать инструкцию, либо он потерялся при переносе"
        )


@pristine_only
def test_protocol_zones_reference_only_existing_paths():
    """Каждый путь зоны обязан существовать.

    Несуществующий путь даёт WARN на каждом запуске validate. Двадцать
    таких предупреждений превращают в фон, и агент перестаёт читать
    вывод целиком — включая настоящие блокирующие проблемы.
    """
    protocol = json.loads(
        (ROOT / ".agent-protocol.json").read_text(encoding="utf-8-sig")
    )
    missing = []
    for zone, patterns in protocol["zones"].items():
        if zone.startswith("_") or not isinstance(patterns, list):
            continue
        for pattern in patterns:
            if "*" in pattern or pattern.startswith("future"):
                continue
            if not (ROOT / pattern).exists():
                missing.append(f"{zone}: {pattern}")

    assert missing == [], f"зоны ссылаются на несуществующие пути: {missing}"


def test_reserved_zone_is_empty_and_that_is_valid(fake_repo):
    """Пустой reserved — корректное состояние, а не недозаполненность.

    Огороженная зона нужна там, где ошибка ломает систему ВНЕ репозитория.
    В свежем шаблоне такой территории нет, и выдумывать её вредно: лишний
    запрет блокирует агентов без причины.

    Проверка на изолированной фикстуре, а не на файлах шаблона: тест,
    создающий настоящий claim ради проверки, оставляет его в журнале и делает
    следующий же прогон health-check красным по собственной вине.
    """
    protocol, _, _ = fake_repo
    data = json.loads(protocol.read_text(encoding="utf-8-sig"))
    data["zones"]["reserved"] = []
    protocol.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    assert json.loads(protocol.read_text(encoding="utf-8-sig"))["zones"]["reserved"] == []
    assert run("claim", "alice", "anything", "--files", "src/app.py") == 0


@pristine_only
def test_mechanism_sections_are_not_empty():
    """Ядро шаблона не должно выглядеть опциональным.

    Если кто-то «уберёт на время» principles или zones_policy, шаблон
    останется рабочим и станет причиной тех самых дедлоков, ради устранения
    которых он и выделен.
    """
    protocol = json.loads(
        (ROOT / ".agent-protocol.json").read_text(encoding="utf-8-sig")
    )
    for section in ("principles", "zones_policy", "claims", "conflict_resolution",
                    "done_means", "priorities", "coordination_files", "workflow"):
        assert protocol.get(section), f"раздел {section} пуст"
    assert len(protocol["principles"]) >= 4
    assert protocol["claims"]["stale_after_minutes"] > 0
    assert protocol["claims"]["heartbeat_required_after_minutes"] < \
        protocol["claims"]["stale_after_minutes"], (
        "порог heartbeat обязан быть меньше порога протухания, иначе "
        "протухание наступит раньше, чем агент обязан был обновиться"
    )
    assert protocol["claims"]["max_agents"] >= 1


@pristine_only
def test_workflow_commands_use_python_not_a_project_venv():
    """Команды протокола должны работать и без venv проекта.

    Требование «обязательно .venv» было верно для проекта с тяжёлым стеком и
    стало блокировкой здесь: шаблон ставят в репозиторий, где venv может и
    не быть, и агент застревал на «инструмент не найден».

    Проверяются ИМЕНО команды (workflow, verification, claims), а не весь
    протокол: `.venv/` в boundaries.never_commit обязан остаться — виртуальное
    окружение действительно не коммитится.
    """
    protocol = json.loads(
        (ROOT / ".agent-protocol.json").read_text(encoding="utf-8-sig")
    )
    sections = ("workflow", "verification", "claims", "coordination_files",
                "priorities", "dashboard")
    assert ".venv\\Scripts\\python.exe" not in json.dumps(sections, ensure_ascii=False), (
        "протокол требует .venv проекта в командах — в чужом репозитории "
        "такого пути может не существовать"
    )
    # Значения собираются из разобранного JSON, а не пересериализуются
    # обратно: повторный json.dumps удвоил бы обратные слэши и проверка искала
    # бы несуществующий текст.
    def _strings(node) -> list[str]:
        if isinstance(node, str):
            return [node]
        if isinstance(node, dict):
            return [s for v in node.values() for s in _strings(v)]
        if isinstance(node, list):
            return [s for v in node for s in _strings(v)]
        return []

    commands = [s for name in sections for s in _strings(protocol[name])]
    assert not any(".venv\\Scripts\\python.exe" in c for c in commands), (
        "протокол требует .venv проекта в командах — в чужом репозитории "
        "такого пути может не существовать"
    )
    assert any("python scripts\\agent_handoff.py" in c for c in commands), (
        "в протоколе нет ни одной команды через интерпретатор — агент не "
        "поймёт, как запускать инструмент в репозитории без venv"
    )
    assert "never_commit" in protocol["boundaries"]
    assert any(".venv" in item for item in protocol["boundaries"]["never_commit"])


@pristine_only
def test_handoff_starts_empty():
    """Чистый шаблон не должен тащить историю чужого проекта.

    task_log на 134 записи и 79 подсказок — это не наследие, а шум: агент
    читает их как контекст и тратит внимание на то, что к его репозиторию
    отношения не имеет.
    """
    handoff = json.loads(
        (ROOT / ".agent-handoff.json").read_text(encoding="utf-8-sig")
    )
    assert handoff["task_log"] == [], "в шаблоне осталась чужая история задач"
    assert handoff["hints"] == [], "в шаблоне остались чужие подсказки"
    assert handoff["team_protocol"]["active_claims"] == {}, (
        "в шаблоне остались чужие активные claims — новый агент увидит "
        "занятые файлы, которых нет"
    )


@pristine_only
def test_status_journal_starts_without_agents():
    """Журнал статуса не должен содержать чужих слотов.

    Слот с несуществующим агентом и текстом next_step дашборд показывает как
    «можно забрать», и агент читает чужую инструкцию как актуальную.
    """
    status = json.loads(
        (ROOT / "docs" / "agent-status.json").read_text(encoding="utf-8-sig")
    )
    assert status["agents"] == {}, "в журнале остались чужие агенты"
    assert status["history"] == [], "в журнале осталась чужая история"
    assert status["current_gate"] == {}, (
        "журнал не должен показывать чужие результаты гейтов как свои"
    )


@pristine_only
def test_status_journal_keeps_self_service_instructions():
    """Журнал без инструкции оказывается бесполезен ровно для тех, кто его
    не знает: новый агент не поймёт, что в него писать, и не напишет."""
    status = json.loads(
        (ROOT / "docs" / "agent-status.json").read_text(encoding="utf-8-sig")
    )
    assert status["self_service"]["your_section"].startswith("status-set agents.")
    assert "refused with exit code 4" in status["self_service"]["foreign_section"]


# ── шаблон проверяем сам себя ─────────────────────────────────────────────

@pristine_only
def test_template_passes_its_own_validate():
    """Самый прямой тест из возможных.

    Шаблон, который не проходит собственный validate, учит агента тому, что
    validate можно не запускать: первое же «зелёное» доверие к инструменту
    будет заработано обманом.
    """
    assert run("validate") == 0


@pristine_only
def test_template_has_no_blocking_problems_in_health_check():
    assert run("health-check") == 0


def test_template_passed_own_tests_are_the_ones_that_ship():
    """Тесты, которые не поедут в чужой репозиторий, бесполезны.

    Каждый coordination-тест здесь работает на изолированной фикстуре и не
    трогает реальные .agent-*: если шаблон проверяет сам себя и при этом
    пишет в собственный журнал, он ломает то, что сам же советует.
    """
    assert "fake_repo" in (ROOT / "tests" / "test_agent_handoff.py").read_text(
        encoding="utf-8-sig")
