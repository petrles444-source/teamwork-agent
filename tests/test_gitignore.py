"""Регрессия на .gitignore: правила проверяются реальным git, а не на глаз.

Причина, по которой файл вообще проверяется: неправильное правило молча
прячет исходники. Реальные случаи из проекта-источника шаблона:

* исключение 'data/' целиком прятало бы любой исходник или markdown рядом с
  артефактами прогона — такие файлы исчезли бы из репозитория без единого
  сообщения;
* исключение 'models/*_20????????_??????.pt' исключало бы ТЕКУЩую модель, и
  установленный проект остался бы без неё.

Общее правило, которое стоит унести в любой проект: исключаются ДАННЫЕ, а не
каталог целиком. Исключение каталога уносит с собой всё, что в него положили
позже, — включая исходники, добавленные через полгода.

Git используется в отдельной временной копии: создавать .git в рабочем
репозитории нельзя, это изменение, которого никто не просил.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Пути шаблона и его артефактов, которые обязаны игнорироваться.
MUST_BE_IGNORED = [
    "build/lib/scripts/agent_handoff.py",
    "teamwork_agent.egg-info/SOURCES.txt",
    ".venv/Scripts/python.exe",
    ".scratch/agent/task/x.py",
    ".zwork/artifacts/abc/agent_handoff.py",
    ".pytest_cache/CACHEDIR.TAG",
    ".ruff_cache/CACHEDIR.TAG",
    "tmpq1xy12/scratch.txt",
    "run_e2e_check/x.parquet",
    "run_harness_1/x.py",
    "app.db",
    "cache.sqlite",
    "coverage.xml",
    "debug.log",
    ".agent-events.jsonl",
    ".agent-handoff.backups/handoff-1757000000.json",
    "tests/__pycache__/test_dev_gates.cpython-312.pyc",
]

#: То, что обязано попадать в коммит. Список НЕ выдуман: каждый путь здесь
#: закрывает реальный класс потери — либо шаблон перестаёт работать, либо
#:: наблюдаемость команды исчезает.
MUST_BE_TRACKED = [
    ".agent-protocol.json",
    ".agent-handoff.json",
    "docs/agent-status.json",
    "scripts/agent_handoff.py",
    "scripts/agent_dashboard.py",
    "scripts/agent_dashboard.html",
    "scripts/dev.py",
    "scripts/gates.example.json",
    "tests/test_agent_handoff.py",
    "tests/test_dev_gates.py",
    "docs/AGENT_PROMPT.md",
    "docs/TEAM_PROTOCOL.md",
    "docs/PROJECT_STATUS.md",
    "docs/rfc/0000-template.md",
    "README.md",
    "pyproject.toml",
    "Makefile",
    ".github/workflows/gates.yml",
]


def _checked(tmp_path: Path, path: str) -> bool:
    """True, если git считает путь проигнорированным."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", "--", path],
        cwd=tmp_path, capture_output=True,
    )
    return result.returncode == 0


@pytest.fixture(scope="module")
def git_copy(tmp_path_factory):
    """Временная копия .gitignore, инициализированная как репозиторий."""
    if shutil.which("git") is None:
        pytest.skip("git недоступен")

    work = tmp_path_factory.mktemp("gitignore")
    shutil.copy(ROOT / ".gitignore", work / ".gitignore")
    subprocess.run(["git", "init", "-q"], cwd=work, capture_output=True, check=True)
    return work


def test_generated_artifacts_are_ignored(git_copy):
    wrongly_tracked = [p for p in MUST_BE_IGNORED if not _checked(git_copy, p)]

    assert wrongly_tracked == [], (
        f"эти пути должны игнорироваться, но не игнорируются: {wrongly_tracked}"
    )


def test_coordination_and_sources_stay_tracked(git_copy):
    """Самое опасное правило, которое можно написать в .gitignore.

    Исключение `.agent-*` выглядит безобидно («это же временное состояние»).
    На деле оно означает, что координация команды живёт в чужой ветке и в
    чужом клоне: новый агент не видит claims и начинает дублировать работу,
    а это ровно та поломка, ради устранения которой шаблон и выделен.
    """
    wrongly_ignored = [p for p in MUST_BE_TRACKED if _checked(git_copy, p)]

    assert wrongly_ignored == [], (
        f"правило прячет то, что коммитить нужно: {wrongly_ignored}"
    )


def test_coordination_files_are_actually_present(git_copy):
    """Мало исключить — надо проверить, что файлы вообще существуют.

    Тест на список строк проходит и при удалённом скрипте: правило работает,
    проверять нечего. Этот тест ломается вслед за файлом.
    """
    missing = [p for p in MUST_BE_TRACKED if not (ROOT / p).exists()]

    assert missing == [], (
        f"в репозитории нет файлов, которые .gitignore обязан пропускать: {missing}"
    )



