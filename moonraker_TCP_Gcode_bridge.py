#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import time
from typing import Optional, Dict, Any, Set, Tuple

import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("BridgeWithWebConsole")

# =========================
# Config
# =========================
TCP_HOST = os.getenv("TCP_HOST", "0.0.0.0")
TCP_PORT = int(os.getenv("TCP_PORT", "4125"))

MOONRAKER_WS = os.getenv("MOONRAKER_WS", "ws://127.0.0.1:7125/websocket")

# Web console
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_HTTP_PORT = int(os.getenv("WEB_HTTP_PORT", "8080"))
WEB_WS_PORT = int(os.getenv("WEB_WS_PORT", "8081"))  # 브라우저가 붙는 WS 포트

# Optional auth (shared for TCP + Web console)
BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN")  # if set, require auth for TCP and Web WS

# Moonraker identify auth (optional)
MOONRAKER_TOKEN = os.getenv("MOONRAKER_TOKEN")
MOONRAKER_API_KEY = os.getenv("MOONRAKER_API_KEY")

CLIENT_NAME = os.getenv("CLIENT_NAME", "MultiBridge")
CLIENT_VERSION = os.getenv("CLIENT_VERSION", "1.0")
CLIENT_TYPE = os.getenv("CLIENT_TYPE", "other")

RPC_TIMEOUT_S = float(os.getenv("RPC_TIMEOUT_S", "5.0"))

# Reconnect backoff
RECONNECT_BASE_S = float(os.getenv("RECONNECT_BASE_S", "1.0"))
RECONNECT_MAX_S = float(os.getenv("RECONNECT_MAX_S", "30.0"))
RECONNECT_FACTOR = float(os.getenv("RECONNECT_FACTOR", "1.6"))

# Filter noisy ok from notify to avoid spamming all viewers
FILTER_OK_IN_NOTIFY = os.getenv("FILTER_OK_IN_NOTIFY", "1").strip() not in ("0", "false", "False")


# =========================
# Helpers
# =========================
def normalize_gcode(line: str) -> Optional[str]:
    s = line.replace("\r", "").strip()
    if not s:
        return None
    s = s.split(";", 1)[0].strip()
    if not s:
        return None
    return s


class RPCError(RuntimeError):
    pass


