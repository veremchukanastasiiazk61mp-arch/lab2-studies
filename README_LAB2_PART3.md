# ЛР2, частина 3: серія вимірювань, розрахунки, діаграми, звіт

Файли розпакуйте поверх проєкту (`scripts/run_series.py` замінює версію з частини 2).
Потрібно: `pip install -r analysis/requirements.txt` (matplotlib); за бажанням `pip install psutil`
(тоді в таблиці кроку 11 зʼявляться завантаження процесора хоста і процесів генератора).

## Порядок дій

1. Серія вимірювань (noop, потім N = 1, 2, 3, 4; приблизно 40–60 хвилин):

       python scripts/run_series.py

   Під час серії комп'ютер не навантажуйте іншими програмами. Після завершення стенд лишається піднятим на 4 екземплярах.

2. Поки стенд працює, зробіть скріншоти і збережіть їх у `report/lab2/` з такими іменами:
   - `prometheus_targets.png`: http://localhost:9090/targets (усі 4 екземпляри web у стані UP);
   - `docker_stop.png`: вивід `python scripts/stop_check.py` (виконайте її окремо до серії або після);
   - `docker_stats_xmax.png`: за бажанням, `docker stats --no-stream`;
   - `prometheus_graph.png`: за бажанням, графік `http_requests_in_flight` у Prometheus.

3. Розрахунки і діаграми (кроки 8–11):

       python analysis/analyze.py

   Створює `results/analysis.json`, `results/analysis.md` (таблиці) і `results/figures/*.png`.

4. Звіт:

       python analysis/make_report.py

   Створює `REPORT_LAB2.md` у корені проєкту; діаграми копіюються в `report/lab2/`.
   Скрипт друкує, яких скріншотів бракує. Додайте їх і запустіть його ще раз.

5. Перевірте текст звіту (особливо розділ 6 про спільний ресурс і висновки) і закомітьте результати: `git add . && git commit && git push`.

## Якщо точка N = 1 обмежена генератором (крок 7)

`analyze.py` напише, що підбір неможливий. Тоді:
1. у `.env` зменште `WEB_CPUS` удвічі (0.5 → 0.25);
2. повторіть серію `python scripts/run_series.py --tag cpu025_ --skip-noop`;
3. `python analysis/analyze.py --tag cpu025_` і `python analysis/make_report.py --tag cpu025_ --out REPORT_LAB2.md`.

## Перевірка розрахункових функцій

    python analysis/test_usl.py
