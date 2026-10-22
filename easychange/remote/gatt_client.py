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
import sys
import threading
from importlib import metadata as importlib_metadata

from .gatt_diagnostics import GattDiagnosticLog

from .gatt_security import GattHandshake, GattSecureSession
from .gatt_wire import FramedMessages, frame_object, fragments, seal_json, open_sealed

SERVICE_UUID = "c24c1e20-9b28-4e07-9c29-20167e20a4e1"
HOST_TO_REMOTE_UUID = "c24c1e20-9b28-4e07-9c29-20167e20a4e2"
REMOTE_TO_HOST_UUID = "c24c1e20-9b28-4e07-9c29-20167e20a4e3"
DIAGNOSTIC_READ_UUID = "c24c1e20-9b28-4e07-9c29-20167e20a4e4"
DEVICE_NAME = "BT KeyboardMouse E"
SEQUENCE_RE = re.compile(r"Q[A-Z0-9_-]{4,40}\Z")


BLE_ADDRESS_RE = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\Z")


def _on_windows() -> bool:
    return sys.platform == "win32"


def _ble_address(value: int) -> str:
    """Convert WinRT uint64 address to Bleak 6-octet form."""
    if type(value) is not int or not 0 < value < (1 << 48):
        raise ValueError("ESP_BLE_WINDOWS_ADDRESS_INVALID")
    text = f"{value:012X}"
    return ":".join(text[i:i + 2] for i in range(0, 12, 2))


async def _paired_windows_device_infos(device_information, selector, kind=None,
                                       diagnostic=None):
    """Handle PyWinRT FindAllAsync overload differences safely.

    Some Windows Python WinRT bindings reject the AQS-only overload with
    'Invalid parameter count'. Try the documented AQS + property-list form,
    then the no-argument enumerator as a final compatibility fallback. An
    unfiltered list MUST be checked for pairing before selecting a device.
    """
    # Paired BluetoothLEDevice selectors describe ASSOCIATION_ENDPOINT
    # objects, not the default DEVICE_INTERFACE kind. Without this
    # explicit kind, Windows can return zero matches for an active HID bond.
    variants = []
    if kind is not None:
        # The WinRT projection accepts a nullable IIterable on some Windows
        # builds but rejects Python [] for the same signature. The explicit
        # AssociationEndpoint kind is vital: zero-arg enumeration can return
        # thousands of DEVICE INTERFACE child records with pairing=False.
        variants.extend((
            ((selector, [], kind), True, "AEP_LIST"),
            ((selector, None, kind), True, "AEP_NULL"),
            ((selector, (), kind), True, "AEP_TUPLE"),
        ))
    variants.extend((
        ((selector,), True, "SELECTOR_ONLY"),
        ((selector, []), True, "SELECTOR_LIST"),
        ((selector, None), True, "SELECTOR_NULL"),
        ((), False, "UNFILTERED"),
    ))
    empty = None
    for args, paired_filter_from_windows, label in variants:
        try:
            devices = await device_information.find_all_async(*args)
            count = len(devices)
            if count:
                if isinstance(diagnostic, dict):
                    diagnostic["source"] = label
                return devices, paired_filter_from_windows
            if empty is None:
                empty = (devices, paired_filter_from_windows, label)
        except TypeError:
            # Only this read-only WinRT overload is retried.
            continue
    if empty is not None:
        devices, is_filtered, label = empty
        if isinstance(diagnostic, dict):
            diagnostic["source"] = label
        return devices, is_filtered
    raise RuntimeError("WINRT_FIND_ALL_ASYNC_OVERLOADS_UNSUPPORTED")


