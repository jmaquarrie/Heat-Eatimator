"""Observation store: 30-minute rows accumulated from Home Assistant history, plus forecast."""
import datetime as dt
import json
import os
import urllib.request

from . import model

UTC = dt.timezone.utc
DATA_DIR = os.environ.get("HEATPLANNER_DATA", "/opt/heatplanner/data")


def path(*p):
    return os.path.join(DATA_DIR, *p)


def load_json(name, default):
    try:
        with open(path(name)) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(name, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path(name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path(name))


def load_config():
    return model.deep_merge(model.DEFAULT_CONFIG, load_json("config.json", {}))


def load_params():
    return model.deep_merge(model.DEFAULT_PARAMS, load_json("params.json", {}).get("params", {}))


# ------------------------------------------------------------------ HA history -> 30 min grid
def _num(s):
    try:
        return float(s)
    except Exception:
        return None


def _series(hist):
    out = []
    for t, s in hist:
        v = _num(s)
        if v is not None:
            out.append((dt.datetime.fromisoformat(t), v))
    return out


def _step_value(series, t):
    v = None
    for a, b in series:
        if a <= t:
            v = b
        else:
            break
    return v


def _mean_over(series, a, b):
    tot = w = 0.0
    for i, (t, v) in enumerate(series):
        t1 = series[i + 1][0] if i + 1 < len(series) else b
        lo, hi = max(t, a), min(t1, b)
        if hi > lo:
            s = (hi - lo).total_seconds()
            tot += v * s
            w += s
    return tot / w if w else None


def _counter_increments(series, grid):
    """Spread a 'total_increasing daily' counter (resets at midnight, infrequent updates) onto a grid."""
    inc = {g: 0.0 for g in grid}
    for (t0, v0), (t1, v1) in zip(series, series[1:]):
        d = v1 - v0
        secs = (t1 - t0).total_seconds()
        if d < 0 or secs <= 0 or secs > 8 * 3600:
            continue
        for g in grid:
            lo, hi = max(g, t0), min(g + dt.timedelta(minutes=30), t1)
            if hi > lo:
                inc[g] += d * (hi - lo).total_seconds() / secs
    return inc


def _raw(hist):
    return [(dt.datetime.fromisoformat(t), s) for t, s in hist]


def _fraction(raw, a, b, on_values):
    """Fraction of [a, b) during which a state series is in on_values."""
    tot = 0.0
    for i, (t, v) in enumerate(raw):
        t1 = raw[i + 1][0] if i + 1 < len(raw) else b
        lo, hi = max(t, a), min(t1, b)
        if hi > lo and v in on_values:
            tot += (hi - lo).total_seconds()
    return tot / (b - a).total_seconds()


def _counter_increments(series, grid, onfrac=None):
    """Spread a 'total_increasing daily' counter (resets at midnight, updated every ~2 h) over a
    30-minute grid, weighted by the time the heat pump was actually heating when known."""
    inc = {g: 0.0 for g in grid}
    half = dt.timedelta(minutes=30)
    for (t0, v0), (t1, v1) in zip(series, series[1:]):
        d = v1 - v0
        secs = (t1 - t0).total_seconds()
        if d < 0 or secs <= 0 or secs > 8 * 3600:
            continue
        parts = []
        for g in grid:
            lo, hi = max(g, t0), min(g + half, t1)
            if hi > lo:
                w = (hi - lo).total_seconds()
                if onfrac is not None:
                    w *= onfrac.get(g, 0.0)
                parts.append((g, w))
        tot = sum(w for _, w in parts)
        if tot <= 1e-9:  # pump never seen heating in this span: fall back to even spread
            parts = [(g, (min(g + half, t1) - max(g, t0)).total_seconds()) for g in grid if min(g + half, t1) > max(g, t0)]
            tot = sum(w for _, w in parts) or 1.0
        for g, w in parts:
            inc[g] += d * w / tot
    return inc


def _dense_increments(series, grid):
    """Like _counter_increments but linear time, for meters that update every few seconds.
    The grid must be uniform 30-minute steps starting at grid[0]."""
    inc = {g: 0.0 for g in grid}
    if not grid:
        return inc
    g0, step, n = grid[0], 1800.0, len(grid)
    for (t0, v0), (t1, v1) in zip(series, series[1:]):
        d, secs = v1 - v0, (t1 - t0).total_seconds()
        if d <= 0 or secs <= 0 or secs > 8 * 3600:
            continue          # resets (daily counters) and long gaps are skipped
        a, b = (t0 - g0).total_seconds(), (t1 - g0).total_seconds()
        i = max(0, int(a // step))
        while i < n and i * step < b:
            lo, hi = max(a, i * step), min(b, (i + 1) * step)
            if hi > lo:
                inc[grid[i]] += d * (hi - lo) / secs
            i += 1
    return inc


def fetch_weather(lat, lon, past_days=0, forecast_days=3, tz="Europe/London"):
    """Hourly Open-Meteo data -> list of (naive datetime in tz, {t, rad (kW/m2), wind (m/s), rh})."""
    u = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
         "&hourly=temperature_2m,shortwave_radiation,wind_speed_10m,relative_humidity_2m&wind_speed_unit=ms"
         "&timezone=%s&forecast_days=%d&past_days=%d" % (lat, lon, tz.replace("/", "%2F"), forecast_days, past_days))
    d = json.load(urllib.request.urlopen(u, timeout=40))["hourly"]
    out = []
    for i, t in enumerate(d["time"]):
        if d["temperature_2m"][i] is None:
            continue
        out.append((dt.datetime.fromisoformat(t), {
            "t": d["temperature_2m"][i], "rad": (d["shortwave_radiation"][i] or 0) / 1000.0,
            "wind": d["wind_speed_10m"][i], "rh": d["relative_humidity_2m"][i]}))
    return out


def collect(ha, cfg, days=14):
    end = dt.datetime.now(UTC)
    start = (end - dt.timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
    e = cfg["entities"]
    raw, rawstate = {}, {}
    for k in ("outdoor", "room", "heat_kwh", "water_kwh", "cop", "lwt", "rate", "target"):
        raw[k] = _series(ha.history(e[k], start.isoformat(), end.isoformat()))
    for k in ("battery_soc", "battery_charge_w", "battery_discharge_w"):   # optional: real battery behaviour
        try:
            raw[k] = _series(ha.history(e[k], start.isoformat(), end.isoformat())) if e.get(k) else []
        except Exception:
            raw[k] = []
    for k in ("heating", "person"):
        try:
            rawstate[k] = _raw(ha.history(e[k], start.isoformat(), end.isoformat()))
        except Exception:
            rawstate[k] = []
    grid = []
    t = start
    while t < end:
        grid.append(t)
        t += dt.timedelta(minutes=30)
    half = dt.timedelta(minutes=30)
    onfrac = {g: _fraction(rawstate["heating"], g, g + half, ("on",)) for g in grid} if rawstate["heating"] else None
    heat = _counter_increments(raw["heat_kwh"], grid, onfrac)
    water = _counter_increments(raw["water_kwh"], grid)
    offs = []
    if e.get("lwt_offset"):
        try:   # leaving-water offset history, so the weather-curve fit can remove it
            offs = _series(ha.history_attr(e["lwt_offset"], start.isoformat(), end.isoformat(), "temperature"))
        except Exception:
            offs = []
    gridkwh = None
    if e.get("grid_import_kwh"):
        try:
            gridkwh = _dense_increments(_series(ha.history(e["grid_import_kwh"], start.isoformat(), end.isoformat())), grid)
        except Exception:
            gridkwh = None
    # weather (past model data + forecast), stored in UTC
    wx = {}
    try:
        conf = ha.rest("/api/config")
        for t_, v in fetch_weather(conf["latitude"], conf["longitude"], past_days=min(days + 1, 30), forecast_days=1, tz="UTC"):
            wx[t_.replace(tzinfo=UTC)] = v
    except Exception:
        pass
    rows = {}
    for g in grid:
        g2 = g + half
        row = {"ts": g.isoformat()}
        for k in ("outdoor", "room", "cop", "lwt", "rate", "target"):
            row[k] = _mean_over(raw[k], g, g2)
        row["heat_kwh"] = heat[g]
        row["water_kwh"] = water[g]
        row["grid_kwh"] = None if gridkwh is None else round(gridkwh[g], 4)
        row["lwt_offset"] = _mean_over(offs, g, g2) if offs else None
        for k in ("battery_soc", "battery_charge_w", "battery_discharge_w"):
            row[k] = _mean_over(raw[k], g, g2) if raw[k] else None
        row["heating"] = None if onfrac is None else onfrac[g]
        row["home"] = _fraction(rawstate["person"], g, g2, ("home",)) if rawstate["person"] else None
        w = wx.get(g.replace(minute=0))
        if w:
            row.update({"om_temp": w["t"], "rad": w["rad"], "wind": w["wind"], "rh": w["rh"]})
        rows[row["ts"]] = row
    store = load_json("obs.json", {"rows": {}})
    before = len(store["rows"])
    for k, v in rows.items():
        if v["outdoor"] is not None:
            old = store["rows"].get(k, {})
            merged = dict(old)
            merged.update({a: b for a, b in v.items() if b is not None or a not in old})
            store["rows"][k] = merged
    cutoff = (end - dt.timedelta(days=400)).isoformat()
    store["rows"] = {k: v for k, v in store["rows"].items() if k >= cutoff}
    save_json("obs.json", store)
    return {"rows_total": len(store["rows"]), "rows_added": len(store["rows"]) - before, "from": start.isoformat(),
            "heating_runtime_used": onfrac is not None, "weather_rows": len(wx)}


def load_rows():
    rows = list(load_json("obs.json", {"rows": {}})["rows"].values())
    rows.sort(key=lambda r: r["ts"])
    return rows


def series_fn(pts, key=None, bias=0.0):
    """Linear interpolation over hourly points [(naive local dt, value or dict)]."""
    pts = [(t, (v[key] if key else v)) for t, v in pts if (v if key is None else v.get(key)) is not None]

    def f(now):
        now = now.replace(tzinfo=None)
        for i, (t, v) in enumerate(pts):
            if t >= now:
                if i == 0:
                    return v + bias
                t0, v0 = pts[i - 1]
                w = (now - t0).total_seconds() / max(1.0, (t - t0).total_seconds())
                return v0 + w * (v - v0) + bias
        return pts[-1][1] + bias
    return f