class MoonrakerClient:
    """Single shared Moonraker WS connection with reconnect + RPC matching."""
    def __init__(self):
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._id_counter = 1
        self._id_lock = asyncio.Lock()

        self._connected_event = asyncio.Event()
        self._stop = False

        # global serialized queue: (origin, gcode)
        self._gcode_queue: "asyncio.Queue[Tuple[Any, str]]" = asyncio.Queue()

        self._ws_rx_task: Optional[asyncio.Task] = None
        self._gcode_worker_task: Optional[asyncio.Task] = None
        self._reconnector_task: Optional[asyncio.Task] = None

        # callback to push console lines outward
        self.on_gcode_response = None  # async callable(str)->None

    async def start(self):
        self._reconnector_task = asyncio.create_task(self._reconnect_loop())
        self._gcode_worker_task = asyncio.create_task(self._gcode_worker())

    async def stop(self):
        self._stop = True
        self._connected_event.clear()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

        for task in (self._ws_rx_task, self._gcode_worker_task, self._reconnector_task):
            if task:
                task.cancel()
                try:
                    await task
                except Exception:
                    pass

        for _, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(RPCError("Moonraker client stopped"))
        self._pending.clear()

    async def enqueue_gcode(self, origin: Any, gcode: str):
        await self._gcode_queue.put((origin, gcode))

    async def _next_id(self) -> int:
        async with self._id_lock:
            rid = self._id_counter
            self._id_counter += 1
            return rid

    async def send_rpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if not self.ws:
            raise RPCError("Moonraker WS not connected")

        req_id = await self._next_id()
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[req_id] = fut

        msg = {"jsonrpc": "2.0", "method": method, "id": req_id}
        if params is not None:
            msg["params"] = params

        try:
            await self.ws.send(json.dumps(msg))
        except Exception as e:
            self._pending.pop(req_id, None)
            raise RPCError(f"WS send failed: {e}") from e

        try:
            return await asyncio.wait_for(fut, timeout=RPC_TIMEOUT_S)
        except asyncio.TimeoutError as e:
            self._pending.pop(req_id, None)
            raise RPCError(f"RPC timeout: {method}") from e
        except Exception:
            self._pending.pop(req_id, None)
            raise

    async def _identify_and_wait_ready(self):
        params: Dict[str, Any] = {
            "client_name": CLIENT_NAME,
            "version": CLIENT_VERSION,
            "type": CLIENT_TYPE,
            "url": "http://localhost/"
        }
        if MOONRAKER_TOKEN:
            params["access_token"] = MOONRAKER_TOKEN
        if MOONRAKER_API_KEY:
            params["api_key"] = MOONRAKER_API_KEY

        await self.send_rpc("server.connection.identify", params)

        while not self._stop:
            info = await self.send_rpc("server.info")
            state = info.get("klippy_state")
            if state == "ready":
                return
            if state in ("shutdown", "error"):
                raise RPCError(f"Klippy bad state: {state}")
            logger.info(f"[moonraker] klippy_state={state}, waiting...")
            await asyncio.sleep(1)

    async def _ws_receiver(self):
        assert self.ws is not None
        try:
            async for raw in self.ws:
                try:
                    data = json.loads(raw)
                except Exception:
                    continue

                msg_id = data.get("id")
                if msg_id is not None and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if fut.done():
                        continue
                    if "error" in data:
                        err = data["error"]
                        message = err.get("message", "Unknown error")
                        fut.set_exception(RPCError(message))
                    else:
                        fut.set_result(data.get("result"))
                    continue

                method = data.get("method")
                if method == "notify_gcode_response":
                    params = data.get("params") or []
                    for p in params:
                        line = str(p)
                        if FILTER_OK_IN_NOTIFY and line.strip().lower() == "ok":
                            continue
                        if self.on_gcode_response:
                            await self.on_gcode_response(line)

                elif method == "notify_klippy_disconnected":
                    if self.on_gcode_response:
                        await self.on_gcode_response("!! klippy disconnected")
                    raise RPCError("Klippy disconnected")

        except Exception as e:
            for _, fut in list(self._pending.items()):
                if not fut.done():
                    fut.set_exception(RPCError(f"WS receiver stopped: {e}"))
            self._pending.clear()
            raise

    async def _connect_once(self):
        self._connected_event.clear()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

        self.ws = await websockets.connect(MOONRAKER_WS, ping_interval=20, ping_timeout=30)
        logger.info("[moonraker] WS connected")

        self._ws_rx_task = asyncio.create_task(self._ws_receiver())
        await self._identify_and_wait_ready()
        self._connected_event.set()
        logger.info("[moonraker] READY")

    async def _reconnect_loop(self):
        delay = RECONNECT_BASE_S
        while not self._stop:
            try:
                await self._connect_once()
                assert self._ws_rx_task is not None
                await self._ws_rx_task
                raise RPCError("WS closed")
            except asyncio.CancelledError:
                return
            except Exception as e:
                self._connected_event.clear()
                logger.warning(f"[moonraker] disconnected: {e}. reconnect in {delay:.1f}s")
                await asyncio.sleep(delay)
                delay = min(delay * RECONNECT_FACTOR, RECONNECT_MAX_S)

    async def _gcode_worker(self):
        while not self._stop:
            origin, gcode = await self._gcode_queue.get()

            try:
                await self._connected_event.wait()
                await self.send_rpc("printer.gcode.script", {"script": gcode})

                # origin에 ack를 줄 수 있으면 준다 (웹은 JSON, TCP는 ok 라인)
                if hasattr(origin, "ack_ok"):
                    await origin.ack_ok()

            except Exception as e:
                if hasattr(origin, "ack_err"):
                    await origin.ack_err(str(e))


# =========================
# TCP clients (optional)
# =========================
class TcpClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, server: "BridgeServer"):
        self.reader = reader
        self.writer = writer
        self.server = server
        self.closed = False
        self.peer = writer.get_extra_info("peername")
        self._out: "asyncio.Queue[Optional[str]]" = asyncio.Queue()
        self._wt: Optional[asyncio.Task] = None

    async def start(self):
        self._wt = asyncio.create_task(self._writer_loop())

    async def close(self):
        if self.closed:
            return
        self.closed = True
        await self._out.put(None)
        if self._wt:
            self._wt.cancel()
            try:
                await self._wt
            except Exception:
                pass
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass

    async def send_line(self, line: str):
        if not self.closed:
            await self._out.put(line)

    async def _writer_loop(self):
        try:
            while not self.closed:
                item = await self._out.get()
                if item is None:
                    break
                self.writer.write((item + "\n").encode("utf-8", "ignore"))
                await self.writer.drain()
        except Exception:
            self.closed = True

    async def _auth(self) -> bool:
        if not BRIDGE_TOKEN:
            return True
        await self.send_line("AUTH REQUIRED")
        try:
            raw = await asyncio.wait_for(self.reader.readline(), timeout=5.0)
        except asyncio.TimeoutError:
            return False
        if not raw:
            return False
        line = raw.decode("utf-8", "ignore").strip()
        if not line.startswith("AUTH "):
            return False
        return line[5:].strip() == BRIDGE_TOKEN

    async def ack_ok(self):
        await self.send_line("ok")

    async def ack_err(self, msg: str):
        await self.send_line(f"!! error: {msg}")

    async def run(self):
        await self.start()
        logger.info(f"[tcp] connected: {self.peer}")

        if not await self._auth():
            await self.send_line("AUTH FAILED")
            await self.close()
            return
        await self.send_line("AUTH OK")
        await self.send_line("[*] READY (TCP)")

        try:
            while True:
                raw = await self.reader.readline()
                if not raw:
                    break
                g = normalize_gcode(raw.decode("utf-8", "ignore"))
                if not g:
                    continue
                await self.server.moonraker.enqueue_gcode(self, g)
        finally:
            await self.close()
            logger.info(f"[tcp] disconnected: {self.peer}")