# --- .gitattributes ----------------------------------------------------------


def test_line_endings_are_normalised():
    """Разнобой переводов строк делает `git diff` нечитаемым.

    Координационные файлы переписываются агентами на разных системах. Если
    checkout на Windows переписывает их в CRLF, а wsl-агент пишет обратно в
    LF, diff показывает изменение каждой строки файла, которого никто не
    менял. Через неделю такой шум приучает агента не открывать diff — а
    именно проверка чужих правок перед записью нужна для координации.

    Проверяется правилом из файла, а не наличием ключевых слов: неверная
    директива (`* text` без `eol`, или `* -text`) выглядит так же, а ведёт
    себя обратно.
    """
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

    assert "* text=auto eol=lf" in attributes, (
        "общего правила нормализации нет: агенты на разных системах будут "
        "портить diff друг друга"
    )

    # Обязательный LF для файлов, которые читаются побайтово и сравниваются.
    for pattern in ("*.py", "*.json", "*.md"):
        assert f"{pattern} text eol=lf" in attributes, (
            f"{pattern} без принудительного LF"
        )

    # Обратный случай: нормализовать то, что обязано остаться двоичным.
    for pattern in ("*.png", "*.pdf", "*.whl"):
        assert f"{pattern} binary" in attributes, (
            f"{pattern} объявлен бинарным неверно — «нормализация» ломает файл"
        )


def test_batch_files_keep_crlf():
    """.bat и .ps1 обязаны остаться CRLF: cmd.exe и PowerShell читают их так.

    Приведённые к LF, они работают у большинства, но ломаются на
    конструкциях с многострочными блоками — и это выглядит как «скрипт
    сломался», а не как «неправильный перевод строки».
    """
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

    for pattern in ("*.bat", "*.cmd", "*.ps1"):
        assert f"{pattern} text eol=crlf" in attributes, (
            f"{pattern} должен оставаться CRLF"
        )


def test_eol_directives_survive_git(git_copy):
    """Правила из .gitattributes git действительно понимает.

    Проверка на файле прошла бы и при опечатке в директиве: `git
    check-attr` отвечает по фактическим правилам, а не по тому, что файл
    выглядит правильно.
    """
    import shutil as _shutil
    import subprocess as _subprocess

    if _shutil.which("git") is None:
        pytest.skip("git недоступен")

    _shutil.copy(ROOT / ".gitattributes", git_copy / ".gitattributes")

    def attr(path: str, name: str) -> str:
        result = _subprocess.run(["git", "check-attr", name, "--", path],
                                 cwd=git_copy, capture_output=True, text=True)
        return result.stdout.split(":")[-1].strip()

    assert attr("scripts/agent_handoff.py", "eol") == "lf"
    assert attr(".agent-protocol.json", "eol") == "lf"
    assert attr("start.bat", "eol") == "crlf"
    assert attr("logo.png", "text") in ("unset", "false")

def test_rules_match_the_protocols_never_commit_list():
    """Список из .agent-protocol.json -> покрыт ли .gitignore.

    Сравнение нормализует хвосты: протокол пишет '.venv/', а git-правило
    '.venv/' — это одно и то же, и требовать буквального совпадения строк
    было бы проверкой ради проверки. Проверяется именно ПРИСУТСТВИЕ правила:
    расхождение здесь означает, что агент коммитит то, что протокол запрещает.
    """
    protocol = json.loads(
        (ROOT / ".agent-protocol.json").read_text(encoding="utf-8-sig")
    )
    rules = (ROOT / ".gitignore").read_text(encoding="utf-8")

    def normalise(item: str) -> str:
        return item.strip().rstrip("/*").replace("\\", "/")

    rule_paths = {normalise(line) for line in rules.splitlines()}
    missing = [
        item for item in protocol["boundaries"]["never_commit"]
        if normalise(item) not in rule_paths
    ]

    assert missing == [], f"правила протокола не отражены в .gitignore: {missing}"


def test_scratch_prefixes_agree_between_protocol_and_gitignore():
    """Ловушка temp_trap описана в двух местах, и они обязаны совпадать.

    Протокол объясняет агенту, какие каталоги считать мусором; .gitignore
    прячет их. Расхождение даёт один из двух неприятных исходов: либо
    health-check ругается на каталог, который git считает нормальным, либо
    агент создаёт мусор, который никто не замечает, потому что git его прячет.
    """
    protocol = json.loads(
        (ROOT / ".agent-protocol.json").read_text(encoding="utf-8-sig")
    )
    rules = (ROOT / ".gitignore").read_text(encoding="utf-8")

    for prefix in protocol["boundaries"]["scratch_prefixes"]:
        head = prefix.strip(".").split("/")[0]
        assert head in rules, (
            f"префикс {prefix} описан в протоколе, но не спрячен в .gitignore"
        )
