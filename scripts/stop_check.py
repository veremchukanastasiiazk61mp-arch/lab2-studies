#!/usr/bin/env python3
"""ЛР2, крок 3: перевірка коректної зупинки одного з двох екземплярів під навантаженням.

Що робить:
  1. піднімає стенд на 2 екземплярах web, перезапускає балансувальник, чекає готовності;
  2. запускає постійне навантаження 60 запитів/с (stepload.py: прогрів 15 с + одна сходинка 25 с);
  3. на 25-й секунді виконує `docker stop` для одного з екземплярів;
  4. перевіряє код завершення контейнера (має бути 0) і те, що за ±10 с навколо зупинки
     генератор не зафіксував відповідей не 2xx і помилок зʼєднання;
  5. записує results/stop_check.json і results/stop_check.txt, знову запускає зупинений контейнер.

Запуск з кореня проєкту:   python scripts/stop_check.py
"""
import csv
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RES = os.path.join(ROOT, "results")
PY = sys.executable
AROUND_S = 10  # вікно навколо зупинки: ±10 с


def env_value(key, fallback):
    """Значення з .env (без коментаря): порти стенду беруться звідти, а не жорстко з коду."""
    try:
        with open(os.path.join(ROOT, ".env"), encoding="utf-8") as f:
            for line in f:
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].split("#")[0].strip()
    except OSError:
        pass
    return fallback

BASE = os.environ.get("BASE") or f"http://localhost:{env_value('LB_PORT', '8080')}"


def run(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, check=True, text=True, **kw)


def out(cmd):
    return subprocess.run(cmd, cwd=ROOT, check=True, text=True, capture_output=True).stdout.strip()


def healthy(url, timeout=1.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def wait(cond, what, seconds=120):
    for _ in range(seconds):
        if cond():
            return
        time.sleep(1)
    sys.exit(f"не дочекались: {what}")


def main():
    if not os.path.exists(os.path.join(ROOT, ".env")):
        sys.exit("Створіть .env: cp .env.example .env")
    os.makedirs(RES, exist_ok=True)

    env = dict(os.environ, WEB_REPLICAS="2")
    run(["docker", "compose", "up", "-d", "--build"], env=env)
    run(["docker", "compose", "restart", "lb"])  # нові адреси екземплярів
    wait(lambda: healthy(f"{BASE}/healthz"), "healthz = 200")
    ids = os.path.join(ROOT, "loadgen", "ids.json")
    if not os.path.exists(ids):
        run([PY, "loadgen/seed.py", "--url", BASE, "--count", "100", "--out", ids])

    containers = out(["docker", "compose", "ps", "-q", "web"]).split()
    if len(containers) != 2:
        sys.exit(f"очікувалось 2 екземпляри web, є {len(containers)}")
    victim = containers[-1]
    name = out(["docker", "inspect", "--format", "{{.Name}}", victim]).lstrip("/")

    gen = subprocess.Popen(
        [PY, "-u", "loadgen/stepload.py", "--url", BASE, "--name", "stop_check", "--warmup", "15",
         "--step-rate", "60", "--step-seconds", "25", "--transient", "5", "--max-rate", "60", "--procs", "2"],
        cwd=ROOT, stdout=subprocess.PIPE, text=True,
    )
    t0 = None
    for line in gen.stdout:
        print("  генератор:", line.rstrip())
        if line.startswith("T0_UNIX="):
            t0 = float(line.split("=")[1])
            break
    assert t0, "генератор не повідомив T0_UNIX"

    time.sleep(max(0.0, t0 + 25 - time.time()))  # 10 с у сходинці після прогріву
    stop_begin = time.time()
    run(["docker", "stop", victim], stdout=subprocess.DEVNULL)
    stop_end = time.time()
    exit_code = int(out(["docker", "inspect", "--format", "{{.State.ExitCode}}", victim]))
    print(f"  docker stop {name}: {stop_end - stop_begin:.2f} с, код завершення {exit_code}")

    for line in gen.stdout:
        print("  генератор:", line.rstrip())
    gen.wait()

    off = stop_begin - t0  # момент зупинки відносно початку прогону, с
    with open(os.path.join(RES, "stop_check_seconds.csv"), newline="") as f:
        rows = list(csv.DictReader(f))

    def count(lo, hi):
        sel = [r for r in rows if lo <= int(r["t_s"]) <= hi]
        return (sum(int(r["non_2xx"]) for r in sel), sum(int(r["conn_errors"]) for r in sel),
                sum(int(r["ok_2xx"]) for r in sel))

    non2, conn, ok = count(off - AROUND_S, off + AROUND_S)
    non2_all, conn_all, ok_all = count(0, 10**9)
    result = {
        "load_rps": 60, "container": name, "exit_code": exit_code, "docker_stop_seconds": round(stop_end - stop_begin, 2),
        "stop_at_s_from_start": round(off, 2), "window_s": [round(off - AROUND_S, 1), round(off + AROUND_S, 1)],
        "window": {"ok_2xx": ok, "non_2xx": non2, "conn_errors": conn},
        "whole_run": {"ok_2xx": ok_all, "non_2xx": non2_all, "conn_errors": conn_all},
        "passed": exit_code == 0 and non2 == 0 and conn == 0,
    }
    with open(os.path.join(RES, "stop_check.json"), "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    text = (
        f"Зупинено {name} на {off:.1f} с прогону (docker stop тривав {stop_end - stop_begin:.2f} с)\n"
        f"Код завершення: {exit_code} (очікується 0)\n"
        f"За ±{AROUND_S} с навколо зупинки: відповідей 2xx {ok}, не 2xx {non2}, помилок зʼєднання {conn}\n"
        f"За весь прогін: відповідей 2xx {ok_all}, не 2xx {non2_all}, помилок зʼєднання {conn_all}\n"
        f"Результат: {'ПРОЙДЕНО' if result['passed'] else 'НЕ ПРОЙДЕНО'}\n"
    )
    with open(os.path.join(RES, "stop_check.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text)

    run(["docker", "start", victim], stdout=subprocess.DEVNULL)  # повернути стенд у початковий стан
    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
