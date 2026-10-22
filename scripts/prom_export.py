#!/usr/bin/env python3
"""Вивантаження сирих вибірок із Prometheus у JSON (ЛР2, кроки 7–11).

Діаграми і розрахунки будуються з цього файла, тому вони відтворюються і після
видалення томів Compose (docker compose down -v).

Для кожного селектора береться запит виду  селектор[<тривалість>s]  на момент end:
це сирі вибірки з їхніми справжніми часовими мітками (не інтерполяція query_range).

Запуск окремо:
  python scripts/prom_export.py --prom http://localhost:9090 --start <unix> --end <unix> --out results/prom_n1.json
"""
import argparse
import json
import time
import urllib.parse
import urllib.request

# імʼя в файлі -> селектор PromQL
SELECTORS = {
    "bucket": 'http_request_duration_seconds_bucket{job="web",path="/studies/{study_id}"}',
    "requests": 'http_requests_total{job="web"}',
    "cpu_seconds": 'process_cpu_seconds_total{job="web"}',
    "rss_bytes": 'process_resident_memory_bytes{job="web"}',
    "in_flight": 'http_requests_in_flight{job="web"}',
    "up": 'up{job="web"}',
    # М2 (варіант 5): postgres-exporter
    "pg_conn": "pg_conn_count",
    "pg_xact_commit": "pg_stat_database_xact_commit",
    "pg_xact_rollback": "pg_stat_database_xact_rollback",
    "pg_numbackends": "pg_stat_database_numbackends",
}


def api(prom, path, params):
    url = f"{prom.rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as r:
        body = json.load(r)
    if body.get("status") != "success":
        raise RuntimeError(f"Prometheus: {body}")
    return body["data"]


def prom_clock_offset(prom):
    """host_time − prometheus_time, с. Годинник віртуальної машини Docker може відставати."""
    before = time.time()
    data = api(prom, "/api/v1/query", {"query": "time()"})
    after = time.time()
    return round((before + after) / 2 - float(data["result"][1]), 3)


def export(prom, host_start, host_end, out_path, extra=None):
    """host_start/host_end — unix-час за годинником хоста; у файлі часові мітки вибірок
    залишаються за годинником Prometheus, а зсув clock_offset_s (хост − Prometheus) записано поруч."""
    offset = prom_clock_offset(prom)
    start, end = host_start - offset, host_end - offset
    duration = int(end - start) + 1
    result = {"host_start": host_start, "host_end": host_end, "prom": prom, "clock_offset_s": offset, "series": {}}
    for name, sel in SELECTORS.items():
        data = api(prom, "/api/v1/query", {"query": f"{sel}[{duration}s]", "time": end})
        result["series"][name] = [
            {"metric": s["metric"], "values": [[float(t), float(v)] for t, v in s["values"]]}
            for s in data["result"]
        ]
    if extra:
        result.update(extra)
    with open(out_path, "w") as f:
        json.dump(result, f)
    n = sum(len(s["values"]) for v in result["series"].values() for s in v)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prom", default="http://localhost:9090")
    ap.add_argument("--start", type=float, required=True, help="unix-час початку (за годинником хоста)")
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    n = export(a.prom, a.start, a.end, a.out)
    print(f"{a.out}: {n} вибірок")


if __name__ == "__main__":
    main()