# =========================
# Web console (HTTP + WS)
# =========================
WEB_HTML = r"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Moonraker Bridge Console</title>
  <style>
    body { font-family: sans-serif; margin: 16px; }
    #log { width: 100%; height: 60vh; white-space: pre; border: 1px solid #ccc; padding: 8px; overflow: auto; }
    #row { display: flex; gap: 8px; margin-top: 10px; }
    #cmd { flex: 1; }
    .muted { color: #666; font-size: 12px; margin-top: 6px; }
  </style>
</head>
<body>
  <h2>Moonraker Bridge Console</h2>
  <div id="log"></div>

  <div id="row">
    <input id="cmd" placeholder="input G-code" />
    <button id="send">Send</button>
  </div>

  <div class="muted">
    연결: <span id="status">disconnected</span>
  </div>

<script>
  const logEl = document.getElementById("log");
  const statusEl = document.getElementById("status");
  const cmdEl = document.getElementById("cmd");
  const sendBtn = document.getElementById("send");

  const wsUrl = (location.protocol === "https:" ? "wss://" : "ws://") + location.hostname + ":" + (location.port ? (parseInt(location.port)+1) : "8081") + "/ws";
  

  function append(line) {
    logEl.textContent += line + "\n";
    logEl.scrollTop = logEl.scrollHeight;
  }

  let ws;

  function connect() {
    statusEl.textContent = "connecting...";
    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      statusEl.textContent = "connected";
      append("[web] connected");
      // 토큰이 필요한 경우: prompt로 받거나 고정 값 사용
      // 아래는 prompt 방식 (원하면 제거)
      ws.send(JSON.stringify({type:"hello"}));
    };

    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "log") append(msg.line);
        else if (msg.type === "ack") append("[ack] ok");
        else if (msg.type === "err") append("[err] " + msg.message);
        else if (msg.type === "need_auth") {
          const token = prompt("Token required. Enter BRIDGE_TOKEN:");
          ws.send(JSON.stringify({type:"auth", token}));
        } else if (msg.type === "auth_ok") {
          append("[web] auth ok");
        } else if (msg.type === "auth_failed") {
          append("[web] auth failed");
        } else if (msg.type === "info") {
          append("[info] " + msg.message);
        }
      } catch (e) {
        append(ev.data);
      }
    };

    ws.onclose = () => {
      statusEl.textContent = "disconnected";
      append("[web] disconnected (reconnecting...)");
      setTimeout(connect, 1000);
    };

    ws.onerror = () => {
      statusEl.textContent = "error";
    };
  }

  sendBtn.onclick = () => {
    const line = cmdEl.value.trim();
    if (!line) return;
    ws.send(JSON.stringify({type:"send", line}));
    cmdEl.value = "";
    cmdEl.focus();
  };

  cmdEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendBtn.click();
  });

  connect();
