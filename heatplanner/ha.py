"""Minimal Home Assistant client: REST + stdlib websocket (no third-party deps)."""
import base64, json, os, socket, ssl, struct, urllib.parse, urllib.request


class HA:
    def __init__(self, url=None, token=None):
        self.url = (url or os.environ["HASS_URL"]).rstrip("/")
        self.token = token or os.environ["HASS_TOKEN"]

    # ---- REST ----
    def rest(self, path, method="GET", body=None, timeout=60):
        req = urllib.request.Request(
            self.url + path, method=method,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
            data=None if body is None else json.dumps(body).encode())
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        return json.loads(raw) if raw else None

    def state(self, entity_id):
        return self.rest("/api/states/" + entity_id)

    def history(self, entity_id, start_iso, end_iso):
        q = {"filter_entity_id": entity_id, "end_time": end_iso, "minimal_response": "", "no_attributes": ""}
        r = self.rest("/api/history/period/%s?%s" % (urllib.parse.quote(start_iso), urllib.parse.urlencode(q)), timeout=180)
        return [(p["last_changed"], p["state"]) for p in r[0]] if r else []

    # ---- websocket (a short-lived connection per batch of commands) ----
    def ws_commands(self, commands, timeout=30):
        u = urllib.parse.urlparse(self.url)
        port = u.port or (443 if u.scheme == "https" else 80)
        sock = socket.create_connection((u.hostname, port), timeout=timeout)
        if u.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(("GET /api/websocket HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (u.hostname, port, key)).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = sock.recv(4096)
            if not c:
                raise IOError("ws handshake failed")
            buf += c
        head, pending = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise IOError("ws upgrade refused: " + head.decode(errors="replace")[:100])
        state = {"buf": pending}

        def send(obj):
            data = json.dumps(obj).encode()
            mask = os.urandom(4)
            n = len(data)
            if n < 126:
                hdr = bytes([0x81, 0x80 | n])
            elif n < 65536:
                hdr = bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
            else:
                hdr = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", n)
            sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

        def need(n):
            while len(state["buf"]) < n:
                c = sock.recv(65536)
                if not c:
                    raise IOError("ws closed")
                state["buf"] += c
            out, state["buf"] = state["buf"][:n], state["buf"][n:]
            return out

        def recv():
            msg = b""
            while True:
                b1, b2 = need(2)
                op, ln = b1 & 0x0F, b2 & 0x7F
                if ln == 126:
                    ln = struct.unpack(">H", need(2))[0]
                elif ln == 127:
                    ln = struct.unpack(">Q", need(8))[0]
                payload = need(ln)
                if op == 9:
                    continue
                if op == 8:
                    raise IOError("ws closed by server")
                msg += payload
                if b1 & 0x80:
                    return json.loads(msg)

        try:
            recv()  # auth_required
            send({"type": "auth", "access_token": self.token})
            r = recv()
            if r.get("type") != "auth_ok":
                raise IOError("ws auth failed: %s" % r)
            results = []
            for i, cmd in enumerate(commands, 1):
                cmd = dict(cmd)
                cmd["id"] = i
                send(cmd)
                while True:
                    r = recv()
                    if r.get("id") == i:
                        break
                results.append(r)
            return results
        finally:
            try:
                sock.close()
            except Exception:
                pass

    # ---- schedule helper ----
    def schedule_list(self):
        r = self.ws_commands([{"type": "schedule/list"}])[0]
        if not r.get("success"):
            raise RuntimeError(r)
        return r["result"]

    def schedule_create(self, name, icon="mdi:heat-pump"):
        r = self.ws_commands([{"type": "schedule/create", "name": name, "icon": icon}])[0]
        if not r.get("success"):
            raise RuntimeError(r)
        return r["result"]

    def schedule_update(self, schedule_id, fields):
        r = self.ws_commands([{"type": "schedule/update", "schedule_id": schedule_id, **fields}])[0]
        if not r.get("success"):
            raise RuntimeError("schedule update failed: %s" % r)
        return r["result"]
