# teamwork-agent — команды шаблона.
#
# Логика лежит в scripts/dev.py, этот Makefile — тонкая обёртка над ней.
# Так сделано намеренно: `make` есть не на всех машинах (на Windows часто
# отсутствует), а непроверяемый Makefile хуже проверяемого Python-раннера.
# Если make недоступен, выполняйте те же команды напрямую:
#
#     python scripts/dev.py <doctor|test|gates|status|clean>

PY      ?= python
DEV     := $(PY) scripts/dev.py

.DEFAULT_GOAL := help
.PHONY: help setup doctor test gates status validate lint dashboard init clean

## help — список команд
help:
	@echo "teamwork-agent — доступные команды:"
	@echo ""
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## /  /' | column -t -s ':'
	@echo ""
	@echo "Переменные: PY=$(PY)"

## setup — поставить зависимости шаблона (pytest, ruff, fastapi)
setup:
	$(PY) -m pip install -e ".[all]"

## doctor — что готово, что нет (ничего не чинит)
doctor:
	$(DEV) doctor

## test — pytest
test:
	$(DEV) test

## gates — ВСЕ гейты разом, сводка в конце; падение одного не отменяет остальные
gates:
	$(DEV) gates

## gates-list — только показать список гейтов, не запускать
gates-list:
	$(DEV) gates --list

## status — картина команды: claims, зоны, подсказки
status:
	$(DEV) status

## validate — целостность координационных файлов
validate:
	$(PY) scripts/agent_handoff.py validate

## health — полная сводка: P0/P1, протухшие claims, кодировка
health:
	$(PY) scripts/agent_handoff.py health-check

## lint — ruff
lint:
	$(PY) -m ruff check scripts tests

## dashboard — read-only дашборд команды на :18900 (нужны fastapi + uvicorn)
dashboard:
	$(PY) scripts/agent_dashboard.py

## clean — убрать сгенерированное (данные проекта не трогает)
clean:
	$(DEV) clean
