"""Тесты дашборда агентов.

Дашборд — единственный НЕобязательный компонент шаблона: он требует fastapi и
uvicorn, которые не нужны для координации. Поэтому весь модуль пропускается
целиком, если fastapi не установлен.

Пропуск, а не падение — не формальность. В исходном проекте отсутствие
fastapi роняло СБОР тестов целиком, и «прогнал гейты» становилось невозможно
ровно в том окружении, где координация нужнее всего. Гейт, который краснеет
из-за отсутствия необязательного инструмента, приучает агента его
игнорировать, а потом игнорируются и настоящие падения.

Что здесь проверяется по существу:

* дашборс **read-only** — он никогда не пишет в coordination-файлы;
* в скрипте UI нет обращений к id, которых нет в разметке (регрессия: одно
  отсутствующее исключение гасилось catch'ом, и панель навсегда показывала
  «offline» вместо данных);
* health_score честно помечен как advisory и считается из backlog ПРОТОКОЛА,
  а не из ручного списка в журнале (расхождение двух источников давало
  «здоровый» дашборд при открытых P0);
* протухшие claims не считаются живыми и не съедают лимит агентов;
* отсутствующий файл событий и недоступные метрики не роняют ответ.
"""

import json
import re

import pytest

fastapi = pytest.importorskip("fastapi", reason="дашборд необязателен: pip install fastapi uvicorn")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from scripts.agent_dashboard import app  # noqa: E402


def test_dashboard_index_served():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "<!doctype html>" in response.text.lower()


def test_ui_script_only_touches_elements_that_exist():
    """Регрессия: load() обращалась к #priorities и #network, которых не было
    в разметке. Первое отсутствующее исключение гасилось catch'ом, панель
    навсегда оставалась пустой со значком 'offline'.

    Каждый id, который читает скрипт, обязан существовать в отдаваемой
    разметке, а помощник — срезать ведущий '#', иначе getElementById его не
    найдёт.
    """
    html = TestClient(app).get("/").text
    script = re.search(r"<script>([\s\S]*)</script>", html).group(1)

    markup_ids = set(re.findall(r'id="([^"]+)"', html))
    referenced = set(re.findall(r"el\('#([A-Za-z0-9_-]+)'\)", script))

    assert referenced, "expected the script to reference ids via el('#id')"
    assert not referenced - markup_ids, (
        "script references ids missing from markup: "
        f"{sorted(referenced - markup_ids)}"
    )

    assert "String(id).replace('#','')" in script
    assert "document.querySelector(" not in script


def test_dashboard_status_is_read_only():
    response = TestClient(app).get("/api/status")
    assert response.status_code == 200
    body = response.json()
    assert body["read_only"] is True
    assert "claims" in body
    assert "protocol_version" in body


def test_dashboard_tasks_are_bounded():
    """Без ограничения журнал на сотни записей утянул бы весь ответ."""
    response = TestClient(app).get("/api/tasks?limit=2")
    assert response.status_code == 200
    assert len(response.json()) <= 2


def test_dashboard_missing_events_is_safe(tmp_path, monkeypatch):
    import scripts.agent_dashboard as dashboard

    monkeypatch.setattr(dashboard, "EVENTS", tmp_path / "missing.jsonl")
    assert TestClient(dashboard.app).get("/api/events").json() == []


def test_dashboard_metrics_has_graceful_contract():
    """Контракт метрик не зависит от psutil и платформы.

    psutil необязателен, а на части платформ нет сенсоров температуры. Ответ
    обязан содержать ключи всегда, иначе UI получит TypeError на
    `m.memory.percent` и покажет «offline» на исправной машине.
    """
    response = TestClient(app).get("/api/metrics")
    assert response.status_code == 200
    data = response.json()
    assert {"cpu_percent", "memory", "disk", "network", "platform", "python"} <= data.keys()


# ── сводка: health_score и источники приоритетов ──────────────────────────


def test_health_score_is_labelled_advisory():
    """Это число придумано дашбордом: у него нет калибровки и базы сравнения.

    Показывать его можно только рядом с исходными счётчиками, по которым его
    пересчитывают руками. Без этой пометки агент читает 40/100 как «у команды
    проблемы», хотя это может быть 0 зелёных гейтов из 3.
    """
    body = TestClient(app).get("/api/summary").json()
    assert body["health_score_is_advisory"] is True
    for field in ("open_p0", "open_p1", "active_claims", "stale_claims"):
        assert field in body, f"исходный счётчик {field} должен быть виден рядом с оценкой"


