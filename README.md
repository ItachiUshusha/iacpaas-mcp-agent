# IACPaaS MCP Agent

Учебный проект по инженерии знаний «Разработка MCP-сервера и ИИ-агента для работы с графовой платформой IACPaaS». MCP-сервер читает графы IACPaaS, а Python-агент и агенты VS Code используют его инструменты. Предметные примеры — база свойств материалов и база технологических газов. Снимок корня системы единиц используется для запуска без авторизации.

Веб-чат поддерживает ранжирование аргоновых газов и сравнение примесей с исходными записями IACPaaS.

[Установка и первый запуск на Windows](#быстрый-запуск-на-windows) · [Запуск в VS Code и CLI](docs/AGENT_SETUP.md) · [Веб-чат и Cline](docs/FREE_MODEL_AND_WEB.md) · [Ответы с источниками](docs/GROUNDED_ANSWERS.md)

## Возможности и статус

| Возможность | Статус |
| --- | --- |
| Вход IACPaaS и экспорт universal JSON | Поддерживаются вход и чтение ресурсов |
| MCP stdio / Streamable HTTP с общей доменной логикой | Проверены оба транспорта |
| Схемы аргументов и результатов, ограничения, ошибки | Реализовано, покрыто тестами |
| Корень и подграф depth=1 | Доступны через MCP |
| Python-агент | CLI, диалог с моделью, проверка схем, трасса и режим `--deterministic` |
| Адаптеры OpenAI Responses / Ollama / совместимый Chat Completions API | Реализованы; ключи задаются при запуске |
| Веб-чат | Поддерживает запросы о газах и ссылки на исходные записи |
| Custom agent и MCP-конфигурация VS Code | Включены в репозиторий |
| Папки фонда | Создание и перенос в корзину через IACPaaS |
| Изменение содержимого графов | Не реализовано |

## Архитектура

```text
VS Code custom agent / Cline ──────┐
Python CLI → LLM → tool calls ─────┼─ MCP stdio / Streamable HTTP
Веб-чат → Python-агент → LLM ──────┘       → server.py → domain.py
                                              → iacpaas_client.py → IACPaaS
```

MCP API общий для всех интерфейсов. В CLI и вебе `agent_core.py` выполняет вызовы, выбранные моделью, и обрабатывает отдельные типовые запросы напрямую. VS Code и Cline управляют своим агентным циклом. `smoke.py` проверяет MCP отдельно от модели.

## Быстрый запуск на Windows

Установите Git и Python 3.11 или новее. Для запуска используйте Python 3.12, который указан в конфигурации CI. Из папки для проектов выполните в PowerShell:

```powershell
git clone https://github.com/ItachiUshusha/iacpaas-mcp-agent.git
Set-Location iacpaas-mcp-agent
.\setup.ps1
```

Установка создаёт `.venv` и использует закреплённые зависимости `requirements-lock.txt`. Скрипт выбирает установленный Python Codex, если он есть, иначе системный Python. Для установки пакетов нужна сеть.

После установки выберите [веб-чат](#веб-чат-и-облачная-модель), [VS Code](docs/AGENT_SETUP.md#начать-с-vs-code) или [Python CLI](docs/AGENT_SETUP.md#отдельное-python-приложение).

Команда `.\run.ps1 smoke` позволяет отдельно проверить MCP через stdio и HTTP на сохранённом корне. Для неё не нужны учётная запись IACPaaS и ключ модели. Ожидаемый вывод:

```text
OK: stdio, snapshot, 8 tool calls
OK: http, snapshot, 8 tool calls
```

Полный набор автоматических тестов запускается командой `.\run.ps1 test`.

Для живого API нужен аккаунт IACPaaS с доступом к ресурсу из `catalog.json`:

```powershell
.\run.ps1 smoke --mode live
```

Вход выполняется в терминале, пароль скрыт. Альтернатива — одна переменная окружения `IACPAAS_TOKEN` или `IACPAAS_API_KEY`. Значения секретов не включаются в исходники и аргументы команд.

## Веб-чат и облачная модель

Создайте API-ключ выбранного провайдера. Для OpenRouter:

```powershell
.\run.ps1 web --provider compatible --model openrouter/free --ask-key
```

Ключ вводится скрыто в терминале. Откройте `http://127.0.0.1:8080`. По умолчанию используется snapshot с корнем системы единиц; для материалов нужен живой режим.

Для работы с данными из «Моего Фонда» запустите [HTTP MCP с входом](#mcp-сервер) в первом окне PowerShell. Используйте свою учётную запись IACPaaS с доступом к нужным ресурсам и оставьте сервер запущенным. Во втором окне перейдите в ту же папку проекта и запустите веб-чат с ключом выбранного провайдера:

```powershell
.\run.ps1 web --provider compatible --model openrouter/free --ask-key --mcp-url http://127.0.0.1:8765/mcp
```

Если у вас есть ключ для Qwen через IACPaaS, во втором окне используйте эту команду:

```powershell
.\run.ps1 web --provider compatible --base-url https://aichat.iacpaas.dvo.ru/api --model qwen3.8-27b --ask-key --mcp-url http://127.0.0.1:8765/mcp --deterministic --seed 42
```

После запуска откройте [веб-чат на своём компьютере](http://127.0.0.1:8080/). Пример запроса к газовой базе «Какие общие примеси указаны у газов на основе водорода и аргона?»

История диалога находится в памяти, трассы — в исключённой из Git папке `data/traces/`. Под ответом видны фактические вызовы инструментов. Карточки чисел строятся из успешных MCP-результатов. Форматирование поддерживает основной набор Markdown; HTML модели выводится как текст. Для обновления интерфейса используйте Ctrl+F5.

`openrouter/free` выбирает бесплатную модель с нужными возможностями; доступны ограничения квоты и временные отказы провайдера. Код для OpenRouter ограничивает цену входных и выходных токенов нулём. Сменить совместимый сервис можно через `--base-url` или `AI_BASE_URL`; тариф другого сервиса определяется его провайдером.

## MCP-сервер

Локальный HTTP с входом:

```powershell
.\run.ps1 serve --transport http --mode live --login --fund-login
```

Адрес сервера — `http://127.0.0.1:8765/mcp`. Для остановки нажмите Ctrl+C. Этот адрес используют `.mcp.json` для Copilot в VS Code и `.vscode/mcp.json` для старого режима Local. Сервер рассчитан на локальную однопользовательскую работу.

Stdio на сохранённом корне:

```powershell
.\run.ps1 serve --transport stdio --mode snapshot
```

Stdio — протокольный канал для запуска клиентом; обычного приглашения в терминале не будет. Для live-stdio секрет передаётся в окружении запускающего клиента.

## Инструменты

| MCP tool | Операция IACPaaS и обработка |
| --- | --- |
| `list_inforesources` | Локальный поиск в `catalog.json` |
| `list_fund_resources` | GET `/fund/structure` в веб-сессии «Моего Фонда», фильтрация названий и путей |
| `get_inforesource` | GET `/api/data/export/user-item`, universal, depth=0 |
| `get_subgraph` | Тот же экспорт с ограниченной глубиной и необязательным `start-target-concept-path` |
| `count_graph` | Ограниченный universal-экспорт и локальный подсчёт вершин, ссылок и связей |
| `search_inforesource` | Universal-экспорт и локальный поиск подстроки в полях узлов |
| `read_export_node` | Повторный экспорт и чтение найденной записи с потомками по JSON Pointer и хешу; без придуманного пути `start` |
| `search_fund_content` | GET `/fund/structure`, затем universal-экспорт и поиск до 12 ресурсов за вызов |
| `compare_node_children` | Universal-экспорт и пересечение меток непосредственных потомков двух узлов |
| `rank_records_by_percentage` | Universal-экспорт и сортировка точных долей по подтверждённым путям в графе |
| `analyze_gas_composition` | Universal-экспорт газовой базы. Составы и рейтинг моногазов, пересечение примесей с исходными вершинами. Сохраняет операторы ≥ и ≤. |
| `create_folder` | POST `/api/storage-unit/folder/create`, только live |
| `delete_folder` | GET `/fund/structure` для проверки объекта, затем POST `/fund/deleteItem` с `recycle=true` в веб-сессии |

Каталог содержит систему единиц, базы материалов и газов и учебные онтологии. Доступ к ресурсу зависит от учётной записи IACPaaS. Для другого фонда используйте `list_fund_resources` или укажите `path`, `title` и `resource_id` доступного ресурса. [Изменение папок и воспроизводимость](docs/FOLDERS_AND_DETERMINISM.md)

Статистика относится к возвращённому фрагменту и может включать предков. Ссылки не разворачиваются; число описаний не выдаётся за точный размер полного графа. При depth=0 сохранённый корень даёт одну вершину и ноль связей.

## Агент и VS Code

[Запуск в VS Code и Python CLI](docs/AGENT_SETUP.md). [Подключение веб-чата и Cline](docs/FREE_MODEL_AND_WEB.md).

```powershell
.\run.ps1 agent --provider compatible --model openrouter/free --mode live
# Или другой провайдер:
.\run.ps1 agent --provider openai --model MODEL_ID --mode live
# Или локальный Ollama:
.\run.ps1 agent --provider ollama --model MODEL_ID --mode live
```

`MODEL_ID` заменяется точным именем доступной модели с tool calling. Совместимый API использует `AI_API_KEY`, `AI_MODEL`, `AI_BASE_URL`; OpenAI — `OPENAI_API_KEY`, `OPENAI_MODEL`. Ключи можно вводить скрыто в терминале. Модели не устанавливаются автоматически; привязки к модели преподавателя нет.

В VS Code откройте корень репозитория, запустите HTTP-сервер, проверьте `iacpaas` через `MCP: List Servers` и выберите custom agent **IACPaaS** во встроенном чате Copilot. [Подключение и примеры запросов](docs/AGENT_SETUP.md#начать-с-vs-code).

Для чата Cline установите расширение, выберите провайдера и модель, затем добавьте сервер из `config/cline-mcp.json` в настройки MCP Cline. Это отдельная конфигурация: Cline не подхватывает `.vscode/mcp.json` автоматически. Не заменяйте другие настроенные серверы при добавлении записи.

## Проверки и доказательства

Автоматические тесты охватывают чтение, агентный цикл, веб-чат, политику записи, диагностику отказов и проверку ссылок. Сценарии с подменённым провайдером проверяют поведение приложения при заданных ответах модели. Для чтения живых баз нужны доступ к IACPaaS и соответствующие права.

- [Проверка на снимке](evidence/verification-snapshot.json): корень, статистика и ожидаемые ошибки.

Новые отчёты и трассы пишутся в `data/`, исключённую из Git. `fixtures/root.json` — сохранённый реальный корень с удалёнными полями персонального пути и владельца. Он не подтверждает текущий доступ к API.

## Ограничения текущей версии

- MCP: depth=0..3, максимум 200 описаний вершин, граф до 128 КБ, ответ платформы до 2 МБ, таймаут 40 секунд.
- Snapshot: только корень, без потомков и `start`.
- Для материалов проверены ограниченные фрагменты и подсчёты. Поиск по содержимому использует глубину до 10 и отдельный лимит объёма.
- Полная официальная JSON Schema IACPaaS и проверка онтологии не реализованы.
- HTTP MCP и веб-чат слушают loopback и рассчитаны на одного локального пользователя. При выборе облачной модели ей передаётся контекст диалога и результаты MCP, нужные для ответа.

## Структура

```text
server.py              MCP-инструменты и транспорты
domain.py              каталог, папки, ограничения, проекция графа, статистика
generation.py          параметры воспроизводимости
folders_demo.py        явная проверка создания одной тестовой папки
iacpaas_client.py      HTTP-клиент IACPaaS
agent.py               CLI и подключение агента
agent_core.py          агентный цикл, политика, трасса
providers.py           OpenAI Responses / Ollama / совместимый Chat Completions
web_app.py             локальный HTTP-интерфейс Python-агента
web/                   HTML, CSS, JS и безопасное форматирование ответов
smoke.py               проверка MCP без LLM
tests/                 тесты
.github/agents/        custom agent VS Code
.mcp.json              HTTP-конфигурация для Copilot
.vscode/               HTTP-конфигурация для VS Code Local
config/                альтернативные stdio-конфигурации
evidence/              сохранённые отчёты
```

## Источники

- [Внешнее HTTP API IACPaaS](https://iacpaas.dvo.ru/docs/RemoteCalls/RemoteCalls.html)
- [JSON графов IACPaaS](https://iacpaas.dvo.ru/docs/Json/Json.html)
- [MCP Python SDK, ветка 1.x](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)
- [MCP в VS Code](https://code.visualstudio.com/docs/agent-customization/mcp-servers)
- [Custom agents](https://code.visualstudio.com/docs/agent-customization/custom-agents)
