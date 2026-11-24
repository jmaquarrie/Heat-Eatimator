"""Change log for each day's setpoints: who changed what, when and why (shown on the dashboard charts)."""
import datetime as dt
from zoneinfo import ZoneInfo

from . import data

LOCAL = ZoneInfo("Europe/London")
FILE = "revisions.json"
KEEP_DAYS = 21


def _raw(cfg, values, offsets):
    return [dict({"from": a, "to": b, "temperature": v}, **({"lwt_offset": offsets[i]} if offsets is not None else {}))
            for i, ((a, b), v) in enumerate(zip(cfg["blocks"], values))]


def record(cfg, date_iso, by, before_vals, before_offs, after_vals, after_offs, reason=None, kind="edit"):
    """Append one revision. by: "you" | "hermes" | "planner". Returns the revision (or None if nothing changed)."""
    changed = [i for i in range(len(cfg["blocks"]))
               if abs(before_vals[i] - after_vals[i]) > 1e-9
               or (before_offs is not None and after_offs is not None and before_offs[i] != after_offs[i])]
    if not changed and kind == "edit":
        return None
    store = data.load_json(FILE, {})
    cutoff = (dt.date.today() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    store = {k: v for k, v in store.items() if k >= cutoff}
    day = store.setdefault(date_iso, [])
    rev = {"id": len(day) + 1, "at": dt.datetime.now(LOCAL).isoformat(timespec="seconds"), "by": by, "kind": kind,
           "reason": (reason or "")[:500], "changed": changed,
           "before": _raw(cfg, before_vals, before_offs), "after": _raw(cfg, after_vals, after_offs)}
    day.append(rev)
    data.save_json(FILE, store)
    return rev


def for_date(date_iso):
    return data.load_json(FILE, {}).get(date_iso, [])


def describe(cfg, rev):
    """Short plain-English list of what a revision changed, e.g. '16:00-19:00 20.0 -> 20.5, water -2 -> 0'."""
    out = []
    for i in rev["changed"]:
        b, a = rev["before"][i], rev["after"][i]
        parts = []
        if abs(b["temperature"] - a["temperature"]) > 1e-9:
            parts.append("%.1f° → %.1f°" % (b["temperature"], a["temperature"]))
        if "lwt_offset" in b and b.get("lwt_offset") != a.get("lwt_offset"):
            parts.append("water %+g → %+g" % (b["lwt_offset"], a["lwt_offset"]))
        out.append("%02d:00–%02d:00 %s" % (b["from"], b["to"], ", ".join(parts)))
    return out