def test_priorities_come_from_the_protocol_backlog(tmp_path, monkeypatch):
    """Backlog в протоколе — канон; список priorities в журнале заполняют руками.

    Раньше сводка читала журнал. Два источника P0 расходятся всегда (кто-то
    добавил в backlog и не продублировал в журнал), и расхождение выглядело
    как «здоровый» дашборд при открытом P0.
    """
    import scripts.agent_dashboard as dashboard

    protocol = tmp_path / ".agent-protocol.json"
    handoff_file = tmp_path / ".agent-handoff.json"
    status = tmp_path / "docs" / "agent-status.json"
    status.parent.mkdir(parents=True, exist_ok=True)

    protocol.write_text(json.dumps({
        "protocol_version": "2.0",
        "claims": {"max_agents": 5},
        "backlog": [
            {"id": "P0-001", "priority": "P0", "status": "open"},
            {"id": "P0-002", "priority": "P0", "status": "done"},
            {"id": "P1-001", "priority": "P1", "status": "open"},
        ],
    }), encoding="utf-8")
    handoff_file.write_text(json.dumps({"team_protocol": {"active_claims": {}},
                                        "task_log": []}), encoding="utf-8")
    # Журнал утверждает обратное: ни одного P0.
    status.write_text(json.dumps({"priorities": {"P0": [], "P1": []},
                                  "agents": {}, "current_gate": {}}), encoding="utf-8")

    monkeypatch.setattr(dashboard, "PROTOCOL", protocol)
    monkeypatch.setattr(dashboard, "HANDOFF", handoff_file)
    monkeypatch.setattr(dashboard, "STATUS", status)

    body = dashboard._summary()
    assert body["open_p0"] == 1, "открытый P0 из протокола потерян"
    assert body["open_p1"] == 1, "закрытая P0 не должна считаться открытой"


def test_stale_claims_are_not_counted_as_active(tmp_path, monkeypatch):
    """Протухший claim занимает слот агента только в интерфейсе.

    Он же объясняет, почему агент не может начать: лимит max_agents исчерпан
    «призраками». Считать надо живые — тем же критерием, что и CLI.
    """
    import datetime as _dt

    import scripts.agent_dashboard as dashboard

    protocol = tmp_path / ".agent-protocol.json"
    handoff_file = tmp_path / ".agent-handoff.json"
    status = tmp_path / "docs" / "agent-status.json"
    status.parent.mkdir(parents=True, exist_ok=True)

    old = (_dt.datetime.now() - _dt.timedelta(hours=5)).strftime("%Y-%m-%d %H:%M")
    fresh = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    protocol.write_text(json.dumps({
        "protocol_version": "2.0",
        "claims": {"max_agents": 5, "stale_after_minutes": 90},
        "backlog": [],
    }), encoding="utf-8")
    handoff_file.write_text(json.dumps({
        "team_protocol": {"active_claims": {
            "ghost": {"agent": "ghost", "task": "умер", "files": ["a.py"],
                      "started_at": old, "heartbeat_at": old, "status": "IN_PROGRESS"},
            "alive": {"agent": "alive", "task": "работает", "files": ["b.py"],
                      "started_at": fresh, "heartbeat_at": fresh, "status": "IN_PROGRESS"},
        }},
        "task_log": [],
    }), encoding="utf-8")
    status.write_text(json.dumps({"priorities": {}, "agents": {}, "current_gate": {}}),
                      encoding="utf-8")

    monkeypatch.setattr(dashboard, "PROTOCOL", protocol)
    monkeypatch.setattr(dashboard, "HANDOFF", handoff_file)
    monkeypatch.setattr(dashboard, "STATUS", status)

    body = dashboard._summary()
    assert body["active_claims"] == 1, "протухший claim посчитан живым"
    assert body["stale_claims"] == 1
    assert body["max_agents"] == 5


def test_dashboard_never_writes_coordination_files(tmp_path, monkeypatch):
    """Главный контракт дашборда, проверяемый на файловой системе.

    Read-only заявлен в докстрингге и в протоколе. Объявление ничего не
    стоит: значение имеет проверка, которая сравнивает содержимое файлов до и
    после полного обхода всех эндпоинтов. Именно такой дашборд однажды мог
    стать вторым писателем журнала — и вернуть все замороженные правила
    single-writer.
    """
    import scripts.agent_dashboard as dashboard

    protocol = tmp_path / ".agent-protocol.json"
    handoff_file = tmp_path / ".agent-handoff.json"
    status = tmp_path / "docs" / "agent-status.json"
    status.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "protocol_version": "2.0",
        "claims": {"max_agents": 5},
        "zones": {"coordination": ["docs/agent-status.json"]},
        "backlog": [],
    }
    protocol.write_text(json.dumps(payload), encoding="utf-8")
    handoff_file.write_text(json.dumps({"team_protocol": {"active_claims": {}},
                                        "task_log": [], "hints": []}), encoding="utf-8")
    status.write_text(json.dumps({"priorities": {}, "agents": {}, "current_gate": {}}),
                      encoding="utf-8")

    monkeypatch.setattr(dashboard, "PROTOCOL", protocol)
    monkeypatch.setattr(dashboard, "HANDOFF", handoff_file)
    monkeypatch.setattr(dashboard, "STATUS", status)
    monkeypatch.setattr(dashboard, "EVENTS", tmp_path / "none.jsonl")

    before = {p: p.read_bytes() for p in (protocol, handoff_file, status)}
    client = TestClient(app)
    for url in ("/", "/api/status", "/api/summary", "/api/metrics",
                "/api/tasks?limit=10", "/api/events"):
        assert client.get(url).status_code == 200, url
    after = {p: p.read_bytes() for p in (protocol, handoff_file, status)}

    assert before == after, "дашборд изменил coordination-файл при чтении"
