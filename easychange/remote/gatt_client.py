"""EasyChange native Windows BLE GATT client; no remote MCP or network listener.

Runs entirely in a dedicated background asyncio thread. Communication happens
via the existing bonded Bluetooth keyboard/mouse ESP32 plus its new custom
encrypted characteristic. Qt command dispatch remains in the GUI thread.
"""
from __future__ import annotations
import asyncio
import base64
import json
import os
import queue
import re
import threading

from .gatt_security import GattHandshake, GattSecureSession
from .gatt_wire import FramedMessages, frame_object, fragments, seal_json, open_sealed

SERVICE_UUID = "c24c1e20-9b28-4e07-9c29-20167e20a4e1"
HOST_TO_REMOTE_UUID = "c24c1e20-9b28-4e07-9c29-20167e20a4e2"
REMOTE_TO_HOST_UUID = "c24c1e20-9b28-4e07-9c29-20167e20a4e3"
DEVICE_NAME = "BT KeyboardMouse E"
SEQUENCE_RE = re.compile(r"Q[A-Z0-9_-]{4,40}\Z")


class EasyChangeGattClient:
    def __init__(self) -> None:
        self.events = queue.Queue(maxsize=256)
        self.commands = queue.Queue(maxsize=128)
        self._stop = threading.Event()
        self._loop = None
        self._thread = None
        self._client = None
        self._reader = FramedMessages()
        self._handshake = None
        self._session = None
        self._accepted = False
        self._sending = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._thread_main, daemon=True, name="easychange-native-gatt")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(lambda: None)

    def status(self) -> dict:
        return {"running": self._thread is not None and self._thread.is_alive(),
                "secure": self._accepted, "bluetooth_connected": bool(
                    self._client and self._client.is_connected)}

    def _emit(self, event: dict) -> None:
        try:
            self.events.put_nowait(event)
        except queue.Full:
            pass

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run())
        finally:
            self._loop = None
            loop.close()

    async def _run(self) -> None:
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError:
            self._emit({"type":"status","message":"GATT indisponivel: instale o extra GUI (bleak)."})
            return
        while not self._stop.is_set():
            self._session = None
            self._handshake = None
            self._accepted = False
            self._reader.reset()
            try:
                address = os.getenv("EASYCHANGE_BLE_DEVICE_ADDRESS", "").strip()
                if not address:
                    results = await BleakScanner.discover(timeout=4)
                    matches = [item for item in results if item.name == DEVICE_NAME]
                    if not matches:
                        raise RuntimeError("ESP_BLE_NOT_DISCOVERABLE_SET_DEVICE_ADDRESS")
                    target = matches[0]
                else:
                    target = address
                async with BleakClient(target, timeout=12) as client:
                    self._client = client
                    self._sending = asyncio.Lock()
                    await client.start_notify(HOST_TO_REMOTE_UUID, self._notify)
                    self._emit({"type":"status","message":"Bluetooth GATT conectado; aguardando pareamento seguro."})
                    while client.is_connected and not self._stop.is_set():
                        await asyncio.sleep(.15)
            except Exception as exc:
                self._emit({"type":"status","message":f"GATT aguardando conexao: {type(exc).__name__}: {str(exc)[:90]}"})
            finally:
                self._accepted = False
                self._session = None
                self._client = None
                self._emit({"type":"status","message":"GATT desconectado; QR permanece disponivel."})
            if not self._stop.is_set():
                await asyncio.sleep(3)

    def _notify(self, sender, data) -> None:
        try:
            messages = self._reader.feed(bytes(data))
            for message in messages:
                asyncio.create_task(self._message(message))
        except Exception:
            self._reader.reset()
            self._accepted = False
            self._emit({"type":"status","message":"Erro de integridade na montagem GATT."})

    async def _send(self, message: dict) -> None:
        client = self._client
        if not client or not client.is_connected or self._sending is None:
            raise RuntimeError("GATT_REMOTE_DISCONNECTED")
        payload = frame_object(message)
        async with self._sending:
            peer_mtu = max(23, int(getattr(client, "mtu_size", 23)))
            chunk_bytes = min(160, peer_mtu - 3)
            for piece in fragments(payload, chunk_bytes):
                await client.write_gatt_char(REMOTE_TO_HOST_UUID, piece, response=True)

    async def _message(self, message: dict) -> None:
        if message.get("t") == "hello":
            try:
                peer = base64.b64decode(str(message.get("pk", "")), validate=True)
                handshake = GattHandshake("remote")
                code = handshake.receive_peer(peer)
                self._handshake = handshake
                self._session = handshake.approve(code)
                self._accepted = False
                await self._send({"t":"hello","pk":base64.b64encode(handshake.public_key).decode("ascii")})
                self._emit({"type":"pair","sas":code})
            except Exception:
                self._accepted = False
                self._emit({"type":"status","message":"Falha ao negociar chaves GATT."})
            return
        if message.get("t") != "sealed" or self._session is None:
            return
        try:
            command = open_sealed(self._session, message)
        except Exception:
            self._accepted = False
            self._emit({"type":"status","message":"Pacote GATT rejeitado: autenticacao falhou."})
            return
        if command.get("t") == "confirm":
            # A host-side serial/notify ACK does not prove the application
            # received confirmation. Return a session-sealed receipt before
            # admitting no-focus source commands on this transport.
            try:
                await self._send(seal_json(self._session, {"t": "confirmed"}))
            except Exception:
                self._accepted = False
                self._emit({"type":"status","message":"GATT: falha ao confirmar pareamento ao MCP."})
                return
            self._accepted = True
            self._emit({"type":"status","message":"GATT seguro autenticado. EasyChange executa sem roubar foco."})
            return
        if not self._accepted or command.get("t") != "exec":
            return
        sequence = command.get("sequence")
        text = command.get("command")
        if (not isinstance(sequence,str) or not SEQUENCE_RE.fullmatch(sequence)
                or not isinstance(text,str) or not text or len(text) > 1000000):
            return
        try:
            self.commands.put_nowait((sequence, f":ec {sequence} {text}"))
        except queue.Full:
            await self.send_result(sequence, {
                "ok": False, "command": "ec", "code": "COMMAND_QUEUE_FULL",
                "error": "GATT_COMMAND_QUEUE_FULL", "sequence": sequence,
            })

    async def send_result(self, sequence: str, result: dict) -> None:
        if not self._accepted or self._session is None:
            return
        await self._send(seal_json(self._session,
            {"t":"result","sequence":sequence,"result":result}))

    def publish_result(self, sequence: str, result: dict) -> None:
        if not self._accepted or self._loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(
            self.send_result(sequence, result), self._loop)
        def verify_delivery(done):
            try:
                done.result()
            except Exception as exc:
                # Do not leave the GUI claiming a healthy secure result channel
                # after the Windows GATT write has failed. The MCP caller must
                # reconcile its original sequence, never replay a mutation.
                self._accepted = False
                self._emit({"type": "status",
                            "message": "Falha ao enviar resultado GATT: " +
                                       type(exc).__name__})
        future.add_done_callback(verify_delivery)