async def _paired_windows_ble_target(diagnostic=None):
    """Find an already paired HID device even when it stops advertising."""
    if not _on_windows():
        return None
    from bleak.backends.device import BLEDevice
    from winrt.windows.devices.bluetooth import BluetoothLEDevice
    from winrt.windows.devices.enumeration import DeviceInformation
    from winrt.windows.devices import enumeration as enumeration_module

    # Some WinRT projections reject selectors or overloads at runtime.
    # Never let an optional paired-device lookup prevent ordinary BLE scan.
    try:
        selector = BluetoothLEDevice.get_device_selector_from_pairing_state(True)
        selector_is_paired = True
    except TypeError:
        selector = BluetoothLEDevice.get_device_selector()
        selector_is_paired = False
    kind_type = getattr(enumeration_module, "DeviceInformationKind", None)
    endpoint_kind = getattr(kind_type, "ASSOCIATION_ENDPOINT", None)
    items, paired_filter_from_windows = await _paired_windows_device_infos(
        DeviceInformation, selector, kind=endpoint_kind, diagnostic=diagnostic)
    paired_filter_from_windows = paired_filter_from_windows and selector_is_paired
    from_aep = bool(isinstance(diagnostic, dict) and
                    str(diagnostic.get("source", "")).startswith("AEP_"))
    named = []
    if isinstance(diagnostic, dict):
        diagnostic["paired_records"] = min(9999, len(items))
        diagnostic["named_records"] = 0
        diagnostic["matching_records"] = 0
        diagnostic["verified_le_objects"] = 0
        diagnostic["verified_aep_addresses"] = 0
    for item in items:
        if str(getattr(item, "name", "")).strip().casefold() != DEVICE_NAME.casefold():
            continue
        named.append(item)
    if isinstance(diagnostic, dict):
        diagnostic["named_records"] = len(named)
    # DeviceInformation child-interface entries may have pairing=False even
    # when the actual BluetoothLEDevice is paired. Verify the resolved LE
    # object against its OWN DeviceInformation pairing state. Never trust
    # the name, link status, or a child-interface record alone.
    addresses = set()
    for item in named[:32]:
        pairing = getattr(item, "pairing", None)
        record_paired = getattr(pairing, "is_paired", False) is True
        if paired_filter_from_windows and pairing is None:
            # AQS paired-only filtered results may omit pairing property.
            record_paired = True
        if from_aep and record_paired:
            # Use Windows' own address for a confirmed PAIRED association
            # endpoint, avoiding another WinRT FromIdAsync from the worker
            # thread (some Windows builds require a UI dispatcher).
            try:
                address_value = item.properties["System.Devices.Aep.DeviceAddress"]
            except (KeyError, AttributeError, TypeError, OSError):
                address_value = None
            if isinstance(address_value, str) and BLE_ADDRESS_RE.fullmatch(
                    address_value.strip()):
                addresses.add(address_value.strip().upper())
                if isinstance(diagnostic, dict):
                    diagnostic["verified_aep_addresses"] += 1
                continue
        try:
            obj = await BluetoothLEDevice.from_id_async(item.id)
        except (OSError, ValueError, RuntimeError, TypeError):
            # A default WinRT device-interface ID need not be a valid
            # BluetoothLEDevice endpoint ID. Never treat that as consent
            # to connect based on the child interface's display name.
            continue
        if obj is None:
            continue
        try:
            info = getattr(obj, "device_information", None)
            device_pairing = getattr(getattr(info, "pairing", None),
                                     "is_paired", None)
            verified_paired = (device_pairing is True or
                               (device_pairing is None and record_paired))
            if not verified_paired:
                continue
            if str(getattr(obj, "name", DEVICE_NAME)).strip().casefold() != DEVICE_NAME.casefold():
                continue
            if isinstance(diagnostic, dict):
                diagnostic["verified_le_objects"] += 1
            addresses.add(_ble_address(int(obj.bluetooth_address)))
        finally:
            obj.close()
    if isinstance(diagnostic, dict):
        diagnostic["matching_records"] = len(addresses)
    if len(addresses) > 1:
        raise RuntimeError("ESP_BLE_PAIRED_MULTIPLE_SAME_NAME")
    if not addresses:
        return None
    return BLEDevice(next(iter(addresses)), DEVICE_NAME, None)


def _gatt_attribute_details(services) -> str:
    """Return metadata-only inventory for all expected service attributes."""
    lines = []
    for label, uuid in (("SERVICE", SERVICE_UUID),
                        ("RX_NOTIFY", HOST_TO_REMOTE_UUID),
                        ("TX_WRITE", REMOTE_TO_HOST_UUID)):
        try:
            obj = (services.get_service(uuid) if label == "SERVICE"
                   else services.get_characteristic(uuid))
            if obj is None:
                lines.append(f"{label}: NOT_FOUND uuid={uuid}")
                continue
            handle = getattr(obj, "handle", "?")
            if label == "SERVICE":
                lines.append(f"SERVICE: FOUND uuid={uuid} handle={handle}")
                continue
            properties = [str(p) for p in (getattr(obj, "properties", ()) or ())][:16]
            lines.append(
                f"{label}: FOUND uuid={uuid} handle={handle}"
                f" properties={','.join(properties)}")
            for desc in (getattr(obj, "descriptors", ()) or ())[:24]:
                lines.append(f"  DESCRIPTOR uuid={str(getattr(desc, 'uuid', '?'))[:60]}"
                             f" handle={getattr(desc, 'handle', '?')}")
        except (TypeError, ValueError, RuntimeError, AttributeError, KeyError) as exc:
            lines.append(f"{label}: INSPECTION_FAILED {type(exc).__name__}")
    return "\n".join(lines)


