#!/usr/bin/env python3
"""ЛР2, кроки 6–7: прогін проти /noop (межа генератора G) і ступінчасті прогони на N = 1..4.

Для кожного N:
  - піднімає стенд (WEB_REPLICAS=N), перезапускає балансувальник, чекає готовності web і стану UP
    усіх N цілей у Prometheus;
  - запускає stepload.py з --g G і під час прогону знімає docker stats (кожні ~5 с);
  - вивантажує сирі вибірки Prometheus за час прогону у results/<тег>prom_nN.json.

Запуск з кореня проєкту (Git Bash, PowerShell або Linux):
  python scripts/run_series.py                       # повна серія: noop + N=1,2,3,4
  python scripts/run_series.py --ns 1 2              # лише частина
  python scripts/run_series.py --step-rate 25        # крок сходинки для web (за замовчуванням 25)
  python scripts/run_series.py --skip-noop --g 2400  # межа G уже відома
  python scripts/run_series.py --tag cpu025_         # повтор серії з іншим лімітом CPU (крок 7)

Ліміт CPU береться з WEB_CPUS у .env. Щоб зменшити його вдвічі, змініть WEB_CPUS (0.5 -> 0.25)
і запустіть серію з іншим --tag, щоб не перезаписати результати.

Необовʼязково: `pip install psutil` — тоді додатково знімається завантаження CPU хоста і процесів
генератора (для кроку 11).
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
import prom_export  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RES = os.path.join(ROOT, "results")
PY = sys.executable


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


def run(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, check=True, text=True, **kw)


def out(cmd):
    return subprocess.run(cmd, cwd=ROOT, check=True, text=True, capture_output=True).stdout.strip()


def http_ok(url, timeout=1.0):
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


def prom_targets_up(prom):
    try:
        data = prom_export.api(prom, "/api/v1/targets", {"state": "active"})
    except Exception:  # noqa: BLE001
        return 0
    return sum(1 for t in data["activeTargets"] if t["labels"].get("job") == "web" and t["health"] == "up")


def bring_up(n, base, prom):
    env = dict(os.environ, WEB_REPLICAS=str(n))
    run(["docker", "compose", "up", "-d", "--build", "--remove-orphans"], env=env)
    run(["docker", "compose", "restart", "lb"])  # балансувальник отримує адреси поточних екземплярів
    wait(lambda: http_ok(f"{base}/healthz"), "healthz = 200")
    wait(lambda: prom_targets_up(prom) == n, f"{n} цілей web у стані UP у Prometheus", 90)
    # Усі n екземплярів мають відповідати через балансувальник
    pods = set()
    for _ in range(60 * n):
        try:
            with urllib.request.urlopen(f"{base}/livez", timeout=2) as r:
                pods.add(r.headers.get("X-Pod"))
        except Exception:  # noqa: BLE001
            pass
        if len(pods) == n:
            break
    if len(pods) != n:
        sys.exit(f"через балансувальник відповідають {len(pods)} екземплярів із {n}: {pods}")
    print(f"  стенд готовий: {n} екземпляр(ів), Prometheus бачить {n} цілей UP, X-Pod: {sorted(pods)}", flush=True)


def ensure_ids(base):
    ids_path = os.path.join(ROOT, "loadgen", "ids.json")
    if os.path.exists(ids_path):
        with open(ids_path) as f:
            first = json.load(f)[0]
        if http_ok(f"{base}/studies/{first}"):
            return
    run([PY, "loadgen/seed.py", "--url", base, "--count", "100", "--out", ids_path])


class Sampler(threading.Thread):
    """Знімає docker stats (і, якщо є psutil, завантаження хоста) раз на ~5 с."""

    def __init__(self, path):
        super().__init__(daemon=True)
        self.path, self.stop_flag = path, threading.Event()
        try:
            import psutil  # noqa: PLC0415
            self.psutil = psutil
            psutil.cpu_percent(None)
        except ImportError:
            self.psutil = None

    def run(self):
        with open(self.path, "w") as f:
            while not self.stop_flag.is_set():
                t = time.time()
                try:
                    lines = subprocess.run(
                        ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                        capture_output=True, text=True, timeout=30,
                    ).stdout.splitlines()
                    containers = [json.loads(x) for x in lines if x.strip()]
                except Exception:  # noqa: BLE001
                    containers = []
                rec = {"ts": t, "ts_end": time.time(), "containers": containers}
                if self.psutil:
                    rec["host_cpu_percent"] = self.psutil.cpu_percent(None)
                    rec["host_mem_percent"] = self.psutil.virtual_memory().percent
                    rec["gen_cpu_percent"] = self._generator_cpu()
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                self.stop_flag.wait(max(0.0, 5 - (time.time() - t)))

    def _generator_cpu(self):
        """Сума завантаження CPU процесів генератора (stepload.py і його дочірніх), % одного ядра."""
        total = 0.0
        for p in self.psutil.process_iter(["cmdline"]):
            try:
                cmd = " ".join(p.info["cmdline"] or [])
                if "stepload.py" in cmd:
                    total += p.cpu_percent(None)
            except Exception:  # noqa: BLE001
                pass
        return total


def stepload(a, name, extra):
    cmd = [PY, "-u", "loadgen/stepload.py", "--url", a.base, "--name", name, "--procs", str(a.procs), *extra]
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, text=True)
    t0 = None
    for line in proc.stdout:
        print("  ", line.rstrip(), flush=True)
        if line.startswith("T0_UNIX="):
            t0 = float(line.split("=")[1])
    proc.wait()
    if proc.returncode != 0 or t0 is None:
        sys.exit(f"stepload.py завершився з помилкою ({proc.returncode})")
    with open(os.path.join(RES, f"{name}.json")) as f:
        return t0, json.load(f)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ns", type=int, nargs="+", default=[1, 2, 3, 4])
    ap.add_argument("--base", default=f"http://localhost:{env_value('LB_PORT', '8080')}",
                    help="адреса балансувальника; за замовчуванням порт LB_PORT з .env")
    ap.add_argument("--prom", default=f"http://localhost:{env_value('PROM_PORT', '9090')}")
    ap.add_argument("--procs", type=int, default=max(1, (os.cpu_count() or 2) // 2),
                    help="процесів генератора; однакове для noop і всіх прогонів серії")
    ap.add_argument("--step-rate", type=int, default=25,
                    help="початкова інтенсивність і приріст сходинки для прогонів web, запитів/с (для /noop завжди 200)")
    ap.add_argument("--tag", default="", help="префікс імен файлів, напр. cpu025_")
    ap.add_argument("--skip-noop", action="store_true")
    ap.add_argument("--g", type=float, help="межа генератора G, якщо noop-прогін уже виконано")
    a = ap.parse_args()

    if not os.path.exists(os.path.join(ROOT, ".env")):
        sys.exit("Створіть .env: cp .env.example .env")
    os.makedirs(RES, exist_ok=True)
    manifest_path = os.path.join(RES, f"{a.tag}manifest.json")
    manifest = {"procs": a.procs, "runs": {}}
    if os.path.exists(manifest_path):
        with open(manifest_path) as f:
            manifest = json.load(f)
        manifest["procs"] = a.procs
    manifest["docker"] = {
        "ncpu": int(out(["docker", "info", "--format", "{{.NCPU}}"])),
        "mem_total_bytes": int(out(["docker", "info", "--format", "{{.MemTotal}}"])),
        "server_version": out(["docker", "version", "--format", "{{.Server.Version}}"]),
        "compose_version": out(["docker", "compose", "version", "--short"]),
    }
    manifest["step_rate"] = a.step_rate
    manifest["web_limits"] = {
        k: v for k, v in (
            line.split("=", 1) for line in open(os.path.join(ROOT, ".env")).read().splitlines()
            if line.startswith(("WEB_CPUS=", "WEB_MEMORY=", "DB_POOL_SIZE=", "DB_NAME="))
        )
    }
    manifest["web_limits"] = {k: v.split("#")[0].strip() for k, v in manifest["web_limits"].items()}

    def save():
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

    g = a.g
    if not a.skip_noop:
        print("=== Крок 6: прогін проти /noop (без обмеження межею G) ===", flush=True)
        bring_up(1, a.base, a.prom)
        _, res = stepload(a, f"{a.tag}noop", ["--noop"])
        g = res["X_rps"]
        manifest["noop"] = {"G_rps": g, "X_std_rps": res["X_std_rps"], "stop_reason": res["stop_reason"]}
        save()
    elif g is None:
        noop_path = os.path.join(RES, f"{a.tag}noop.json")
        if not os.path.exists(noop_path):
            sys.exit("Вкажіть --g або виконайте прогін проти /noop")
        with open(noop_path) as f:
            g = json.load(f)["X_rps"]
    print(f"Межа генератора G = {g} запитів/с", flush=True)
    manifest["G_rps"] = g
    save()

    for n in a.ns:
        print(f"=== Крок 7: {n} екземпляр(ів) web ===", flush=True)
        bring_up(n, a.base, a.prom)
        ensure_ids(a.base)
        name = f"{a.tag}step_n{n}"
        sampler = Sampler(os.path.join(RES, f"{name}_docker_stats.jsonl"))
        sampler.start()
        t_begin = time.time()
        try:
            t0, res = stepload(a, name, ["--g", str(g), "--step-rate", str(a.step_rate)])
        finally:
            sampler.stop_flag.set()
            sampler.join(timeout=40)
        time.sleep(7)  # дочекатись останнього скрейпа Prometheus (інтервал 5 с)
        samples = prom_export.export(a.prom, t_begin - 5, time.time(), os.path.join(RES, f"{a.tag}prom_n{n}.json"))
        manifest["runs"][str(n)] = {
            "name": name, "t0_unix": t0, "X_rps": res["X_rps"], "X_std_rps": res["X_std_rps"], "X_step": res["X_step"],
            "stop_reason": res["stop_reason"], "prom_samples": samples,
        }
        save()
        print(f"  N={n}: X = {res['X_rps']} ± {res['X_std_rps']} rps (сходинка {res['X_step']}), "
              f"G = {g}, 0.9·G = {0.9 * g:.0f}", flush=True)

    print("\nПідсумок серії (X(N), rps):")
    for n, r in sorted(manifest["runs"].items(), key=lambda kv: int(kv[0])):
        limited = " ← обмежено генератором (X ≥ 0,9·G)" if r["X_rps"] >= 0.9 * g else ""
        print(f"  N={n}: {r['X_rps']}{limited}")
    print(f"\nДалі: python analysis/analyze.py{' --tag ' + a.tag if a.tag else ''}")


if __name__ == "__main__":
    main()
