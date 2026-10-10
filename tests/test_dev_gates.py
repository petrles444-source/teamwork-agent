"""Тесты агрегированной команды `dev.py gates`.

Проверяется не набор гейтов, а поведение агрегатора — и поведение здесь
главное. Два контракта, ради которых команда и написана:

1. Прогон НЕ останавливается на первом падении. Если бы останавливался,
   первый красный гейт оставил бы все последующие неизвестными, и
   «прогнал все гейты» означало бы «посмотрел первое красное и ушёл».
   Это ровно тот отказ, который команда чинит.

2. Пропущенный гейт (нет входных данных) — это НЕ успех. Он обязан быть
   виден отдельно от зелёного, иначе «прогнал гейты» снова станет
   неотличимо от «прогнал половину гейтов».

Гейты подменяются заглушками, поэтому тесты быстрые и не зависят ни от
данных, ни от сети.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dev  # noqa: E402


def make_gate(name: str, code: int = 0, requires: str = "") -> dev.Gate:
    """Заглушка гейта. Код возврата берётся из последнего аргумента."""
    gate = dev.Gate(name, ["fake", name], note=f"заглушка {name}", requires=requires)
    gate.exit_code = code
    return gate


@pytest.fixture
def restore_gates():
    """Подменяет источник списка гейтов, а не сам список.

    Список гейтов в шаблоне — КОНФИГУРАЦИЯ (scripts/gates.json), а не
    константа в коде: он меняется чаще, чем раннер. Тесты подменяют
    load_gates(), поэтому правка tests/ проверяет и новый путь конфигурации
    тоже — как только появится gates.json, тот же набор тестов пойдёт по нему.
    """
    original = dev.load_gates
    yield
    dev.load_gates = original


def _run(monkeypatch, gates, expect_skip: set[str] | None = None,
         outputs: dict[str, str] | None = None,
         codes: list | None = None) -> tuple[int, str, list[str]]:
    """Подменяет запуск подпрограмм; возвращает (код, вывод, команды).

    `expect_skip` — имена гейтов, чьи входные данные считаются отсутствующими.
    `outputs` — вывод, который «процесс» напечатает, по имени гейта.
    `codes` — если задан, коды возврата выдаются по порядку вызовов
    одного гейта: нужно, чтобы первый прогон был красным, а повтор зелёным.
    """
    executed: list[str] = []
    by_token = {g.args[-1]: g.exit_code for g in gates}
    missing = expect_skip or set()
    texts = outputs or {}
    seen: dict[str, int] = {}

    def fake_run(args, **kw):
        executed.append(" ".join(args))
        token = args[-1]
        if codes is not None:
            seen[token] = seen.get(token, -1) + 1
            code = codes[min(seen[token], len(codes) - 1)]
        else:
            code = by_token.get(token, 0)
        return type("P", (), {"returncode": code,
                              "stdout": texts.get(token, ""),
                              "stderr": ""})()

    monkeypatch.setattr(dev, "run", fake_run)
    monkeypatch.setattr(dev.Gate, "missing_input", lambda self: self.name in missing)
    monkeypatch.setattr(dev, "load_gates", lambda: list(gates))
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = dev.gates()
    return code, buffer.getvalue(), executed


# ── контракт 1: не останавливаться на первом падении ─────────────────

def test_runs_every_gate_even_after_a_failure(monkeypatch, restore_gates):
    gates = [make_gate("first"), make_gate("broken", 1),
             make_gate("third"), make_gate("fourth")]
    code, out, executed = _run(monkeypatch, gates)

    # Считаем РАЗНЫЕ гейты, а не число запусков: красный гейт
    # перезапускается один раз (P1-007), и второй его запуск - это тот же
    # гейт, а не пятый гейт. Проверять точное число процессов здесь
    # значило бы прибить гвоздями реализацию повтора.
    ran = {cmd.rsplit(" ", 1)[-1] for cmd in executed}
    assert len(ran) == 4, f"прогнаны только {ran}"
    for expected in ("first", "broken", "third", "fourth"):
        assert expected in ran, f"{expected} не прогнан"
    assert code == 1, "красный обязательный гейт должен давать код 1"


# ── контракт 3: красный перезапускается, чтобы отличить мерцание ────

def test_failing_gate_is_rerun_exactly_once(monkeypatch, restore_gates):
    """Причина контракта: файл правит другой агент прямо во время
    прогона, и гейт краснеет на ровном месте."""
    gates = [make_gate("stable", 0), make_gate("red", 1)]
    code, out, executed = _run(monkeypatch, gates)

    retries = [c for c in executed if c.endswith("red")]
    assert len(retries) == 2, (
        f"красный гейт должен быть запущен дважды, а не {len(retries)} раз"
    )
    assert "повтор" in out.lower(), "повтор не объявлен в выводе"


def test_gate_that_is_red_then_green_is_reported_as_flaky(monkeypatch, restore_gates):
    """Главный случай P1-007: агент, который правит файл, не должен
    получать «у тебя красный гейт»."""
    gates = [make_gate("touched", 0)]
    code, out, _ = _run(monkeypatch, gates, codes=[1, 0])

    assert "МЕРЦ" in out, "мерцание не отмечено в таблице итога"
    assert "мерцаний: 1" in out, "мерцание не посчитано в сводке"
    assert code == 0, (
        "повтор зелёный - код должен быть 0, иначе гейт продолжает "
        "кричать «красный» на пустом месте и его перестают читать"
    )


def test_gate_that_is_red_twice_stays_red(monkeypatch, restore_gates):
    """Обратная сторона: повтор не имеет права отмазывать настоящую
    регрессию."""
    gates = [make_gate("broken", 1)]
    code, out, _ = _run(monkeypatch, gates, codes=[1, 1])

    assert code == 1, "устойчивое падение обязано блокировать"
    assert "FAIL" in out
    assert "мерцаний: 0" in out


def test_flaky_gate_still_produces_a_loud_warning(monkeypatch, restore_gates,
                                                 capsys):
    """Мерцание не должно выглядеть как «красных нет»."""
    gates = [make_gate("touched", 0)]
    code, _out, _ = _run(monkeypatch, gates, codes=[1, 0])

    err = capsys.readouterr().err
    assert "МЕРЦАНИЕ" in err, (
        "мерцание не выведено в stderr - агент прочитает только «красных нет»"
    )
    assert "регрессия" in err, (
        "нет напоминания, что постоянное падение - это уже регрессия"
    )


def test_failing_test_names_are_named(monkeypatch, restore_gates, capsys):
    """Имя теста сразу указывает файл, а файл - чей это claim."""
    pytest_output = (
        "1 failed, 500 passed\n"
        "FAILED tests/test_api.py::test_state_is_ready - AssertionError: ...\n"
    )
    gates = [make_gate("pytest", 1)]
    code, out, _ = _run(monkeypatch, gates, codes=[1, 1],
                        outputs={"pytest": pytest_output})

    err = capsys.readouterr().err
    assert "tests/test_api.py::test_state_is_ready" in err, (
        f"имя упавшего теста не показано:\n{err}"
    )


def test_failing_names_parser_ignores_noise():
    """Парсер не должен выдумывать тесты из любых строк вывода."""
    text = ("progress 50%\n"
            "FAILED tests/test_x.py::test_y - AssertionError\n"
            "ERROR tests/test_z.py::test_w\n"
            "FAILED tests/test_x.py::test_y - AssertionError: другой текст\n"
            "просто строка с FAILED посреди\n")
    names = dev._failing_names(text)
    assert names == ["tests/test_x.py::test_y", "tests/test_z.py::test_w"]


def test_failure_reported_even_when_it_is_not_first(monkeypatch, restore_gates):
    """Падение в конце не должно теряться за зелёными в начале."""
    gates = [make_gate("a"), make_gate("b"), make_gate("c"), make_gate("last", 1)]
    code, out, _ = _run(monkeypatch, gates)
    assert code == 1
    assert "last" in out


def test_summary_marks_each_gate(monkeypatch, restore_gates):
    gates = [make_gate("alpha"), make_gate("beta", 1)]
    code, out, _ = _run(monkeypatch, gates)
    assert "alpha" in out and "beta" in out
    assert "OK" in out and "FAIL" in out


def test_all_green_returns_zero(monkeypatch, restore_gates):
    code, out, _ = _run(monkeypatch, [make_gate("a"), make_gate("b"), make_gate("c")])
    assert code == 0
    assert "красных гейтов нет" in out


# ── контракт 2: пропуск — это не успех ───────────────────────────────

def test_missing_input_gate_is_skipped_not_run(monkeypatch, restore_gates):
    gates = [make_gate("needs_data", requires="data/raw/*.parquet"),
             make_gate("always")]
    code, out, executed = _run(monkeypatch, gates, expect_skip={"needs_data"})

    assert not any("needs_data" in cmd for cmd in executed), (
        "гейт без входных данных всё равно запустился"
    )
    assert "SKIP" in out
    assert code == 0, "отсутствие данных — не регрессия, код должен остаться 0"


def test_skip_is_visible_and_labelled_as_not_success(monkeypatch, restore_gates):
    """Пропуск обязан быть виден и не должен читаться как успех."""
    gates = [make_gate("needs_data", requires="data/raw/*.parquet")]
    code, out, _ = _run(monkeypatch, gates, expect_skip={"needs_data"})

    assert "needs_data" in out
    assert "пропущено: 1" in out, "пропуск не посчитан в сводке"
    # Строка про пропуск уходит в stderr — проверяем через тот же поток,
    # который тест перенаправляет, плюс сам факт формулировки в коде.
    assert "SKIP" in out
    assert code == 0


def test_all_skipped_does_not_claim_a_clean_bill_of_health(monkeypatch, restore_gates):
    """Ноль зелёных, ноль красных, всё пропущено — итог не должен читаться
    как «всё хорошо». Кейс поймала проба на живой таблице гейтов, когда
    в коде стояло безусловное «красных гейтов нет»."""
    gates = [make_gate("needs_a", requires="x"), make_gate("needs_b", requires="y")]
    code, out, _ = _run(monkeypatch, gates, expect_skip={"needs_a", "needs_b"})

    assert code == 0, "пропуск не блокирует сдачу"
    assert "зелёных: 0" in out
    assert "НЕПОЛНАЯ" in out, (
        "ни одна проверка не выполнилась, а итог выглядит как чистый лист"
    )


def test_one_skipped_among_green_is_labelled_incomplete(monkeypatch, restore_gates):
    gates = [make_gate("ok_one"), make_gate("needs", requires="x"),
             make_gate("ok_two")]
    code, out, _ = _run(monkeypatch, gates, expect_skip={"needs"})
    assert code == 0
    assert "зелёных: 2" in out
    assert "НЕПОЛНАЯ" in out, "частичный прогон должен называться неполным"


def test_skip_counts_towards_summary_totals(monkeypatch, restore_gates):
    gates = [make_gate("ok_one"), make_gate("skipped", requires="x"),
             make_gate("ok_two"), make_gate("red", 1)]
    code, out, _ = _run(monkeypatch, gates, expect_skip={"skipped"})
    assert "зелёных: 2" in out
    assert "красных: 1" in out
    assert "пропущено: 1" in out
    assert code == 1


def test_missing_input_helper_detects_absent_data(monkeypatch):
    """Сам помощник: матчится ли glob по реальному корню репозитория."""
    present = dev.Gate("x", ["a"], requires="pyproject.toml")
    absent = dev.Gate("y", ["a"], requires="data/нет_такой_папки/*.parquet")
    assert present.missing_input() is False
    assert absent.missing_input() is True


def test_gate_without_requires_never_skips():
    gate = dev.Gate("no_input", ["a"])
    assert gate.missing_input() is False


# ── настоящая таблица гейтов ─────────────────────────────────────────

def test_default_gate_list_is_sane():
    """Проверка встроенного минимального набора, а не заглушек.

    Минимум выбран так, чтобы работать в ЛЮБОМ Python-репозитории: pytest
    есть везде, coordination проверяет целостность самого шаблона. Предметные
    гейты (e2e, реальные данные, производительность) здесь отсутствуют
    намеренно — они принадлежат проекту, и шаблон, который на них рассчитывает,
    в чужом репозитории краснеет навсегда.
    """
    names = [g.name for g in dev.DEFAULT_GATES]
    assert "pytest" in names, "обязательный гейт тестов отсутствует"
    assert "coordination" in names, (
        "гейт целостности координации отсутствует — именно он ловит битый "
        "JSON, в который агент пишет каждый день"
    )
    assert len(names) == len(set(names)), "имена гейтов повторяются"
    for gate in dev.DEFAULT_GATES:
        assert gate.note, f"гейт {gate.name} без пояснения, что он защищает"
        assert gate.args, f"гейт {gate.name} без команды"


def test_default_gates_do_not_assume_a_project_layout():
    """Ни один встроенный гейт не должен ссылаться на путь проекта.

    Путь в args, которого нет в чужом репозитории, даёт красный гейт в первый
    же прогон — и агент, не разобравшись, начинает отключать гейты целиком,
    вместе с работающими.
    """
    for gate in dev.DEFAULT_GATES:
        for arg in gate.args:
            if "/" in arg and not arg.startswith("-"):
                assert (ROOT / arg).exists(), (
                    f"гейт {gate.name} ссылается на несуществующий путь {arg}"
                )


def test_no_gate_is_marked_optional():
    """У гейта нет статуса «необязательный».

    Необязательный гейт, который по умолчанию не блокирует, через месяц
    перестают читать — и он снова становится тем тихим инструментом,
    которым был. Единственная честная форма «необязательности» — SKIP по
    отсутствию входных данных, и она видна в сводке.
    """
    assert not hasattr(dev.Gate("x", ["a"]), "required"), (
        "вернулось необязательное деление гейтов"
    )


def test_gates_takes_no_fast_flag():
    """`--fast` удалён: замер опроверг его основание.

    Флаг пропускал почти ничего, назывался «быстрым», и агент, его
    запустивший, был бы уверен, что прогнал все гейты. Единственный
    действительно долгий гейт — pytest, и его пропускать нельзя.
    """
    import inspect
    assert "fast" not in inspect.signature(dev.gates).parameters


# ── список гейтов как конфигурация ────────────────────────────────────


def test_missing_tool_is_distinguished_from_missing_data(monkeypatch):
    """Отсутствие линтера и отсутствие данных — РАЗНЫЕ вещи.

    Первое чинится `pip install`, второе — созданием данных. Если оба
    показывать как SKIP, агент привыкает читать «проверка не выполнена» как
    «проверка не нужна» — и начинает пропускать настоящие падения.

    Проверяется на doctor, потому что там это и показывается: сам гейт в
    обоих случаях честно падает, и подменять его поведение нельзя.
    """
    monkeypatch.setattr(dev, "_has", lambda module: module != "ruff")

    lint = next(g for g in dev.DEFAULT_GATES if g.name == "lint")
    pytest_gate = next(g for g in dev.DEFAULT_GATES if g.name == "pytest")

    assert lint.needs_missing_tool() is True, "отсутствие ruff не замечено"
    assert pytest_gate.needs_missing_tool() is False
    # Отсутствие инструмента НЕ должно маскироваться под SKIP по данным:
    # requires пуст, и missing_input обязан остаться ложным.
    assert lint.missing_input() is False


def test_gate_without_needs_never_reports_missing_tool():
    gate = dev.Gate("coordination", ["scripts/agent_handoff.py", "validate"])
    assert gate.needs_missing_tool() is False


def test_needs_survives_gates_json_round_trip(monkeypatch, tmp_path):
    """Ключ `needs` переживает разбор конфига — по той же причине, что и
    `requires`: потерянный признак превращает «нет инструмента» в SKIP."""
    config = tmp_path / "gates.json"
    config.write_text(json.dumps([
        {"name": "lint", "args": ["x"], "needs": "ruff"},
    ]), encoding="utf-8")
    monkeypatch.setattr(dev, "GATES_JSON", config)
    monkeypatch.setattr(dev, "_has", lambda module: False)

    assert dev.load_gates()[0].needs == "ruff"
    assert dev.load_gates()[0].needs_missing_tool() is True


def test_default_gates_have_a_reason_for_skipping():
    """Каждый гейт по умолчанию объявляет требуемый модуль.

    Без этого doctor не может отличить «инструмент не установлен» от «гейт
    настроен неправильно», и первое выглядит как второе.
    """
    for gate in dev.DEFAULT_GATES:
        if gate.name in ("pytest", "lint"):
            assert gate.needs, f"гейт {gate.name} не объявляет требуемый модуль"


def test_gates_json_overrides_the_defaults(monkeypatch, tmp_path):
    """Проект задаёт гейты файлом, а не правкой кода.

    Это позволяет агенту добавить гейт и сразу им проверить, не изменяя
    раннер, который в этот момент может править кто-то другой.
    """
    config = tmp_path / "gates.json"
    config.write_text(json.dumps([
        {"name": "my_check", "args": ["-u", "scripts/check.py"], "note": "мой"},
    ]), encoding="utf-8")
    monkeypatch.setattr(dev, "GATES_JSON", config)

    gates = dev.load_gates()
    assert [g.name for g in gates] == ["my_check"]
    assert gates[0].note == "мой"


def test_gates_json_requires_survives_round_trip(monkeypatch, tmp_path):
    """Ключ `requires` не должен теряться при разборе конфига.

    Потерянный `requires` — тихая поломка: гейт начнёт запускаться без данных
    и упадёт вместо SKIP, то есть отсутствие окружения станет выглядеть как
    регрессия кода.
    """
    config = tmp_path / "gates.json"
    config.write_text(json.dumps([
        {"name": "needs_data", "args": ["x"], "requires": "data/*.csv"},
    ]), encoding="utf-8")
    monkeypatch.setattr(dev, "GATES_JSON", config)

    assert dev.load_gates()[0].requires == "data/*.csv"


def test_broken_gates_json_does_not_crash(monkeypatch, tmp_path):
    """Битый конфиг не должен ронять проверку целиком.

    Поведение выбрано в пользу встроенного набора, а не исключения: агент
    чинит JSON и теряет при этом возможность прогнать то, что ещё работает.
    Ошибка печатается в stderr — молчаливый откат выглядел бы как «гейты
    всегда зелёные».
    """
    config = tmp_path / "gates.json"
    config.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(dev, "GATES_JSON", config)

    assert [g.name for g in dev.load_gates()] == [g.name for g in dev.DEFAULT_GATES]


def test_empty_gates_json_falls_back(monkeypatch, tmp_path):
    """Пустой список гейтов — это опечатка, а не «проверять нечего».

    Принять его молча значило бы превратить `gates` в команду, которая всегда
    зелёная: агент отчитался о проверке, которой не было.
    """
    config = tmp_path / "gates.json"
    config.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(dev, "GATES_JSON", config)

    assert [g.name for g in dev.load_gates()] == [g.name for g in dev.DEFAULT_GATES]


def test_example_gates_file_is_valid_and_documented():
    """Файл-пример обязан разбираться тем же кодом, что и настоящий.

    Пример, который нельзя загрузить, хуже отсутствующего: агент скопирует его,
    сразу получит исключение и потратит время на выяснение, что не так.
    """
    example = ROOT / "scripts" / "gates.example.json"
    assert example.exists(), "нет файла-примера для scripts/gates.json"

    raw = json.loads(example.read_text(encoding="utf-8-sig"))
    for item in raw:
        assert {"name", "args", "note"} <= item.keys(), (
            f"в примере гейт {item.get('name')} описан неполно"
        )

    # Собственный парсер принимает этот файл без исключения.
    gates = [dev.Gate(g["name"], list(g["args"]), g["note"], g.get("requires", ""))
             for g in raw]
    assert gates


def test_gates_list_flag_does_not_run_anything(monkeypatch, capsys):
    """`gates --list` показывает состав и ничего не запускает.

    Агент читает список гейтов перед правкой — и не должен при этом
    запускать pytest на двадцать минут.
    """
    import dev as dev_module

    called: list = []
    monkeypatch.setattr(dev_module, "run", lambda *a, **k: called.append(a))
    monkeypatch.setattr(sys, "argv", ["dev.py", "gates", "--list"])

    assert dev_module.main() == 0
    assert called == [], "чтение списка не должно запускать подпроцессы"

    out = capsys.readouterr().out
    for gate in dev_module.load_gates():
        assert gate.name in out


def test_gates_without_list_flag_runs(monkeypatch):
    """Обратная сторона: без флага гейты действительно запускаются.

    Без этой проверки тест выше прошёл бы и при сломанном разборе аргументов,
    при котором `--list` вообще не известен и молча превращается в
    `gates` с лишним позиционным аргументом.
    """
    import dev as dev_module

    called: list = []
    monkeypatch.setattr(dev_module, "run",
                        lambda *a, **k: (called.append(a), _fake_result())[1])
    monkeypatch.setattr(dev_module.Gate, "missing_input", lambda self: False)
    monkeypatch.setattr(dev_module, "load_gates",
                        lambda: [dev_module.Gate("one", ["x"], note="n")])
    monkeypatch.setattr(sys, "argv", ["dev.py", "gates"])

    assert dev_module.main() == 0
    assert called, "без --list гейты обязаны запускаться"


def _fake_result():
    """Объект с формой CompletedProcess для подменённого run()."""
    import types

    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


# --- ruff ----------------------------------------------------------------


def test_ruff_uses_extend_exclude_not_exclude():
    """extend-exclude, а не exclude: exclude ЗАМЕНЯЕТ список и открывает .venv.

    Именно так первая версия подняла число нарушений с единиц до десятков
    тысяч — ruff начал проверять пакеты внутри .venv. Проверка ловит именно
    эту ошибку, а не любое слово 'exclude' в конфиге.
    """
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8-sig"))
    ruff = data.get("tool", {}).get("ruff", {})

    assert "exclude" not in ruff, "exclude переопределяет список по умолчанию"
    excluded = ruff.get("extend-exclude", [])
    for directory in (".scratch", ".zwork"):
        assert directory in excluded, f"{directory} не исключён — счётчик будет шумным"


def test_ruff_is_not_a_mandatory_dependency():
    """Ruff — гейт, а не условие запуска координации.

    Если сделать его обязательной зависимостью, `pip install` шаблона в
    репозиторий без сети перестанет работать — и откатится вся координация,
    ради которой шаблон ставится.
    """
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8-sig"))
    assert data["project"]["dependencies"] == [], (
        "ядро шаблона обязано работать на стандартной библиотеке; необязательные "
        "инструменты живут в optional-dependencies"
    )
    dev_extra = data["project"]["optional-dependencies"]["dev"]
    assert any(item.startswith("ruff") for item in dev_extra)


# ── гейт ссылается на несуществующий скрипт ────────────────────────────────


def test_missing_script_is_found():
    """Гейт, чей скрипт не написан, обязан называться ДО прогона.

    Проверено на чистой установке: пример scripts/gates.example.json
    перенесённый дословно даёт гейты, ссылающиеся на скрипты, которых в
    новом проекте нет. doctor показывал их как исправные, и человек узнавал
    о проблеме только из красного прогона — после того, как потратил время.
    """
    gate = dev.Gate("e2e", ["-u", "scripts/не_существует.py"], note="n")
    assert gate.missing_scripts() == ["scripts/не_существует.py"]
    assert gate.needs_missing_script() is True


def test_existing_script_is_not_reported_missing():
    gate = dev.Gate("coordination",
                    ["scripts/agent_handoff.py", "validate"], note="n")
    assert gate.missing_scripts() == []
    assert gate.needs_missing_script() is False


def test_missing_input_gate_is_not_reported_as_missing_script():
    """requires и «нет скрипта» — разные вещи.

    Гейт с шаблоном входных данных не имеет скриптов вообще, и его
    отсутствие входных данных не должно выглядеть как сломанный гейт.
    """
    gate = dev.Gate("needs_data", ["-m", "pytest", "-q"],
                    requires="data/*.csv")
    assert gate.missing_scripts() == []


def test_flags_and_module_names_are_not_paths():
    """`-q` и `pytest` не являются файлами, которых может не быть."""
    gate = dev.Gate("t", ["-m", "pytest", "-q", "--strict-markers"], note="n")
    assert gate.missing_scripts() == []


def test_doctor_names_the_gates_that_cannot_pass(monkeypatch, capsys):
    """doctor обязан перечислить гейты, которые не пройдут, и сказать почём.

    Красный гейт, который нельзя починить, приучает не читать остальные:
    агент привыкает, что `gates` всегда красный, и перестаёт смотреть.
    Поэтому причина обязана называться ДО прогона.
    """
    monkeypatch.setattr(dev, "load_gates", lambda: [
        dev.Gate("ok_one", ["-m", "pytest", "-q"], note="n"),
        dev.Gate("ghost", ["-u", "scripts/нет_такого.py"], note="n"),
    ])
    monkeypatch.setattr(sys, "argv", ["dev.py", "doctor"])

    dev.doctor()
    out = capsys.readouterr().out

    assert "НЕТ СКРИПТА" in out, "гейт с отсутствующим скриптом не помечен"
    assert "scripts/нет_такого.py" in out, "не назван конкретный отсутствующий файл"
    assert "НЕ ПРОЙДУТ" in out, "не сказано, что эти гейты красные"


def test_doctor_lists_missing_tool_by_name(monkeypatch, capsys):
    monkeypatch.setattr(dev, "_has", lambda m: m != "ruff")
    monkeypatch.setattr(dev, "load_gates", lambda: list(dev.DEFAULT_GATES))
    monkeypatch.setattr(sys, "argv", ["dev.py", "doctor"])

    dev.doctor()
    out = capsys.readouterr().out

    assert "НЕТ ИНСТРУМЕНТА" in out
    assert "не установлен ruff" in out
