"""Bonjour / mDNS advertisement of the LAN bridge (`_kairos._tcp`).

The pairing QR carries the desktop's IP address, and a DHCP renewal or a router reboot changes it.
Without discovery the phone kept dialling the old address and stayed "Offline" until the user
scanned the QR again. The bridge now announces itself on the local network; the iPhone browses
`_kairos._tcp`, finds the service whose TXT record carries the certificate fingerprint it paired
with, and reconnects on its own. Nothing secret is announced: the fingerprint is public (it is
the hash of the certificate the server shows to anybody), and the phone still needs its bearer
token and the pinned certificate to talk to us.

Best effort by design: no `zeroconf` module, no multicast-capable adapter or a firewall that
drops 5353 only means the phone falls back to the QR address, exactly as before.
"""
from __future__ import annotations

import re
import socket
import threading

SERVICE_TYPE = "_kairos._tcp.local."
RECHECK_S = 15.0                    # how often the advertised addresses are compared with reality


def _label(text: str, fallback: str = "Kairos") -> str:
    """A DNS-SD instance label: printable, <= 63 bytes, no dots."""
    t = re.sub(r"[\x00-\x1f.]", " ", str(text or "")).strip() or fallback
    return t.encode("utf-8")[:60].decode("utf-8", "ignore")


def _host_label(text: str) -> str:
    t = re.sub(r"[^A-Za-z0-9-]+", "-", str(text or "")).strip("-")[:40]
    return t or "kairos-desktop"


def local_addresses() -> list[str]:
    """IPv4 addresses worth announcing: the one the default route uses first, then the others."""
    out: list[str] = []

    def add(ip):
        if ip and not ip.startswith(("127.", "169.254.", "0.")) and ip not in out:
            out.append(ip)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))            # no packet is sent: this only picks the route
        add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    return out


def txt_record(fingerprint: str, library_id: str, name: str, protocol: int) -> dict[str, str]:
    """TXT keys (each key=value pair must stay under 255 bytes): fp = SHA-256 of the DER
    certificate (what the QR carries), lib = library id, v = protocol version."""
    return {"fp": str(fingerprint or "").lower(), "lib": str(library_id or ""), "v": str(protocol),
            "name": _label(name)}


def build_info(port: int, addresses: list[str], fingerprint: str, library_id: str, name: str, protocol: int):
    """The zeroconf ServiceInfo for this desktop (None when no address is known)."""
    from zeroconf import ServiceInfo
    if not addresses:
        return None
    label = _label(name)
    return ServiceInfo(
        SERVICE_TYPE,
        f"{label}.{SERVICE_TYPE}",
        port=int(port),
        addresses=[socket.inet_aton(a) for a in addresses],
        properties=txt_record(fingerprint, library_id, name, protocol),
        server=f"{_host_label(socket.gethostname())}.local.",
    )


class Advertiser:
    """Registers the service, re-registers it when the machine's address changes, withdraws it on stop()."""

    def __init__(self, port, fingerprint, library_id, name, protocol=1):
        self.port, self.fingerprint, self.library_id = port, fingerprint, library_id
        self.name, self.protocol = name, protocol
        self.addresses: list[str] = []
        self.active = False
        self._zc = None
        self._info = None
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        try:
            from zeroconf import IPVersion, Zeroconf
        except Exception:                           # noqa: BLE001 - optional dependency
            return False
        with self._lock:
            if self._zc is not None:
                return True
            try:
                self._zc = Zeroconf(ip_version=IPVersion.V4Only)
                self._register(local_addresses())
            except Exception as e:                  # noqa: BLE001 - never take the bridge down
                print(f"[Sync] mDNS advertisement unavailable: {e}")
                self._close()
                return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, name="sync-mdns", daemon=True)
        self._thread.start()
        return True

    def _register(self, addrs):
        info = build_info(self.port, addrs, self.fingerprint, self.library_id, self.name, self.protocol)
        if info is None:
            self.active = False
            return
        if self._info is None:
            self._zc.register_service(info, allow_name_change=True)
        else:
            self._zc.update_service(info)           # same name: peers' caches refresh, no duplicate
        self._info, self.addresses, self.active = info, list(addrs), True

    def _watch(self):
        """A DHCP renewal moves the desktop to a new address while it runs: announce the new one."""
        while not self._stop.wait(RECHECK_S):
            try:
                addrs = local_addresses()
                with self._lock:
                    if self._zc is not None and addrs and addrs != self.addresses:
                        self._register(addrs)
            except Exception:                       # noqa: BLE001
                pass

    def _close(self):
        zc, info = self._zc, self._info
        self._zc = self._info = None
        self.active = False
        if zc is not None:
            try:
                if info is not None:
                    zc.unregister_service(info)     # goodbye packets: the phone drops us at once
            except Exception:                       # noqa: BLE001
                pass
            try:
                zc.close()
            except Exception:                       # noqa: BLE001
                pass

    def stop(self):
        self._stop.set()
        with self._lock:
            self._close()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2)
        self._thread = None
