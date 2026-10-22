# ЛР2, частина 2: стенд із Prometheus і ступінчастий генератор

Файли розпакуйте поверх теки проєкту (`docker-compose.yml` і `.env.example` замінюють файли з частини 1).
Потрібно: `cp .env.example .env` (змініть пароль), Docker Compose v2, Python 3.9+.

| Що | Команда |
|---|---|
| Стенд на 4 екземплярах | `WEB_REPLICAS=4 docker compose up -d --build` (або `docker compose up -d --build --scale web=4`), далі `docker compose restart lb` |
| Сторінка Targets (мають бути 4 цілі UP) | http://localhost:9090/targets |
| Метрики одного екземпляра | `curl localhost:8080/metrics` |
| Коректна зупинка (крок 3) | `python scripts/stop_check.py` |
| Серія: /noop, потім N = 1, 2, 3, 4 (кроки 6–7) | `python scripts/run_series.py` |
| Один ступінчастий прогін вручну | `python loadgen/stepload.py --url http://localhost:8080 --name test --procs 4` |
| Прогін проти /noop вручну | `python loadgen/stepload.py --url http://localhost:8080 --noop --name noop --procs 4` |

Генератор потребує лише стандартної бібліотеки Python. Однакове значення `--procs` має бути в прогоні /noop і в усіх прогонах серії.
