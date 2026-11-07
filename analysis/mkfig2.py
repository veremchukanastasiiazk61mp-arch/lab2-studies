
import statistics, sys, json
sys.path.insert(0, "../l2/z/046be806-lab2-part3/analysis")
import matplotlib
matplotlib.use("Agg")
from analyze import SERIES, SURFACE, INK2, fmt, setup_matplotlib
from usl import fit_usl, usl
# (X сходинки, стандартне відхилення за 15 с) для кожного прогону: manifest.json і step_*.json у results/
RUNS = {1: [(150.133, 2.696), (198.4, 6.555), (295.067, 44.466), (374.733, 18.437)],
        2: [(550.533, 51.226), (623.467, 64.029)],
        3: [(623.6, 124.979), (648.933, 161.345)],
        4: [(423.467, 12.944), (706.467, 104.451)]}
rows = []
for n, v in sorted(RUNS.items()):
    v = sorted(v); mid = v[1:3] if len(v) == 4 else v
    rows.append({"N": n, "X": statistics.median(a for a, _ in v), "std": statistics.fmean(b for _, b in mid), "runs": [a for a, _ in v]})
x1 = rows[0]["X"]
fit = fit_usl(x1, [(r["N"], r["X"]) for r in rows[1:]])
plt = setup_matplotlib()
fig, ax = plt.subplots(figsize=(8, 5), constrained_layout=True)
xm = 6; ns = [i / 10 for i in range(10, xm * 10 + 1)]
ax.plot(ns, [x1 * n for n in ns], color=INK2, linewidth=1.2, label=f"лінійне λN, λ = X(1) = {fmt(x1, 0)}")
ax.plot(ns, [usl(n, x1, fit["sigma"], fit["kappa"]) for n in ns], color=SERIES[0], label=f"закон: σ = {fmt(fit['sigma'], 3)}, κ = {fmt(fit['kappa'], 4)}")
for r in rows:
    ax.scatter([r["N"]] * len(r["runs"]), r["runs"], facecolors="none", edgecolors=SERIES[2], s=40, zorder=3,
               label="окремі прогони" if r["N"] == 1 else None)
ax.errorbar([r["N"] for r in rows], [r["X"] for r in rows], yerr=[r["std"] for r in rows], fmt="o", color=SERIES[1], ecolor=SERIES[1],
            markersize=8, markeredgecolor=SURFACE, markeredgewidth=2, capsize=4, zorder=4,
            label="медіана X(N) ± стандартне відхилення на сходинці")
ax.set_xlabel("кількість екземплярів N"); ax.set_ylabel("X(N), запитів за секунду")
ax.set_title("Пропускна здатність залежно від кількості екземплярів")
ax.set_xticks(range(1, xm + 1)); ax.set_ylim(0, 1800)
ax.text(0.995, 0.02, "межа генератора G = 5487 лежить вище за межі графіка", transform=ax.transAxes, ha="right", fontsize=8.5, color=INK2)
ax.legend(loc="upper left")
fig.savefig("usl_compose_std.png"); plt.close(fig)
json.dump({"rows": rows, "fit": {k: fit[k] for k in ("sigma", "kappa", "N_star", "X_at_N_star")}}, open("rows.json", "w"))
print([(r["N"], round(r["X"], 2), round(r["std"], 2)) for r in rows], fit["sigma"], fit["kappa"])
