"""Тесты координационного инструмента (протокол v2.0).

Контракт, который здесь закрепляется:

* владение определяется ЖИВЫМ claim'ом, а не именем агента в протоколе;
* зона — подсказка, она никогда не блокирует;
* устаревший claim (без heartbeat) перестаёт держать файлы, иначе исчезнувший
  агент оставляет их занятыми навсегда;
* журнал статуса self-service: каждый пишет свой раздел, блокировки «один
  писатель» нет — именно она замораживала журнал при отключении владельца;
* зарезервированная зона требует явного --force.
"""

import json

import pytest

# Фикстура fake_repo и хелперы run/read живут в tests/conftest.py: ими
# пользуются и проверки координации, и проверки универсальности шаблона.
# Дублировать их нельзя — две копии разъедутся, и тесты начнут проверять
# разное. Импорт после scripts.agent_handoff — из-за sys.path, который
# настраивает conftest.
from conftest import read, run  # noqa: E402

import scripts.agent_handoff as handoff

# --- перезапись чужого слота статуса ------------------------------------------


def _set_slot(status_file, name, **fields):
    data = read(status_file)
    data.setdefault("agents", {})[name] = fields
    status_file.write_text(json.dumps(data), encoding="utf-8")


def _claim_on(handoff_file, agent, minutes_ago=1):
    import datetime as _dt
    stamp = (_dt.datetime.now() - _dt.timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%d %H:%M")
    data = read(handoff_file)
    data["team_protocol"]["active_claims"][agent] = {
        "agent": agent, "task": "что-то", "files": ["a.py"],
        "started_at": stamp, "heartbeat_at": stamp, "status": "WIP",
    }
    handoff_file.write_text(json.dumps(data), encoding="utf-8")


def test_foreign_slot_without_force_is_refused(fake_repo):
    _, _, status = fake_repo
    _set_slot(status, "other", status="DONE")
    code = run("status-set", "agents.other.status", '"DONE"', "--agent", "me")
    assert code == 4
    assert read(status)["agents"]["other"]["status"] == "DONE"


def test_force_cannot_silently_clobber_a_live_agent(fake_repo):
    """ГЛАВНЫЙ контракт. --force раньше снимал проверку владельца целиком,
    без сверки, и позволял молча затереть слот агента, который прямо
    сейчас работает. Теперь у живого слота нужен отдельный --override-live."""
    _, handoff_file, status = fake_repo
    _set_slot(status, "busy", status="WIP", task="в работе")
    _claim_on(handoff_file, "busy")

    code = run("status-set", "agents.busy.status", '"DONE"',
               "--agent", "me", "--force")
    assert code == 4, "живой слот перезаписан обычным --force"
    assert read(status)["agents"]["busy"]["status"] == "WIP"

    # Явное переигнорирование разрешает - но это осознанное действие.
    code = run("status-set", "agents.busy.status", '"DONE"',
               "--agent", "me", "--force", "--override-live")
    assert code == 0
    assert read(status)["agents"]["busy"]["status"] == "DONE"


def test_force_can_reclaim_a_dead_slot(fake_repo):
    """Ровно тот случай, ради которого выход существует: сессия завершена,
    слот остался с инструкцией, которую все читают как текущую."""
    _, _, status = fake_repo
    _set_slot(status, "gone", status="DONE",
                       next_step="задание, которого никто не закрыл")

    code = run("status-set", "agents.gone.next_step", '"неактуально"',
               "--agent", "me", "--force")
    assert code == 0, "слот завершившегося агента нельзя исправить"
    assert read(status)["agents"]["gone"]["next_step"] == "неактуально"


def test_fresh_heartbeat_alone_also_marks_a_slot_live(fake_repo):
    """Claim могут забыть при нормальном ходе работы, поэтому второе
    свидетельство жизни - свежий heartbeat в самом слоте."""
    import datetime as _dt
    _, _, status = fake_repo
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    _set_slot(status, "quiet", status="WIP", heartbeat_at=stamp)

    code = run("status-set", "agents.quiet.status", '"DONE"',
               "--agent", "me", "--force")
    assert code == 4, "слот со свежим heartbeat перезаписан"


def test_stale_heartbeat_slot_is_reclaimable(fake_repo):
    import datetime as _dt
    _, _, status = fake_repo
    old = (_dt.datetime.now() - _dt.timedelta(hours=5)).strftime("%Y-%m-%d %H:%M")
    _set_slot(status, "old", status="DONE", heartbeat_at=old)

    code = run("status-set", "agents.old.next_step", '"устарело"',
               "--agent", "me", "--force")
    assert code == 0


def test_own_slot_never_needs_force(fake_repo):
    _, _, status = fake_repo
    _set_slot(status, "me", status="WIP")
    assert run("status-set", "agents.me.status", '"DONE"', "--agent", "me") == 0
    assert read(status)["agents"]["me"]["status"] == "DONE"


def test_override_live_alone_does_nothing(fake_repo):
    """Флаг не должен работать как самостоятельный обход: без --force он
    бессмысленен, иначе следующий человек подумает, что достаточно его."""
    _, _, status = fake_repo
    _set_slot(status, "other", status="DONE")
    code = run("status-set", "agents.other.status", '"X"',
               "--agent", "me", "--override-live")
    assert code == 4
    assert read(status)["agents"]["other"]["status"] == "DONE"


def test_slot_liveness_reasons_are_explicit(fake_repo):
    """Помощник обязан объяснять, ПОЧЕМУ слот признан живым или мёртвым:
    решение о чужой записи должно быть проверяемо, а не на веру."""
    _, handoff_file, status = fake_repo
    _set_slot(status, "quiet", status="WIP")
    _claim_on(handoff_file, "quiet")
    live, why = handoff.status_slot_is_live(
        "quiet", read(status), read(handoff_file))
    assert live and "claim" in why

    dead, why_dead = handoff.status_slot_is_live(
        "nobody", read(status), read(handoff_file))
    assert not dead and why_dead


def test_absent_timestamp_does_not_protect_a_slot(fake_repo):
    """Сознательное отличие от is_stale_claim: там отсутствие метки времени
    означает «свежий», потому что речь о ПОТЕРЕ ФАЙЛОВ и лучше не отнять
    чужую работу. Здесь речь о записи в чужую заметку, и слот без claim и
    без heartbeat не доказывает, что кто-то работает - именно такие слоты
    и остаются после завершения сессии."""
    _, _, status = fake_repo
    _set_slot(status, "ancient", status="DONE")  # без heartbeat_at
    code = run("status-set", "agents.ancient.next_step", '"правка"',
               "--agent", "me", "--force")
    assert code == 0


# --- одно имя — две сессии ----------------------------------------------------


def test_duplicate_name_is_refused_while_the_first_claim_is_live(fake_repo):
    """ГЛАВНЫЙ КОНТРАКТ.

    active_claims — словарь по имени агента. Перезапущенная сессия,
    взявшая прежнее имя, МОЛЧА затирала живой claim первой сессии: её
    задача исчезала, её файлы переставали быть занятыми, и никто этого
    не видел — просто набор занятых файлов становился короче. Хуже
    всего: последующий release снимал уже новый claim, а первой сессии
    оставалось думать, что файлы её.
    """
    _, handoff_file, _ = fake_repo
    _claim_on(handoff_file, "bunny", minutes_ago=2)

    code = run("claim", "bunny", "новая задача", "--files", "src/a.py")
    assert code == 4, "живой claim с тем же именем перезаписан"

    claims = read(handoff_file)["team_protocol"]["active_claims"]
    assert claims["bunny"]["task"] == "что-то", "задача первой сессии заменена"


def test_refusal_explains_the_right_fix(fake_repo, capsys):
    """Отказ бесполезен, если не сказать, что делать. Правильный путь -
    взять имя вида '<имя>/2', а не забирать чужой claim."""
    _, handoff_file, _ = fake_repo
    _claim_on(handoff_file, "bunny", minutes_ago=2)
    run("claim", "bunny", "новая", "--files", "src/a.py")
    err = capsys.readouterr().err
    assert "/2" in err, "не подсказан способ взять отдельное имя"
    assert "take-over" in err


def test_refusal_names_the_previous_task_and_files(fake_repo, capsys):
    _, handoff_file, _ = fake_repo
    _claim_on(handoff_file, "bunny", minutes_ago=2)
    run("claim", "bunny", "новая", "--files", "src/a.py")
    err = capsys.readouterr().err
    assert "что-то" in err, "не назван прежний task"
    assert "a.py" in err, "не названы прежние файлы - а именно они терялись"


def test_distinct_name_is_the_way_two_sessions_coexist(fake_repo):
    """Два живых claim'а под разными именами - норма, а не конфликт."""
    _, handoff_file, _ = fake_repo
    _claim_on(handoff_file, "bunny", minutes_ago=2)
    assert run("claim", "bunny/2", "другая задача", "--files", "src/b.py") == 0

    claims = read(handoff_file)["team_protocol"]["active_claims"]
    assert set(claims) == {"bunny", "bunny/2"}
    assert claims["bunny"]["files"] == ["a.py"], "файлы первой сессии потеряны"
    assert claims["bunny/2"]["files"] == ["src/b.py"]


def test_take_over_requires_the_explicit_flag_and_is_loud(fake_repo, capsys):
    _, handoff_file, _ = fake_repo
    _claim_on(handoff_file, "bunny", minutes_ago=2)

    assert run("claim", "bunny", "забираю себе", "--files", "src/c.py",
               "--take-over") == 0
    out = capsys.readouterr().out
    assert "ЗАБРАН ЖИВОЙ claim" in out, "захват не объявлен вслух"
    assert "a.py" in out, "не сказано, какие файлы перестали быть защищены"

    claim = read(handoff_file)["team_protocol"]["active_claims"]["bunny"]
    assert claim.get("took_over_live_claim") is True, "захват не помечен в записи"


def test_take_over_never_loses_the_previous_files(fake_repo):
    """Даже при явном захвате прежние файлы обязаны сохраниться в записи:
    иначе конфликт-детектор перестанет их защищать, и это заметят не сразу."""
    _, handoff_file, _ = fake_repo
    _claim_on(handoff_file, "bunny", minutes_ago=2)
    run("claim", "bunny", "забираю", "--files", "src/c.py", "--take-over")

    claim = read(handoff_file)["team_protocol"]["active_claims"]["bunny"]
    assert claim.get("previous_files") == ["a.py"]
    assert claim.get("previous_task") == "что-то"
    assert claim.get("previous_started_at")


def test_claiming_over_a_stale_own_claim_needs_no_flag(fake_repo):
    """Зависший claim - это не живая сессия. Перехват должен быть свободным,
    иначе агент, вернувшийся через два часа, упрётся в собственное имя."""
    _, handoff_file, _ = fake_repo
    _claim_on(handoff_file, "bunny", minutes_ago=200)
    assert run("claim", "bunny", "продолжаю", "--files", "src/a.py") == 0


def test_first_ever_claim_is_unaffected(fake_repo):
    _, handoff_file, _ = fake_repo
    assert run("claim", "fresh", "первая задача", "--files", "src/a.py") == 0
    claim = read(handoff_file)["team_protocol"]["active_claims"]["fresh"]
    assert "took_over_live_claim" not in claim
    assert "previous_files" not in claim


# --- сверка журнала статуса с claim'ами --------------------------------------


def test_live_claim_with_a_done_slot_is_a_contradiction(fake_repo):
    """ГЛАВНЫЙ КОНТРАКТ.

    health check читала .agent-handoff.json и .agent-protocol.json, но
    docs/agent-status.json не открывала вообще. А именно по журналу
    статуса агент решает, кто работает и можно ли брать задачу.
    Слот 'DONE' при живом claim'е говорит новому агенту, что можно
    начинать с нуля, пока файлы всё ещё заняты.
    """
    _, handoff_file, status = fake_repo
    _claim_on(handoff_file, "bunny", minutes_ago=2)
    _set_slot(status, "bunny", status="DONE", task="якобы закончил")

    contradictions, _, _ = handoff.status_journal_consistency(
        read(handoff_file)["team_protocol"]["active_claims"])
    assert contradictions, "живой claim при слоте DONE не замечен"
    assert "bunny" in contradictions[0]


def test_live_claim_with_a_working_slot_is_fine(fake_repo):
    """Проверка обязана оставаться тихой, когда всё согласовано, иначе
    she'll превратится в шум, который перестанут читать."""
    _, handoff_file, status = fake_repo
    _claim_on(handoff_file, "bunny", minutes_ago=2)
    _set_slot(status, "bunny", status="IN_PROGRESS", task="в работе")

    contradictions, unreadable, _ = handoff.status_journal_consistency(
        read(handoff_file)["team_protocol"]["active_claims"])
    assert not contradictions
    assert not unreadable


def test_prose_where_a_state_belongs_is_flagged(fake_repo):
    """Свободный текст вместо состояния делает журнал нечитаемым машинно.

    Это не выдумка: проверка нашла ровно такой случай у двух живых
    агентов в момент её написания - в поле status лежал абзац.
    """
    _, handoff_file, status = fake_repo
    _set_slot(status, "chatty", status="Duplicate agent name no longer "
                                       "silently discards a live claim")

    _, unreadable, _ = handoff.status_journal_consistency(
        read(handoff_file)["team_protocol"]["active_claims"])
    assert unreadable, "абзац вместо состояния не замечен"
    assert "chatty" in unreadable[0]


def test_live_claim_with_no_slot_at_all_is_flagged(fake_repo):
    """Отсутствие слота хуже неверного: агент, читающий журнал, просто
    не найдёт этого человека и решит, что он не работает."""
    _, handoff_file, _ = fake_repo
    _claim_on(handoff_file, "ghost", minutes_ago=2)

    contradictions, _, _ = handoff.status_journal_consistency(
        read(handoff_file)["team_protocol"]["active_claims"])
    assert any("ghost" in c for c in contradictions)


def test_dead_slot_keeping_a_next_step_is_reported_as_reclaimable(fake_repo):
    """Не проблема, а приглашение: там может лежать инструкция, которую
    все продолжают читать как текущую."""
    _, _, status = fake_repo
    _set_slot(status, "gone", status="DONE",
              next_step="переразметь всё до любых заявлений")

    _, _, reclaimable = handoff.status_journal_consistency({})
    assert any("gone" in r for r in reclaimable)


def test_clean_slot_without_next_step_is_not_flagged(fake_repo):
    _, _, status = fake_repo
    _set_slot(status, "done_fully", status="DONE")
    contradictions, unreadable, reclaimable = handoff.status_journal_consistency({})
    assert not contradictions and not unreadable and not reclaimable


def test_stale_claim_does_not_count_as_live(fake_repo):
    _, handoff_file, status = fake_repo
    _claim_on(handoff_file, "sleepy", minutes_ago=300)
    _set_slot(status, "sleepy", status="IDLE")

    contradictions, _, _ = handoff.status_journal_consistency(
        read(handoff_file)["team_protocol"]["active_claims"])
    assert not contradictions, "зависший claim ошибочно принят за живой"


def test_health_check_exit_code_reflects_the_journal(fake_repo):
    """Проверка обязана менять код выхода, иначе её результат никто
    не увидит в автоматике."""
    _, handoff_file, status = fake_repo
    _claim_on(handoff_file, "bunny", minutes_ago=2)
    _set_slot(status, "bunny", status="DONE")

    import argparse
    code = handoff.cmd_health_check(argparse.Namespace())
    assert code == 1, "противоречие журнала не повлияло на код выхода"

    _set_slot(status, "bunny", status="IN_PROGRESS")
    assert handoff.cmd_health_check(argparse.Namespace()) == 0


def test_missing_status_file_does_not_crash(fake_repo, monkeypatch):
    """Отсутствующий журнал - это отсутствие данных, а не повод упасть."""
    from pathlib import Path
    monkeypatch.setattr(handoff, "STATUS", Path(str(fake_repo[2]) + ".absent"))
    assert handoff.status_journal_consistency({}) == ([], [], [])


# --- зоны вместо имён --------------------------------------------------------


def test_zones_are_not_agent_names(fake_repo):
    protocol, _, _ = fake_repo
    zones = set(read(protocol)["zones"])
    assert "some-agent" not in zones
    assert "coordination" in zones


def test_who_owns_reports_zone_as_free(fake_repo):
    """Зона не равна «занято»: агент видит подсказку и идёт работать."""
    _, _, _ = fake_repo
    assert run("who-owns", "scripts/agent_handoff.py") == 0


def test_who_owns_reports_claim_as_busy(fake_repo):
    _, hf, _ = fake_repo
    run("claim", "alice", "some task", "--files", "scripts/agent_handoff.py")
    assert run("who-owns", "scripts/agent_handoff.py") == 1


def test_stale_claim_does_not_block(fake_repo):
    """Ключевой антидедлок: исчезнувший агент не должен держать файлы вечно."""
    _, hf, _ = fake_repo
    run("claim", "alice", "long task", "--files", "src/analyzer/pipeline.py")
    data = read(hf)
    data["team_protocol"]["active_claims"]["alice"]["heartbeat_at"] = "2020-01-01 00:00"
    hf.write_text(json.dumps(data), encoding="utf-8")

    # claim проходит без --force, потому что старый holder молчит
    assert run("claim", "bob", "take over", "--files", "src/analyzer/pipeline.py") == 0


def test_live_claim_still_blocks(fake_repo):
    _, _, _ = fake_repo
    run("claim", "alice", "task", "--files", "src/analyzer/pipeline.py")
    assert run("claim", "bob", "other", "--files", "src/analyzer/pipeline.py") == 4


def test_reserved_zone_requires_force(fake_repo):
    """Единственная настоящая огороженная территория.

    Путь в тесте намеренно нейтральный: шаблон переносится в любой проект, и
    проверка не должна зависеть от того, существует ли в нём каталог с
    каким-то именем. Механика — «требуется явный --force» — от пути не зависит.
    """
    _, _, _ = fake_repo
    assert run("claim", "alice", "deploy", "--files", "deploy/prod.yaml") == 4
    assert run("claim", "alice", "deploy", "--files", "deploy/prod.yaml", "--force") == 0


def test_claim_in_unzoned_file_is_allowed(fake_repo):
    """Отсутствие зоны — не повод ждать."""
    _, _, _ = fake_repo
    assert run("claim", "alice", "new area", "--files", "brand_new_module.py") == 0


def test_max_agents_is_read_from_protocol(fake_repo):
    protocol, _, _ = fake_repo
    assert handoff.max_agents() == 5


# --- self-service журнал -----------------------------------------------------


def test_status_set_allows_any_agent_to_write_own_section(fake_repo):
    """Никакого single-writer: агент пишет свой раздел сам."""
    _, _, status = fake_repo
    assert run("status-set", "agents.bob.status", "IN_PROGRESS", "--agent", "bob") == 0
    assert read(status)["agents"]["bob"]["status"] == "IN_PROGRESS"


def test_status_set_rejects_writing_someone_elses_section(fake_repo):
    """Чужой раздел — единственное, что по-прежнему защищено."""
    _, _, _ = fake_repo
    assert run("status-set", "agents.alice.status", "DONE", "--agent", "bob") == 4
    assert run("status-set", "agents.alice.status", "DONE", "--agent", "bob", "--force") == 0


def test_status_set_append_is_idempotent(fake_repo):
    _, _, status = fake_repo
    run("status-set", "hints", "To all: something", "--agent", "bob")
    run("status-set", "hints", "To all: something", "--agent", "bob")
    assert read(status)["hints"] == ["To all: something"]


def test_status_set_replaces_scalar_key(fake_repo):
    _, _, status = fake_repo
    run("status-set", "current_gate", json.dumps({"pytest": "passed"}), "--agent", "bob")
    assert read(status)["current_gate"] == {"pytest": "passed"}


def test_status_set_nested_path_creates_missing_dict(fake_repo):
    _, _, status = fake_repo
    run("status-set", "agents.carol.status", "DONE", "--agent", "carol")
    assert read(status)["agents"]["carol"]["status"] == "DONE"


def test_status_set_remove_action(fake_repo):
    _, _, status = fake_repo
    run("status-set", "hints", "To all: drop me", "--agent", "bob")
    assert run("status-set", "hints", "To all: drop me", "--agent", "bob", "--action", "remove") == 0
    assert read(status)["hints"] == []


def test_status_set_stamps_author(fake_repo):
    _, _, status = fake_repo
    run("status-set", "agents.bob.status", "DONE", "--agent", "bob")
    assert read(status)["updated_by"] == "bob"


# --- validate ----------------------------------------------------------------


def test_validate_passes_on_healthy_repo(fake_repo):
    _, _, _ = fake_repo
    assert run("validate") == 0


def test_validate_reports_mojibake_as_blocking(fake_repo):
    _, hf, _ = fake_repo
    data = read(hf)
    data["hints"] = ["To all: Р°СЂС‚РµС‚Р°Рё"]
    hf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert run("validate") == 1


def test_validate_does_not_fail_on_legacy_entries(fake_repo):
    """Старая запись без полей — предупреждение, а не повод ронять чужой гейт.

    Помеченная `legacy_entry` запись неполна законно: она сделана до того,
    как схема появилась, и дописывать выдуманные поля — значит врать.
    """
    _, hf, _ = fake_repo
    data = read(hf)
    data["task_log"] = [{
        "date": "2026-01-01",
        "task": "old thing",
        "legacy_entry": True,
        "legacy_note": "Запись старше согласованной схемы task_log.",
    }]
    hf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert run("validate") == 0


def test_unmarked_incomplete_entry_still_blocks(fake_repo):
    """Обратная сторона: «это старая запись» — не оправдание для всех.

    Если бы `legacy_entry` выключал проверку для любой неполной записи,
    агент получил бы красный validate, пометил свою свежую запись как
    legacy и навсегда отключил себе проверку.
    """
    _, hf, _ = fake_repo
    data = read(hf)
    data["task_log"] = [{"date": "2026-01-01", "task": "incomplete, not legacy"}]
    hf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert run("validate") == 1


# --- P2-009: backlog set больше не молчит ------------------------------------


def test_backlog_set_writes_description(fake_repo):
    """Раньше --description принимался и молча игнорировался.

    Агент правил описание задачи, а команда попутно закрывала её, потому что
    --status по умолчанию равнялся done. Ловушку я заел дважды за одну сессию.
    """
    protocol, _, _ = fake_repo
    assert run("backlog", "add", "--priority", "P2", "--description", "исходная") == 0

    assert run("backlog", "set", "--id", "P2-001",
               "--description", "переписано") == 0

    item = next(b for b in read(protocol)["backlog"] if b["id"] == "P2-001")
    assert item["description"] == "переписано"


def test_backlog_set_does_not_close_a_task_by_default(fake_repo):
    """Ключевая часть ловушки: set без --status не должен закрывать задачу."""
    protocol, _, _ = fake_repo
    assert run("backlog", "add", "--priority", "P2", "--description", "исходная") == 0

    run("backlog", "set", "--id", "P2-001", "--description", "уточнено")

    item = next(b for b in read(protocol)["backlog"] if b["id"] == "P2-001")
    assert item["status"] == "open", "set без --status закрыл задачу"


def test_backlog_set_writes_priority_and_area(fake_repo):
    protocol, _, _ = fake_repo
    assert run("backlog", "add", "--priority", "P2", "--description", "исходная") == 0

    run("backlog", "set", "--id", "P2-001", "--priority", "P1", "--area", "scripts")

    item = next(b for b in read(protocol)["backlog"] if b["id"] == "P2-001")
    assert item["priority"] == "P1"
    assert item["area"] == "scripts"


def test_backlog_set_status_still_works(fake_repo):
    protocol, _, _ = fake_repo
    assert run("backlog", "add", "--priority", "P2", "--description", "исходная") == 0

    assert run("backlog", "set", "--id", "P2-001", "--status", "done") == 0

    item = next(b for b in read(protocol)["backlog"] if b["id"] == "P2-001")
    assert item["status"] == "done"
    assert item["description"] == "исходная", "явная смена статуса не должна трогать описание"


def test_backlog_set_unknown_id_is_an_error(fake_repo):
    assert run("backlog", "set", "--id", "P9-999", "--status", "done") == 1



# --- схема записей task_log: что validate считает обязательным ---------------

def _entry(**fields):
    """Запись task_log с полным набором полей; fields переопределяют."""
    base = {
        "date": "2026-01-01",
        "task": "задача",
        "author": "me",
        "status": "DONE",
        "description": "d",
        "changed_files": ["a.py"],
        "verification": "pytest",
        "result": "1 passed",
        "next_agent_hint": "подсказка",
        "coordination_note": "нет конфликтов",
    }
    base.update(fields)
    return base


def test_underscore_keys_are_not_required_fields(fake_repo):
    """Служебные ключи схемы не могут быть обязательными полями.

    Ключ `_comment` в `handoff_entry_schema` — пояснение к схеме, а не поле
    записи. Раньше он попадал в список обязательных, и validate объявлял
    БЛОКИРУЮЩИМ отсутствие поля, которого в записи быть не может: агент не
    мог сделать validate зелёным, не удалив пояснение из протокола.
    """
    protocol, handoff_file, _ = fake_repo
    data = json.loads(protocol.read_text(encoding="utf-8-sig"))
    data["handoff_entry_schema"] = {
        "task": "краткое имя",
        "result": "числа",
        "_comment": "служебное пояснение, не поле записи",
        "_how_to_add": "ещё одно пояснение",
    }
    protocol.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    handoff = read(handoff_file)
    handoff["task_log"] = [_entry()]
    handoff_file.write_text(json.dumps(handoff, ensure_ascii=False), encoding="utf-8")

    assert run("validate") == 0


def test_done_entry_does_not_need_next_agent_hint(fake_repo):
    """Подсказка следующему агенту обязательна только для НЕзавершённой работы.

    Иначе каждая завершённая задача без подсказки роняла validate, а таких
    задач большинство: работа закончена, следующему агенту передавать нечего.
    Инструкция в AGENT_PROMPT.md говорит «обязательно, если status != DONE» —
    код обязан следовать написанному, иначе документация врёт.
    """
    _, handoff_file, _ = fake_repo
    entry = _entry()
    entry.pop("next_agent_hint")

    handoff = read(handoff_file)
    handoff["task_log"] = [entry]
    handoff_file.write_text(json.dumps(handoff, ensure_ascii=False), encoding="utf-8")

    assert run("validate") == 0


def test_unfinished_entry_does_need_next_agent_hint(fake_repo):
    """Обратная сторона: незавершённая работа БЕЗ подсказки — блокирующая.

    Именно этот случай подсказку и оправдывает: работа брошена, и следующий
    агент обязан знать, с чего начать.
    """
    _, handoff_file, _ = fake_repo
    entry = _entry(status="IN_PROGRESS")
    entry.pop("next_agent_hint")

    handoff = read(handoff_file)
    handoff["task_log"] = [entry]
    handoff_file.write_text(json.dumps(handoff, ensure_ascii=False), encoding="utf-8")

    assert run("validate") == 1


def test_abandoned_entry_needs_next_agent_hint(fake_repo):
    """ABANDONED — работа тоже не доделана, подсказка обязательна.

    Группировка с IN_PROGRESS выбрана не по сходству слов, а по смыслу:
    в обоих случаях следующий агент подхватывает недоделанное.
    """
    _, handoff_file, _ = fake_repo
    entry = _entry(status="ABANDONED")
    entry.pop("next_agent_hint")

    handoff = read(handoff_file)
    handoff["task_log"] = [entry]
    handoff_file.write_text(json.dumps(handoff, ensure_ascii=False), encoding="utf-8")

    assert run("validate") == 1


def test_genuinely_missing_field_is_still_blocking(fake_repo):
    """Ослабление не должно стать лазейкой: result по-прежнему обязателен.

    Проверка нужна, потому что правка выше расширила список необязательных.
    Если расширить его ещё на `result`, validate станет зелёным на пустых
    записях — и потеряет смысл.
    """
    _, handoff_file, _ = fake_repo
    entry = _entry()
    entry.pop("result")

    handoff = read(handoff_file)
    handoff["task_log"] = [entry]
    handoff_file.write_text(json.dumps(handoff, ensure_ascii=False), encoding="utf-8")

    assert run("validate") == 1


def test_optional_metadata_does_not_block_when_missing(fake_repo):
    """date, author и coordination_note старые записи законно не имеют."""
    _, handoff_file, _ = fake_repo
    entry = _entry()
    for key in ("date", "author", "coordination_note"):
        entry.pop(key)

    handoff = read(handoff_file)
    handoff["task_log"] = [entry]
    handoff_file.write_text(json.dumps(handoff, ensure_ascii=False), encoding="utf-8")

    assert run("validate") == 0


# --- кодировка: цитата повреждённой строки против повреждения файла ---------

def _marker() -> str:
    """Настоящий маркер двойного кодирования, собранный из кодов.

    chr(), а не литерал: литерал означал бы, что тест содержит искомую
    последовательность и проверяет на себе сам.
    """
    return "".join(chr(c) for c in (0x420, 0xB0))


def test_corruption_inside_quotes_is_a_citation_not_damage():
    """ГЛАВНЫЙ контракт.

    Агент, который чинил испорченную строку, вправе ПРИВЕСТИ её в записи
    как пример. Раньше валидатор объявлял файл повреждённым из-за цитаты, а
    repair-encoding не мог её починить (строка закодирована дважды, и
    обратный проход даёт другой мусор). Гейт краснел у всех из-за чужой
    цитаты, и правильного инструмента не существовало: оставалось либо
    редактировать чужую запись, либо мириться с красным гейтом.
    """
    text = f"строка выглядела как «{_marker()}» - починил"
    real, quoted = handoff.count_mojibake(text)

    assert real == 0, "цитата объявлена повреждением файла"
    assert quoted > 0, "цитата не засчитана как цитата"


def test_corruption_outside_quotes_is_still_damage():
    """Обратная сторона: правило не должно превращать гейт в декорацию."""
    real, quoted = handoff.count_mojibake(f"здесь {_marker()} и это порча")
    assert real > 0 and quoted == 0


@pytest.mark.parametrize("open_c, close_c", [
    ('"', '"'),
    ("'", "'"),
    ("`", "`"),
    ("\u00ab", "\u00bb"),
    ("\u201c", "\u201d"),
    ("\u201e", "\u201c"),
])
def test_every_known_quote_pair_is_recognised(open_c, close_c):
    """Русские кавычки-ёлочки парные: открывающая и закрывающая — РАЗНЫЕ символы.

    Список символов вместо соответствия распознавал только английские
    кавычки, и исходная задача — не краснеть из-за чужой цитаты — решалась
    для меньшинства языков, в которых написан журнал.
    """
    text = f"было {open_c}{_marker()}{close_c} потом починил"
    real, quoted = handoff.count_mojibake(text)
    assert real == 0 and quoted > 0, f"пара {open_c!r}...{close_c!r} не распознана"


def test_apostrophes_in_english_words_do_not_forgive_damage():
    """Апостроф внутри слова — не кавычка.

    Это различие решает всё. Если считать апостроф кавычкой всегда, две
    английские сокращения в одной строке образуют «пару кавычек», и всё
    повреждение между ними объявляется цитатой и прощается. Описания задач
    в журнале пишутся по-английски, сокращения встречаются постоянно, и гейт
    молча зеленеет на испорченном файле — то есть перестаёт работать ровно
    тогда, когда он нужен.
    """
    real, quoted = handoff.count_mojibake(f"it's a {_marker()} thing's broken")
    assert real > 0, "порча между двумя сокращениями прощена как цитата"
    assert quoted == 0


def test_possessive_pair_does_not_forgive_damage():
    real, quoted = handoff.count_mojibake(f"agent's log had {_marker()} writer's note")
    assert real > 0 and quoted == 0


def test_real_quotes_apostrophes_still_work():
    """Проверка обратной стороны: апостроф-кавычка по-прежнему работает."""
    real, quoted = handoff.count_mojibake(f"it was '{_marker()}' indeed")
    assert real == 0 and quoted > 0


def test_unclosed_quote_does_not_swallow_later_damage():
    """Незакрытая кавычка цитатой не считается.

    Содержимое попадает в spans только при закрытии, поэтому настоящий мусор
    после незакрытой кавычки остаётся видимым. Раньше здесь стоял pop() на
    пустом списке — IndexError на первом же таком значении.
    """
    real, _ = handoff.count_mojibake(f'"незакрытая {_marker()} и дальше порча')
    assert real > 0, "незакрытая кавычка съела повреждение"


def test_damage_before_a_quote_is_not_attributed_to_it():
    text = f"{_marker()} потом \"цитата\""
    real, quoted = handoff.count_mojibake(text)
    assert real > 0, "повреждение перед кавычками приписано цитате"


def test_counts_are_per_json_value_not_per_raw_file(fake_repo):
    """Считается по ЗНАЧЕНИЯМ JSON, а не по сырому тексту файла.

    В сыром тексте кавычки из разных строк склеиваются: незакрытая кавычка
    в чужой фразе съедала следующий за собой настоящий мусор, и повреждение
    исчезало из отчёта.
    """
    _, handoff_file, _ = fake_repo
    data = read(handoff_file)
    data["hints"] = ["открытая кавычка \" не закрыта", f"и тут {_marker()} порча"]
    handoff_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    real, _ = handoff.count_mojibake_in_file(handoff_file)
    assert real > 0, "повреждение потерялось при склейке кавычек в сыром тексте"


def test_validate_reports_a_citation_as_a_warning_not_a_problem(fake_repo, capsys):
    """Пропущенное проговаривается ВСЛУХ.

    Валидатор, который что-то пропускает, обязан говорить об этом: молчаливый
    валидатор однажды перестаёт быть строгим, и заметить это некому.
    """
    _, hf, _ = fake_repo
    data = read(hf)
    data["task_log"] = [{
        "date": "2026-01-01", "task": "починил кодировку", "author": "me",
        "status": "DONE", "description": f"было «{_marker()}» - починил",
        "changed_files": ["a.py"], "verification": "pytest",
        "result": "1 passed", "coordination_note": "",
    }]
    hf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    assert run("validate") == 0, "цитата сделала гейт блокирующим"
    out = capsys.readouterr().out
    assert "внутри кавычек" in out, "пропущенное не проговорено вслух"
    assert "WARN" in out


def test_validate_still_blocks_on_real_damage(fake_repo):
    _, hf, _ = fake_repo
    data = read(hf)
    data["hints"] = [f"порча {_marker()} без кавычек"]
    hf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    assert run("validate") == 1


def test_count_never_goes_negative():
    """Защита от арифметики: total - quoted_total не может стать отрицательным.

    Подсчёт идёт по двум независимым проходам, и при изменении любой из
    сторон расхождение даст отрицательное «повреждение». Отрицательное
    число выглядело бы как «минус повреждений» и молча ломало бы счётчик.
    """
    for text in (f"\"{_marker()}\"", f"«{_marker()}»", _marker()):
        real, _ = handoff.count_mojibake(text)
        assert real >= 0, f"отрицательный счётчик на {text!r}"

# --- атомарность -------------------------------------------------------------


def test_write_json_atomic_aborts_on_stale_mtime(fake_repo):
    protocol, _, _ = fake_repo
    import time

    time.sleep(0.01)
    with pytest.raises(RuntimeError):
        handoff.write_json_atomic(protocol, {"x": 1}, expect_mtime=1.0)
