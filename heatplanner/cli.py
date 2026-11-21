"""heatplanner CLI: collect | learn | plan [--push] | nightly | status"""
import datetime as dt
import json
import sys
import traceback

from . import data, learn, planner
from .ha import HA


def log(event, **kw):
    import os
    os.makedirs(data.DATA_DIR, exist_ok=True)
    with open(data.path("log.jsonl"), "a") as f:
        f.write(json.dumps({"at": dt.datetime.now().isoformat(timespec="seconds"), "event": event, **kw}, default=str) + "\n")


def autodetect_tariff(cfg):
    from collections import Counter
    rows = data.load_rows()[-1000:]
    cl = {"rate_cosy": Counter(), "rate_standard": Counter(), "rate_peak": Counter()}
    for r in rows:
        v = r.get("rate")
        if not v:
            continue
        key = "rate_cosy" if v < 0.2 else ("rate_standard" if v < 0.35 else "rate_peak")
        cl[key][round(v, 6)] += 1
    if all(c and c.most_common(1)[0][1] >= 5 for c in cl.values()):
        t = {k: c.most_common(1)[0][0] for k, c in cl.items()}
        cur = data.load_json("config.json", {})
        if any(abs(cfg["tariff"][k] - v) > 1e-6 for k, v in t.items()):
            cur.setdefault("tariff", {}).update(t)
            data.save_json("config.json", cur)
            log("tariff_updated", **t)
            cfg = data.load_config()
    return cfg


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    cfg = data.load_config()
    ha = HA()
    if cmd == "collect":
        days = int(argv[2]) if len(argv) > 2 else 14
        print(json.dumps(data.collect(ha, cfg, days)))
        cfg = autodetect_tariff(cfg)
    elif cmd == "learn":
        print(json.dumps(learn.run_learn(cfg, ha), indent=1))
        log("learn")
    elif cmd in ("plan", "nightly"):
        push = "--push" in argv or cmd == "nightly"
        try:
            if cmd == "nightly":
                print("collect:", data.collect(ha, cfg, 3))
                cfg = autodetect_tariff(cfg)
            params = data.load_params()
            plan = planner.build_plan(ha, cfg, params)
            if push:
                plan["pushed"] = planner.push(ha, cfg, plan)
            planner.save_plan(plan)
            try:
                planner.publish_sensors(ha, cfg, plan)
            except Exception as pe:
                log("publish_sensors_failed", error=str(pe))
            log("plan", for_date=plan["for_date"], chosen=plan["chosen"], saving=plan["saving_vs_current_gbp"], pushed=bool(plan["pushed"]))
            print(json.dumps({k: plan[k] for k in ("for_date", "weekday", "chosen", "blocks", "forecast_tomorrow", "saving_vs_current_gbp", "pushed")}, indent=1))
            for n, r in plan["strategies"].items():
                print("%-45s total £%.2f grid £%.2f pen £%.2f hp %.1f kWh tmin %.1f" % (n, r["total"], r["grid_cost"], r["penalty"], r["kwh_hp"], r["t_min_awake"]))
        except Exception as e:
            log("error", cmd=cmd, error=str(e), tb=traceback.format_exc())
            try:
                planner.publish_sensors(ha, cfg, error=e)
            except Exception:
                pass
            raise
    elif cmd == "seed":
        h, created = planner.ensure_helper(ha, cfg)
        print("helper", h["id"], "created" if created else "exists")
    elif cmd == "status":
        print(json.dumps(data.load_json("params.json", {}).get("info", {}), indent=1))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