def _is_cccd_write_denied(exc: BaseException) -> bool:
    """ATT 0x03 differs from pairing/authentication errors (0x05, 0x0F)."""
    value = getattr(exc, "protocol_error", None)
    if value is not None:
        try:
            return int(value) == 3
        except (ValueError, TypeError):
            return False
    message = str(exc).lower()
    return ("write not permitted" in message and
            ("gatt" in message or "protocol error" in message or
             "0x03" in message or "(3," in message))


async def _custom_service_read_probe(client) -> str:
    """Optional, safe read of public diagnostic marker (never commands or keys).

    Distinguishes custom-service read access from CCCD subscription access.
    Firmware before the read-only diagnostic characteristic reports ABSENT.
    """
    services = getattr(client, "services", None)
    if services is None:
        return "DIAG_SERVICES_UNAVAILABLE"
    try:
        char = services.get_characteristic(DIAGNOSTIC_READ_UUID)
        if char is None:
            return "DIAG_CHARACTERISTIC_ABSENT"
        properties = {str(p).lower() for p in (getattr(char, "properties", ()) or ())}
        if "read" not in properties:
            return "DIAG_CHARACTERISTIC_NOT_READABLE"
        raw = bytes(await asyncio.wait_for(
            client.read_gatt_char(char, use_cached=False), timeout=4))
        # Do not expose peripheral data in log; compare against one fixed
        # public protocol marker only.
        return ("DIAG_READ_OK" if raw == b"BMO-GATT-DIAG-1"
                else "DIAG_READ_UNEXPECTED_LENGTH:" + str(len(raw)))
    except Exception as exc:
        return "DIAG_READ_FAILED:" + type(exc).__name__ + ":" + str(exc)[:110]


async def _cccd_readonly_probe(client) -> str:
    """Inspect CCCD (0x2902) using read only; never write or alter BLE bonds."""
    services = getattr(client, "services", None)
    if services is None:
        return "CCCD_SERVICES_UNAVAILABLE"
    try:
        char = services.get_characteristic(HOST_TO_REMOTE_UUID)
        if char is None:
            return "CCCD_NOTIFY_CHARACTERISTIC_MISSING"
        descriptors = list(getattr(char, "descriptors", ()) or ())[:32]
        cccd = next((desc for desc in descriptors
                     if str(getattr(desc, "uuid", "")).lower() in
                     ("00002902-0000-1000-8000-00805f9b34fb", "2902", "0x2902")), None)
        if cccd is None:
            return "CCCD_DESCRIPTOR_MISSING"
        value = bytes(await asyncio.wait_for(
            client.read_gatt_descriptor(cccd, use_cached=False), timeout=4))
        if len(value) != 2:
            return "CCCD_READ_LENGTH_INVALID"
        return "CCCD_READ_OK:0x" + value.hex()
    except Exception as exc:
        # Optional diagnostics must never mask the actual write-denied error.
        return "CCCD_READ_FAILED:" + type(exc).__name__ + ":" + str(exc)[:85]