</script>
</body>
</html>
"""

class WebWsClient:
    def __init__(self, ws: websockets.WebSocketServerProtocol, server: "BridgeServer"):
        self.ws = ws
        self.server = server
        self.authed = (BRIDGE_TOKEN is None)

    async def ack_ok(self):
        await self.ws.send(json.dumps({"type": "ack"}))

    async def ack_err(self, msg: str):
        await self.ws.send(json.dumps({"type": "err", "message": msg}))


# Minimal HTTP server for serving the HTML
async def http_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        data = await reader.read(4096)
        if not data:
            writer.close()
            return
        # naive parse: only serve GET /
        text = data.decode("utf-8", "ignore")
        if text.startswith("GET / "):
            body = WEB_HTML.encode("utf-8")
            resp = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("utf-8") + body
        else:
            body = b"Not Found"
            resp = (
                "HTTP/1.1 404 Not Found\r\n"
                "Content-Type: text/plain\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("utf-8") + body

        writer.write(resp)
        await writer.drain()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# =========================
# Bridge server
# =========================
class BridgeServer:
    def __init__(self):
        self.moonraker = MoonrakerClient()
        self.moonraker.on_gcode_response = self.broadcast_log

        self.tcp_clients: Set[TcpClient] = set()
        self.web_clients: Set[websockets.WebSocketServerProtocol] = set()
        self._lock = asyncio.Lock()

    async def start(self):
        await self.moonraker.start()

    async def stop(self):
        async with self._lock:
            for c in list(self.tcp_clients):
                await c.close()
            self.tcp_clients.clear()

            for w in list(self.web_clients):
                try:
                    await w.close()
                except Exception:
                    pass
            self.web_clients.clear()

        await self.moonraker.stop()

    async def broadcast_log(self, line: str):
        async with self._lock:
            # TCP clients
            dead_tcp = []
            for c in self.tcp_clients:
                if c.closed:
                    dead_tcp.append(c)
                else:
                    await c.send_line(line)
            for c in dead_tcp:
                self.tcp_clients.discard(c)

            # Web clients
            dead_ws = []
            for w in self.web_clients:
                try:
                    await w.send(json.dumps({"type": "log", "line": line}))
                except Exception:
                    dead_ws.append(w)
            for w in dead_ws:
                self.web_clients.discard(w)

    async def handle_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        client = TcpClient(reader, writer, self)
        async with self._lock:
            self.tcp_clients.add(client)
        try:
            await client.run()
        finally:
            async with self._lock:
                self.tcp_clients.discard(client)

    async def handle_websocket(self, ws: websockets.WebSocketServerProtocol):
        # register ws
        async with self._lock:
            self.web_clients.add(ws)

        web_client = WebWsClient(ws, self)

        try:
            # auth flow for web
            if BRIDGE_TOKEN is not None:
                await ws.send(json.dumps({"type": "need_auth"}))

            async for msg in ws:
                try:
                    data = json.loads(msg)
                except Exception:
                    await ws.send(json.dumps({"type": "err", "message": "Invalid JSON"}))
                    continue

                mtype = data.get("type")

                if mtype == "hello":
                    await ws.send(json.dumps({"type": "info", "message": "hello"}))

                elif mtype == "auth":
                    token = (data.get("token") or "").strip()
                    if token == BRIDGE_TOKEN:
                        web_client.authed = True
                        await ws.send(json.dumps({"type": "auth_ok"}))
                    else:
                        await ws.send(json.dumps({"type": "auth_failed"}))

                elif mtype == "send":
                    if not web_client.authed:
                        await ws.send(json.dumps({"type": "err", "message": "Not authenticated"}))
                        continue
                    line = str(data.get("line") or "")
                    g = normalize_gcode(line)
                    if not g:
                        continue
                    logger.info(f"[web] -> {g}")
                    await self.moonraker.enqueue_gcode(web_client, g)

                else:
                    await ws.send(json.dumps({"type": "err", "message": f"Unknown type: {mtype}"}))

        finally:
            async with self._lock:
                self.web_clients.discard(ws)


# =========================
# Entrypoint
# =========================
import contextlib

async def main():
    bridge = BridgeServer()
    await bridge.start()

    tcp_server = await asyncio.start_server(bridge.handle_tcp, TCP_HOST, TCP_PORT)
    http_server = await asyncio.start_server(http_handler, WEB_HOST, WEB_HTTP_PORT)

    ws_server = await websockets.serve(
        lambda ws: bridge.handle_websocket(ws),
        WEB_HOST,
        WEB_WS_PORT,
        ping_interval=20,
        ping_timeout=30,
    )

    logger.info(f"[*] TCP bridge  : {TCP_HOST}:{TCP_PORT}")
    logger.info(f"[*] Web HTTP   : http://{WEB_HOST}:{WEB_HTTP_PORT}/")
    logger.info(f"[*] Web WS     : ws://{WEB_HOST}:{WEB_WS_PORT}/ws")
    logger.info(f"[*] Moonraker WS (single shared): {MOONRAKER_WS}")
    if BRIDGE_TOKEN:
        logger.info("[*] BRIDGE_TOKEN enabled (TCP + Web auth)")
    if FILTER_OK_IN_NOTIFY:
        logger.info("[*] Filtering 'ok' in notify_gcode_response")

    try:
        await asyncio.gather(
            tcp_server.serve_forever(),
            http_server.serve_forever(),
        )
    finally:
        ws_server.close()
        with contextlib.suppress(Exception):
            await ws_server.wait_closed()
        tcp_server.close()
        http_server.close()
        with contextlib.suppress(Exception):
            await tcp_server.wait_closed()
        with contextlib.suppress(Exception):
            await http_server.wait_closed()
        await bridge.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("shutdown")
