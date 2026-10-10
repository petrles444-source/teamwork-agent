"""Лицензия должна оставаться машинно-читаемой.

Почему это проверяется, а не просто «положим текст и забудем». GitHub
определяет лицензию репозитория ПО ТОЧНОМУ СОВПАДЕНИЮ с эталонным
текстом. Любая приписка - пояснение, благодарность, абзац на русском -
превращает MIT в "Other": страница репозитория показывает
"Other", фильтр по лицензии перестаёт работать, и человек, выбирающий
себе лицензию, видит не то.

Это не выдуманный страх: так и случилось. Файл начинался с канонического
текста и продолжался абзацем "что эта лицензия разрешает". Для автора это
выглядело как забота о читателе, для GitHub - как другой продукт.

Поэтому: LICENSE содержит ТОЛЬКО лицензию. Всё остальное - в README,
где оно и место для пояснений.

Текст эталонной MIT взят из официального detect_license, а не написан
заново: пересказ лицензии юридически ничтожен, даже если слова те же.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LICENSE = ROOT / "LICENSE"

REQUIRED_MARKERS = (
    "MIT License",
    "Permission is hereby granted, free of charge",
    "The above copyright notice and this permission notice shall be included",
    'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND',
)


def test_license_file_exists():
    assert LICENSE.exists(), (
        "нет файла LICENSE. Он объявлен в pyproject.toml, но объявление без "
        "файла ничего не передаёт: лицензия должна быть передана, а не "
        "упомянута"
    )


@pytest.mark.parametrize("marker", REQUIRED_MARKERS)
def test_license_contains_canonical_mit_text(marker):
    text = LICENSE.read_text(encoding="utf-8-sig")
    assert marker in text, f"в LICENSE нет обязательной части MIT: {marker!r}"


def test_license_has_no_text_beyond_mit():
    """ГЛАВНАЯ проверка: после текста лицензии не должно быть ничего.

    detect_license на GitHub ищет известные тексты и отказывается, если
    файл длиннее и содержит посторонний текст. Поэтому проверяется не
    "есть ли приписка", а "заканчивается ли файл ровно на тексте лицензии".

    Пробелы нормализуются ДО поиска: канонический текст MIT перенесён
    строками по 80 символов, и фраза в файле разорвана переводом строки.
    Поиск по сырому тексту искал бы «IN THE SOFTWARE.» и не нашёл его
    именно потому, что лицензия написана правильно.
    """
    raw = LICENSE.read_text(encoding="utf-8-sig").strip()
    flat = " ".join(raw.split())

    marker = "IN THE SOFTWARE."
    assert marker in flat, "не найден конец канонического текста MIT"

    tail = flat[flat.index(marker) + len(marker):].strip()
    assert tail == "", (
        "после текста MIT добавлено:\n"
        f"{tail[:200]}\n"
        "GitHub определит лицензию как Other, а не MIT. Перенесите пояснение "
        "в README - там для него место."
    )


def test_license_declared_in_pyproject_matches():
    """Объявление в pyproject.toml должно совпадать с файлом.

    Расхождение возможно и незаметно: pyproject продолжает говорить MIT,
    файл молча меняет вид, и человек ставит библиотеку, не понимая под
    какими условиями.
    """
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8-sig"))
    declared = data.get("project", {}).get("license", {})

    assert isinstance(declared, dict), "ожидался table-формат license"
    spdx = declared.get("text", "")
    assert spdx.strip().upper().startswith("MIT"), f"в pyproject: {spdx!r}"
    assert LICENSE.read_text(encoding="utf-8-sig").startswith("MIT License"), (
        "файл LICENSE начинается не с MIT, а pyproject объявляет MIT"
    )


def test_readme_carries_the_license_guidance():
    """Пояснение, вынесенное из LICENSE, обязано куда-то переехать.

    Иначе получится ровно то, чего мы избегали: файл стал каноническим,
    а человек, читавший прежнюю версию, потерял полезный текст молча.
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8-sig")
    assert "MIT" in readme, "README не упоминает лицензию"
    assert "LICENSE" in readme, "README не ссылается на файл лицензии"