def _gatt_notify_inventory(services) -> str:
    """Read-only GATT metadata; never issue diagnostic characteristic writes.

    ATT 0x03 from start_notify can mean an actual server CCCD permission
    denial or a stale Windows handle. Observe the expected service, notify
    property and CCCD without guessing the cause or weakening link security.
    """
    if services is None:
        return "gatt=unavailable"
    try:
        service = services.get_service(SERVICE_UUID)
        characteristic = services.get_characteristic(HOST_TO_REMOTE_UUID)
        if characteristic is None:
            return f"service={int(service is not None)},notify_char=0,cccd=0"
        props = [str(value).lower() for value in (getattr(
            characteristic, "properties", ()) or ())][:16]
        descriptors = list(getattr(characteristic, "descriptors", ()) or ())[:32]
        cccd = any(str(getattr(desc, "uuid", "")).lower() in
                   ("00002902-0000-1000-8000-00805f9b34fb", "2902", "0x2902")
                   for desc in descriptors)
        handle = getattr(characteristic, "handle", None)
        handle_info = f",handle={int(handle)}" if type(handle) is int else ""
        return (f"service={int(service is not None)},"
                f"notify_char=1,notify={int('notify' in props)},"
                f"cccd={int(cccd)}" + handle_info)
    except (TypeError, ValueError, AttributeError, KeyError, RuntimeError) as exc:
        return "gatt=metadata_unavailable_" + type(exc).__name__


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
        self._last_error = ""
        self._last_stage = "IDLE"
        self.diagnostics = GattDiagnosticLog()
        self._attempt = 0
        self._consecutive_cccd_denials = 0
        self._retry_wakeup = None
        self.diagnostics.record(
            "INICIO", "Iniciando diagnostico GATT no EasyChange. "
            "Comandos, chaves, endereco BLE, mensagens cifradas e codigo SAS "
            "nao sao coletados.")
        try:
            self.diagnostics.record(
                "AMBIENTE", f"Python={sys.version.split()[0]}, "
                f"OS={'Windows' if _on_windows() else sys.platform}, "
                f"Bleak={importlib_metadata.version('bleak')}")
        except (importlib_metadata.PackageNotFoundError, ValueError):
            self.diagnostics.record("AMBIENTE", "Versao Bleak indisponivel")

    def _trace(self, stage: str, message: str, exc=None) -> None:
        self.diagnostics.record(stage, message, exc=exc)

    def retry_now(self) -> bool:
        """User initiated non-destructive retry; no unpair or admin operations."""
        loop, signal = self._loop, self._retry_wakeup
        if loop is None or not loop.is_running() or signal is None:
            return False
        self._trace("RECONEXAO_MANUAL", "Operador solicitou nova tentativa BLE; "
                    "nenhum pareamento ou configuracao Windows alterado")
        loop.call_soon_threadsafe(signal.set)
        return True

    async def _retry_delay(self, seconds: float | None) -> None:
        signal = self._retry_wakeup
        if signal is None:
            if seconds is not None:
                await asyncio.sleep(seconds)
            return
        try:
            if seconds is None:
                await signal.wait()
            else:
                await asyncio.wait_for(signal.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        finally:
            signal.clear()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._thread_main, daemon=True, name="easychange-native-gatt")
        self._thread.start()
        self._trace("INICIO", "Worker Bluetooth GATT iniciado")

    def stop(self) -> None:
        self._trace("ENCERRAMENTO", "Encerramento solicitado pelo EasyChange")
        self._stop.set()
        loop = self._loop
        if loop and loop.is_running():
            signal = self._retry_wakeup
            loop.call_soon_threadsafe(signal.set if signal is not None else lambda: None)

    def status(self) -> dict:
        return {"running": self._thread is not None and self._thread.is_alive(),
                "secure": self._accepted, "bluetooth_connected": bool(
                    self._client and self._client.is_connected),
                "last_stage": self._last_stage, "last_error": self._last_error}

    async def _resolve_target(self, scanner):
        """Use the paired Windows BLE HID record when advertisements have stopped."""
        address = os.getenv("EASYCHANGE_BLE_DEVICE_ADDRESS", "").strip()
        if address:
            self._trace("DESCOBERTA", "Endereco BLE explicito configurado (valor ocultado)")
            if not BLE_ADDRESS_RE.fullmatch(address):
                raise ValueError("EASYCHANGE_BLE_DEVICE_ADDRESS_INVALID")
            if _on_windows():
                from bleak.backends.device import BLEDevice
                return BLEDevice(address.upper(), DEVICE_NAME, None)
            return address
        discovery = {}
        if _on_windows():
            try:
                target = await _paired_windows_ble_target(discovery)
            except (ImportError, OSError) as exc:
                self._last_error = "PAIRED_LOOKUP_UNAVAILABLE: " + type(exc).__name__
                target = None
            except TypeError:
                self._last_error = "PAIRED_LOOKUP_WINRT_OVERLOAD_UNSUPPORTED"
                target = None
            except RuntimeError as exc:
                if str(exc) != "WINRT_FIND_ALL_ASYNC_OVERLOADS_UNSUPPORTED":
                    raise
                self._last_error = "PAIRED_LOOKUP_WINRT_OVERLOAD_UNSUPPORTED"
                target = None
            if target is not None:
                self._trace("DESCOBERTA", "Dispositivo BLE associado e validado "
                            f"via WinRT; fonte={discovery.get('source', '?')}; "
                            f"registros={discovery.get('paired_records', '?')}; "
                            f"nome={discovery.get('named_records', '?')}; "
                            f"verificados={discovery.get('matching_records', '?')}")
                return target
            self._trace("DESCOBERTA", "WinRT nao retornou endereco BLE confirmado; "
                        f"fonte={discovery.get('source', '?')}; "
                        f"registros={discovery.get('paired_records', '?')}; "
                        f"nome={discovery.get('named_records', '?')}; "
                        f"verificados={discovery.get('matching_records', '?')}. "
                        "Tentando anuncios BLE.")
        results = await scanner.discover(timeout=4)
        self._trace("DESCOBERTA", f"Scanner Bluetooth: anuncios={len(results)}, "
                    f"correspondencias={sum(1 for item in results if item.name == DEVICE_NAME)}")
        matches = [item for item in results if item.name == DEVICE_NAME]
        if len(matches) > 1:
            raise RuntimeError("ESP_BLE_ADVERTISING_MULTIPLE_SAME_NAME")
        if not matches:
            stats = (f"records={discovery.get('paired_records', '?')}, "
                     f"named={discovery.get('named_records', '?')}, "
                     f"verified={discovery.get('matching_records', '?')}, "
                     f"le={discovery.get('verified_le_objects', '?')}, "
                     f"aep={discovery.get('verified_aep_addresses', '?')}, "
                     f"source={discovery.get('source', '?')}, scan={len(results)}")
            raise RuntimeError("ESP_BLE_NOT_DISCOVERABLE_OR_NOT_PAIRED [" + stats + "]")
        return matches[0]

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
        except ImportError as exc:
            self._trace("FALHA", "Biblioteca Bleak nao instalada", exc=exc)
            self._emit({"type":"status","message":"GATT indisponivel: instale o extra GUI (bleak)."})
            return
        self._retry_wakeup = asyncio.Event()
        while not self._stop.is_set():
            self._session = None
            self._handshake = None
            self._accepted = False
            self._reader.reset()
            stage = "BUSCA_BLE"
            failure = ""
            gatt_metadata = ""
            cccd_state = "CCCD_READ_NOT_ATTEMPTED"
            self._last_stage = stage
            cccd_denied = False
            self._attempt += 1
            self._trace("BUSCA_BLE", f"Tentativa #{self._attempt}: iniciando descoberta do "
                        "ESP Bluetooth HID emparelhado")
            try:
                target = await self._resolve_target(BleakScanner)
                stage = "CONEXAO_BLE"
                self._last_stage = stage
                options = {"winrt": {"use_cached_services": False}} if _on_windows() else {}
                self._trace("CONEXAO_BLE", "Dispositivo resolvido; abrindo conexao "
                            f"com timeout=12s, cache_servicos_desabilitado={_on_windows()}")
                # This firmware adds a custom GATT service to an old HID bond.
                # Windows must read its CURRENT attribute DB, not a cached one.
                async with BleakClient(target, timeout=12, **options) as client:
                    self._client = client
                    self._sending = asyncio.Lock()
                    self._trace("CONEXAO_BLE", "Conexao Bluetooth estabelecida. "
                                f"is_connected={bool(client.is_connected)}; "
                                f"MTU={getattr(client, 'mtu_size', '?')}")
                    stage = "INSCRICAO_GATT"
                    self._last_stage = stage
                    gatt_metadata = _gatt_notify_inventory(
                        getattr(client, "services", None))
                    self._trace("INVENTARIO_GATT",
                                _gatt_attribute_details(getattr(client, "services", None))
                                if getattr(client, "services", None) is not None
                                else "Servicos BLE indisponiveis")
                    # Readable characteristic is independent of CCCD (0x2902).
                    # Perform one bounded read while the same BLE link is open.
                    # Firmware without this optional characteristic is valid.
                    read_result = await _custom_service_read_probe(client)
                    self._trace("GATT_LEITURA_TESTE",
                                f"Servico personalizado, acesso somente leitura: {read_result}")
                    self._trace("INSCRICAO_GATT", "Solicitando notificacoes da "
                                f"caracteristica {HOST_TO_REMOTE_UUID}; {gatt_metadata}")
                    try:
                        await client.start_notify(HOST_TO_REMOTE_UUID, self._notify)
                    except Exception as notify_exc:
                        # Must inspect while BleakClient is STILL connected:
                        # outside the async-with, __aexit__ has disconnected
                        # and any CCCD read yields a misleading failure.
                        if _is_cccd_write_denied(notify_exc):
                            cccd_state = await _cccd_readonly_probe(client)
                            self._trace(
                                "CCCD_ATT03_PROBE",
                                "Leitura somente-leitura do descritor durante "
                                f"vinculo ativo: {cccd_state}; "
                                f"conectado={bool(client.is_connected)}")
                        raise
                    self._trace("INSCRICAO_GATT", "Inscricao confirmada pelo sistema")
                    self._consecutive_cccd_denials = 0
                    stage = "CONECTADO"
                    self._last_stage = stage
                    self._last_error = ""
                    self._trace("CONECTADO", "Canal GATT pronto; ainda exige "
                                "autenticacao de aplicacao SAS no MCP local")
                    self._emit({"type":"status","message":"Bluetooth GATT conectado; aguardando pareamento seguro."})
                    while client.is_connected and not self._stop.is_set():
                        await asyncio.sleep(.15)
                    if not self._stop.is_set():
                        failure = "BLE_LINK_DISCONNECTED"
            except Exception as exc:
                self._trace(stage, "Erro na tentativa Bluetooth/GATT "
                            f"#{self._attempt}; trace da excecao abaixo:", exc=exc)
                failure = f"{type(exc).__name__}: {str(exc)[:160]}"
                if stage == "INSCRICAO_GATT":
                    failure += " [" + gatt_metadata + "]"
                    if _is_cccd_write_denied(exc):
                        cccd_denied = True
                        self._consecutive_cccd_denials += 1
                        self._trace("CCCD_ATT03", "Recusa do servidor BLE ao "
                                    "habilitar notificacoes: ATT 0x03 Write Not Permitted; "
                                    f"recusas={self._consecutive_cccd_denials}; "
                                    f"leitura={cccd_state}. Nenhuma escrita "
                                    "direta no CCCD nem mudanca no Windows.")
                        failure = ("ATT_0x03 Write Not Permitted "
                                   f"(recusas={self._consecutive_cccd_denials}; "
                                   f"{cccd_state}) [{gatt_metadata}]")
            finally:
                self._accepted = False
                self._session = None
                self._client = None
                if not self._stop.is_set():
                    self._last_error = failure or "BLE_LINK_DISCONNECTED"
                    self._last_stage = stage
                    self._trace("DESCONECTADO", f"Tentativa #{self._attempt} encerrada. "
                                f"ultima_etapa={stage}; causa={self._last_error[:300]}; "
                                "HID/QR permanecem independentes da falha GATT")
                    # Do not overwrite the actual error with a generic
                    # 'disconnected' event on the following Qt timer tick.
                    self._emit({"type":"status","message":
                        f"GATT desconectado [{stage}]: {self._last_error[:300]}. "
                        "Tentando reconectar; QR disponivel."})
            if not self._stop.is_set():
                if cccd_denied and self._consecutive_cccd_denials >= 2:
                    self._last_stage = "CCCD_ATT03_ACTION_REQUIRED"
                    self._trace("CCCD_ATT03_ACTION_REQUIRED",
                                "Duas conexoes independentes tiveram a mesma recusa "
                                "ATT 0x03. Esperando nova tentativa do operador, "
                                "sem loop infinito; nao e sucesso GATT.")
                    self._emit({"type": "status", "message":
                        "GATT nao autenticado: duas recusas ATT 0x03 do CCCD. "
                        "Verifique o log. Clique 'Tentar GATT' para nova tentativa "
                        "sem administrador. HID permanece disponivel."})
                    await self._retry_delay(None)
                    self._consecutive_cccd_denials = 0
                else:
                    await self._retry_delay(3)

    def _notify(self, sender, data) -> None:
        try:
            messages = self._reader.feed(bytes(data))
            for message in messages:
                asyncio.create_task(self._message(message))
        except Exception as exc:
            self._trace("NOTIFICACAO", "Falha de integridade dos frames GATT", exc=exc)
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
                self._trace("RESULTADO_GATT", "Falha ao enviar resultado ao MCP", exc=exc)
                # Do not leave the GUI claiming a healthy secure result channel
                # after the Windows GATT write has failed. The MCP caller must
                # reconcile its original sequence, never replay a mutation.
                self._accepted = False
                self._emit({"type": "status",
                            "message": "Falha ao enviar resultado GATT: " +
                                       type(exc).__name__})
        future.add_done_callback(verify_delivery)
