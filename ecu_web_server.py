#!/usr/bin/env python3
"""
Zynq-7010 ECU Web Server + TunerStudio .ini Parser (Zero external dependencies)
Connects to EBAZ4205 ECU via ESP32-C3 UART1 TCP bridge (default 192.168.25.134:2543).
"""

import argparse
import json
import os
import re
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# TunerStudio .ini Parser
# ---------------------------------------------------------------------------

def _strip_comment(line: str) -> str:
    in_quote = False
    out = []
    for ch in line:
        if ch == '"':
            in_quote = not in_quote
        if ch == ';' and not in_quote:
            break
        out.append(ch)
    return ''.join(out).strip()


def _split_csv(text: str):
    """Split comma-separated INI tokens respecting quotes and braces."""
    parts, cur, in_q, depth = [], [], False, 0
    for ch in text:
        if ch == '"':
            in_q = not in_q
            cur.append(ch)
        elif not in_q and ch in '[{(':
            depth += 1
            cur.append(ch)
        elif not in_q and ch in ']})':
            depth = max(0, depth - 1)
            cur.append(ch)
        elif not in_q and depth == 0 and ch == ',':
            parts.append(''.join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur or parts:
        parts.append(''.join(cur).strip())
    return parts


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s


def _parse_float(s: str, default: float = 0.0) -> float:
    try:
        return float(s.strip())
    except Exception:
        return default


def parse_ini(path: str) -> dict:
    schema = {
        "signature": "",
        "endianness": "big",
        "pageSizes": [1024, 2048],
        "ochBlockSize": 88,
        "constants": {},
        "outputChannels": {},
        "computedChannels": {},
        "tables": {},
        "curves": {},
        "dialogs": {},
        "menus": [],
        "commands": {},
    }

    section = ""
    cur_page = 1
    cur_table = None
    cur_curve = None
    cur_dialog = None
    cur_menu = None

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = _strip_comment(raw)
            if not line:
                continue

            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                cur_table = cur_curve = cur_dialog = cur_menu = None
                continue

            if "=" not in line:
                continue

            key, val = [x.strip() for x in line.split("=", 1)]

            if section == "MegaTune":
                if key == "signature":
                    schema["signature"] = _unquote(val)

            elif section == "Constants":
                if key == "endianness":
                    schema["endianness"] = val.lower()
                elif key == "pageSize":
                    schema["pageSizes"] = [int(x.strip()) for x in val.split(",")]
                elif key == "page":
                    cur_page = int(val)
                elif key in (
                    "nPages", "pageIdentifier", "pageReadCommand",
                    "pageChunkWrite", "burnCommand", "blockingFactor",
                    "interWriteDelay", "pageActivationDelay", "writeBlocks",
                ):
                    continue
                else:
                    parts = _split_csv(val)
                    if len(parts) < 4:
                        continue
                    kind = parts[0].lower()
                    dtype = parts[1].upper()
                    offset = int(parts[2])
                    if kind == "scalar" and len(parts) >= 9:
                        schema["constants"][key] = {
                            "name": key,
                            "kind": "scalar",
                            "page": cur_page - 1,
                            "type": dtype,
                            "offset": offset,
                            "units": _unquote(parts[3]),
                            "scale": _parse_float(parts[4], 1.0),
                            "translate": _parse_float(parts[5], 0.0),
                            "min": _parse_float(parts[6], 0.0),
                            "max": _parse_float(parts[7], 65535.0),
                            "digits": int(_parse_float(parts[8], 0)),
                        }
                    elif kind == "bits" and len(parts) >= 5:
                        m = re.match(r"\[(\d+):(\d+)\]", parts[3])
                        lo, hi = (int(m.group(1)), int(m.group(2))) if m else (0, 7)
                        opts = [_unquote(p) for p in parts[4:]]
                        schema["constants"][key] = {
                            "name": key,
                            "kind": "bits",
                            "page": cur_page - 1,
                            "type": dtype,
                            "offset": offset,
                            "bitLo": lo,
                            "bitHi": hi,
                            "options": opts,
                        }
                    elif kind == "array" and len(parts) >= 10:
                        shape_str = parts[3].strip("[] ")
                        if "x" in shape_str.lower():
                            r, c = [int(x) for x in shape_str.lower().split("x")]
                            shape = [r, c]
                        else:
                            shape = [int(shape_str)]
                        schema["constants"][key] = {
                            "name": key,
                            "kind": "array",
                            "page": cur_page - 1,
                            "type": dtype,
                            "offset": offset,
                            "shape": shape,
                            "units": _unquote(parts[4]),
                            "scale": _parse_float(parts[5], 1.0),
                            "translate": _parse_float(parts[6], 0.0),
                            "min": _parse_float(parts[7], 0.0),
                            "max": _parse_float(parts[8], 65535.0),
                            "digits": int(_parse_float(parts[9], 0)),
                        }

            elif section == "OutputChannels":
                if key == "ochBlockSize":
                    schema["ochBlockSize"] = int(val)
                elif key in ("ochGetCommand", "ochReady"):
                    continue
                elif val.startswith("{") and val.endswith("}"):
                    schema["computedChannels"][key] = val[1:-1].strip()
                else:
                    parts = _split_csv(val)
                    if len(parts) >= 6 and parts[0].lower() == "scalar":
                        schema["outputChannels"][key] = {
                            "name": key,
                            "type": parts[1].upper(),
                            "offset": int(parts[2]),
                            "units": _unquote(parts[3]),
                            "scale": _parse_float(parts[4], 1.0),
                            "translate": _parse_float(parts[5], 0.0),
                        }

            elif section == "TableEditor":
                if key == "table":
                    parts = _split_csv(val)
                    tid = parts[0]
                    cur_table = {
                        "id": tid,
                        "map3d": parts[1] if len(parts) > 1 else "",
                        "title": _unquote(parts[2]) if len(parts) > 2 else tid,
                        "page": int(parts[3]) - 1 if len(parts) > 3 else 1,
                        "xBins": "", "xLive": "",
                        "yBins": "", "yLive": "",
                        "zBins": "",
                    }
                    schema["tables"][tid] = cur_table
                elif cur_table is not None:
                    parts = _split_csv(val)
                    if key == "xBins":
                        cur_table["xBins"] = parts[0]
                        cur_table["xLive"] = parts[1] if len(parts) > 1 else ""
                    elif key == "yBins":
                        cur_table["yBins"] = parts[0]
                        cur_table["yLive"] = parts[1] if len(parts) > 1 else ""
                    elif key == "zBins":
                        cur_table["zBins"] = parts[0]

            elif section == "CurveEditor":
                if key == "curve":
                    parts = _split_csv(val)
                    cid = parts[0]
                    cur_curve = {
                        "id": cid,
                        "title": _unquote(parts[1]) if len(parts) > 1 else cid,
                        "help": "",
                        "xLabel": "X", "yLabel": "Y",
                        "xBins": "", "xLive": "", "yBins": "",
                    }
                    schema["curves"][cid] = cur_curve
                elif cur_curve is not None:
                    parts = _split_csv(val)
                    if key == "topicHelp":
                        cur_curve["help"] = _unquote(val)
                    elif key == "columnLabel" and len(parts) >= 2:
                        cur_curve["xLabel"] = _unquote(parts[0])
                        cur_curve["yLabel"] = _unquote(parts[1])
                    elif key == "xBins":
                        cur_curve["xBins"] = parts[0]
                        cur_curve["xLive"] = parts[1] if len(parts) > 1 else ""
                    elif key == "yBins":
                        cur_curve["yBins"] = parts[0]

            elif section == "ControllerCommands":
                schema["commands"][key] = _unquote(val)

            elif section == "UserDefined":
                if key == "dialog":
                    parts = _split_csv(val)
                    did = parts[0]
                    cur_dialog = {
                        "id": did,
                        "title": _unquote(parts[1]) if len(parts) > 1 else did,
                        "fields": [],
                    }
                    schema["dialogs"][did] = cur_dialog
                elif cur_dialog is not None:
                    if key == "field":
                        parts = _split_csv(val)
                        if len(parts) >= 2:
                            cond = ""
                            if len(parts) >= 3:
                                c = parts[2].strip()
                                if c.startswith("{") and c.endswith("}"):
                                    cond = c[1:-1].strip()
                            cur_dialog["fields"].append({
                                "kind": "field",
                                "label": _unquote(parts[0]),
                                "var": parts[1].strip(),
                                "cond": cond,
                            })
                    elif key == "commandButton":
                        parts = _split_csv(val)
                        if len(parts) >= 2:
                            cur_dialog["fields"].append({
                                "kind": "command",
                                "label": _unquote(parts[0]),
                                "cmd": parts[1].strip(),
                            })

            elif section == "Menu":
                if key == "menu":
                    cur_menu = {
                        "title": _unquote(val).replace("&", ""),
                        "items": [],
                    }
                    schema["menus"].append(cur_menu)
                elif key == "subMenu" and cur_menu is not None:
                    parts = _split_csv(val)
                    if len(parts) >= 2:
                        cur_menu["items"].append({
                            "target": parts[0].strip(),
                            "label": _unquote(parts[1]).replace("&", ""),
                        })

    return schema


# ---------------------------------------------------------------------------
# ECU TCP Bridge Client (Q / A / r / w / b)
# ---------------------------------------------------------------------------

FMT_MAP = {
    "U08": (">B", 1),
    "S08": (">b", 1),
    "U16": (">H", 2),
    "S16": (">h", 2),
    "U32": (">I", 4),
    "S32": (">i", 4),
}


class EcuTcpClient:
    def __init__(self, host: str, port: int, timeout: float = 1.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.lock = threading.Lock()
        self.sock = None
        self.last_sig = ""
        self.last_error = ""
        self.connected = False

    def _close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        self.connected = False

    def _ensure_conn(self):
        if self.sock is None:
            s = socket.create_connection((self.host, self.port), timeout=self.timeout)
            s.settimeout(self.timeout)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock = s
            self.connected = True

    def _drain(self):
        if self.sock is None:
            return
        self.sock.setblocking(False)
        try:
            while True:
                chunk = self.sock.recv(1024)
                if not chunk:
                    break
        except BlockingIOError:
            pass
        except Exception:
            pass
        finally:
            self.sock.settimeout(self.timeout)

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        deadline = time.monotonic() + self.timeout
        while len(buf) < n:
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError(f"ECU timeout ({len(buf)}/{n} bytes)")
            self.sock.settimeout(rem)
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("ECU TCP closed")
            buf.extend(chunk)
        return bytes(buf)

    def _txrx(self, payload: bytes, expected_len: int) -> bytes:
        with self.lock:
            for attempt in range(2):
                try:
                    self._ensure_conn()
                    self._drain()
                    self.sock.sendall(payload)
                    data = self._recv_exact(expected_len) if expected_len > 0 else b""
                    self.last_error = ""
                    self.connected = True
                    return data
                except Exception as e:
                    self.last_error = str(e)
                    self._close()
                    if attempt == 1:
                        raise

    def query(self) -> str:
        raw = self._txrx(b"Q", 16)
        self.last_sig = raw.decode("ascii", errors="replace").strip("\x00 ")
        return self.last_sig

    def read_och(self, size: int = 88) -> bytes:
        return self._txrx(b"A", size)

    def read_page(self, page_idx: int, total_size: int) -> bytes:
        out = bytearray()
        chunk = 256
        for off in range(0, total_size, chunk):
            cnt = min(chunk, total_size - off)
            cmd = struct.pack(">cHHH", b"r", page_idx, off, cnt)
            out.extend(self._txrx(cmd, cnt))
        return bytes(out)

    def write_bytes(self, page_idx: int, offset: int, data: bytes):
        cmd = struct.pack(">cHHH", b"w", page_idx, offset, len(data)) + data
        self._txrx(cmd, 0)

    def burn(self, page_idx: int):
        cmd = struct.pack(">cH", b"b", page_idx)
        self._txrx(cmd, 0)

    def send_raw(self, raw_bytes: bytes):
        self._txrx(raw_bytes, 0)


def decode_och(raw: bytes, schema: dict) -> dict:
    vals = {}
    for name, ch in schema["outputChannels"].items():
        fmt, sz = FMT_MAP.get(ch["type"], (">H", 2))
        off = ch["offset"]
        if off + sz <= len(raw):
            v = struct.unpack_from(fmt, raw, off)[0]
            vals[name] = round((v + ch["translate"]) * ch["scale"], 4)
    st = int(vals.get("status_flags", 0))
    cs = int(vals.get("compressor_status", 0))
    af = int(vals.get("active_faults", 0))
    pl = int(vals.get("prot_level", 0))
    rp = int(vals.get("reserved_prot", 0))
    vals.update({
        "engine_synced": bool(st & 0x01),
        "stroke_720": bool(st & 0x02),
        "ecu_disable_active": bool(st & 0x04),
        "limp_mode_active": pl >= 2,
        "check_engine_light": pl >= 1,
        "compressor_running": bool(cs & 0x01),
        "compressor_fault": bool(cs & 0x02),
        "vanos_active": bool(cs & 0x04),
        "ac_clutch_active": bool(cs & 0x08),
        "launch_active": bool(cs & 0x10),
        "cam_phase_fault": bool(cs & 0x20),
        "cal_page0_dirty": bool(rp & 0x01),
        "cal_page1_dirty": bool(rp & 0x02),
        "cal_burn_ok": bool(rp & 0x04),
        "cal_burn_refused": bool(rp & 0x08),
        "cal_burn_error": bool(rp & 0x10),
        "has_faults": af != 0,
    })
    return vals


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

def make_handler(schema: dict, ecu: EcuTcpClient, html_path: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return  # quiet console

        def _send_json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            ln = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(ln) if ln > 0 else b"{}"
            return json.loads(raw.decode("utf-8"))

        def do_GET(self):
            path = urlparse(self.path).path

            if path in ("/", "/index.html"):
                with open(html_path, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/api/schema":
                self._send_json(schema)
                return

            if path == "/api/live":
                t0 = time.monotonic()
                try:
                    if not ecu.last_sig:
                        ecu.query()
                    raw = ecu.read_och(schema["ochBlockSize"])
                    dt_ms = round((time.monotonic() - t0) * 1000.0, 1)
                    self._send_json({
                        "connected": True,
                        "signature": ecu.last_sig,
                        "latency_ms": dt_ms,
                        "data": decode_och(raw, schema),
                    })
                except Exception as e:
                    self._send_json({
                        "connected": False,
                        "signature": ecu.last_sig,
                        "error": str(e),
                        "data": {},
                    })
                return

            if path == "/api/pages":
                try:
                    sig = ecu.query()
                    pages = []
                    for idx, sz in enumerate(schema["pageSizes"]):
                        pages.append(list(ecu.read_page(idx, sz)))
                    self._send_json({
                        "connected": True,
                        "signature": sig,
                        "pages": pages,
                    })
                except Exception as e:
                    self._send_json({"connected": False, "error": str(e)}, status=503)
                return

            self.send_error(404)

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                body = self._read_json()
            except Exception as e:
                self._send_json({"ok": False, "error": f"bad json: {e}"}, status=400)
                return

            if path == "/api/write":
                try:
                    writes = body.get("writes")
                    if writes is None:
                        writes = [body]
                    for w in writes:
                        page = int(w["page"])
                        offset = int(w["offset"])
                        data = bytes(int(b) & 0xFF for b in w["bytes"])
                        ecu.write_bytes(page, offset, data)
                    self._send_json({"ok": True, "count": len(writes)})
                except Exception as e:
                    self._send_json({"ok": False, "error": str(e)}, status=503)
                return

            if path == "/api/burn":
                try:
                    pages = body.get("pages", [0, 1])
                    for p in pages:
                        ecu.burn(int(p))
                    self._send_json({"ok": True, "pages": pages})
                except Exception as e:
                    self._send_json({"ok": False, "error": str(e)}, status=503)
                return

            if path == "/api/cmd":
                try:
                    cmd_name = body.get("cmd", "")
                    raw_cmd = schema["commands"].get(cmd_name, "")
                    if not raw_cmd:
                        self._send_json({"ok": False, "error": "unknown cmd"}, status=400)
                        return
                    ecu.send_raw(raw_cmd.encode("ascii"))
                    self._send_json({"ok": True, "cmd": cmd_name})
                except Exception as e:
                    self._send_json({"ok": False, "error": str(e)}, status=503)
                return

            self.send_error(404)

    return Handler


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    default_ini = os.path.abspath(
        os.path.join(here, "..", "..", "firmware_fpga", "tunerstudio", "custom_zynq_ecu.ini")
    )
    ap = argparse.ArgumentParser(description="Zynq-7010 ECU Web UI Server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--ecu-host", default="192.168.25.134")
    ap.add_argument("--ecu-port", type=int, default=2543)
    ap.add_argument("--ini", default=default_ini)
    args = ap.parse_args()

    schema = parse_ini(args.ini)
    print(
        f"[INI] Loaded {os.path.basename(args.ini)}: "
        f"{len(schema['constants'])} constants, {len(schema['outputChannels'])} OCH, "
        f"{len(schema['dialogs'])} dialogs, {len(schema['tables'])} tables, "
        f"{len(schema['curves'])} curves"
    )

    ecu = EcuTcpClient(args.ecu_host, args.ecu_port, timeout=1.0)
    html_path = os.path.join(here, "index.html")
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(schema, ecu, html_path))
    print(f"[WEB] http://{args.host}:{args.port}  ->  ECU tcp://{args.ecu_host}:{args.ecu_port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
