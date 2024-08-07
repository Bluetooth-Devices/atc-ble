"""Parser for BLE advertisements for sensors with ATC firmware.

This file is shamelessly copied from the following repository:
https://github.com/Ernst79/bleparser/blob/ac8757ad64f1fc17674dcd22111e547cdf2f205b/package/bleparser/atc.py

MIT License applies.
"""

from __future__ import annotations

import logging
import sys
from struct import unpack
from typing import Any

from bluetooth_sensor_state_data import BluetoothData
from Cryptodome.Cipher import AES
from home_assistant_bluetooth import BluetoothServiceInfo
from sensor_state_data import SensorLibrary

_LOGGER = logging.getLogger(__name__)


def short_address(address: str) -> str:
    """Convert a Bluetooth address to a short address"""
    results = address.replace("-", ":").split(":")
    if len(results[-1]) == 2:
        return f"{results[-2].upper()}{results[-1].upper()}"
    return results[-1].upper()


def to_mac(addr: bytes) -> str:
    """Return formatted MAC address"""
    return ":".join(f"{i:02X}" for i in addr)


class ATCBluetoothDeviceData(BluetoothData):
    """Date for ATC Bluetooth devices."""

    def __init__(self, bindkey: bytes | None = None) -> None:
        super().__init__()
        self.bindkey = bindkey

        # Data that we know how to parse but don't yet map to the SensorData model.
        self.unhandled: dict[str, Any] = {}

        # If true, the data is encrypted
        self.is_encrypted = False

        # If true, then we know the actual MAC of the device.
        # On macOS, we don't unless the device includes it in the advertisement
        # (CoreBluetooth uses UUID's generated by CoreBluetooth instead of the MAC)
        self.mac_known = sys.platform != "darwin"

        # If true then we have used the provided encryption key to decrypt at least
        # one payload.
        # If false then we have either not seen an encrypted payload, the key is wrong
        # or encryption is not in use
        self.bindkey_verified = False

        # If this is True, then we have not seen an advertisement with a payload
        # Until we see a payload, we can't tell if this device is encrypted or not
        self.pending = True

        # The last service_info we saw that had a payload
        # We keep this to help in reauth flows where we want to reprocess and old
        # value with a new bindkey.
        self.last_service_info: BluetoothServiceInfo | None = None

    def _start_update(self, service_info: BluetoothServiceInfo) -> None:
        """Update from BLE advertisement data."""
        _LOGGER.debug("Parsing ATC BLE advertisement data: %s", service_info)
        self.set_device_manufacturer("ATC")
        self.set_device_type("ATC sensor")
        for id, data in service_info.service_data.items():
            if self._parse_atc(service_info, service_info.name, data):
                self.last_service_info = service_info

    def _parse_atc(
        self, service_info: BluetoothServiceInfo, name: str, data: bytes
    ) -> bool:
        """Parser for ATC sensors"""
        msg_length = len(data)

        mac_readable = service_info.address
        if len(mac_readable) != 17 and mac_readable[2] != ":":
            # On macOS, we get a UUID, which is useless for ATC sensors
            mac_readable = "00:00:00:00:00:00"

        source_mac = bytes.fromhex(mac_readable.replace(":", ""))

        if msg_length == 15:
            # Parse BLE message in ATC (pvvx/custom) format without encryption
            # https://github.com/pvvx/ATC_MiThermometer
            firmware = "ATC (pvvx)"
            atc_mac_reversed = data[0:6]
            atc_mac = atc_mac_reversed[::-1]

            if sys.platform == "darwin":
                "Use MAC address from data on macOS"
                source_mac = atc_mac

            if sys.platform != "darwin" and atc_mac != source_mac:
                _LOGGER.debug(
                    "MAC address doesn't match data frame. Expected: %s, Got: %s)",
                    to_mac(atc_mac),
                    to_mac(source_mac),
                )
                return False

            (temp, hum, volt, bat, packet_id, trg) = unpack(
                "<hHHBBB", data[6:]
            )  # noqa: F841

            self.update_predefined_sensor(
                SensorLibrary.TEMPERATURE__CELSIUS, temp / 100
            )
            self.update_predefined_sensor(SensorLibrary.HUMIDITY__PERCENTAGE, hum / 100)
            self.update_predefined_sensor(
                SensorLibrary.VOLTAGE__ELECTRIC_POTENTIAL_VOLT, volt / 1000
            )
            self.update_predefined_sensor(SensorLibrary.BATTERY__PERCENTAGE, bat)

            # ToDo: binary sensors
            # result = {
            #     "switch": (trg >> 1) & 1,
            #     "opening": (~trg ^ 1) & 1,
            #     "status": "opened",
            # }
        elif msg_length == 13:
            # Parse BLE message in ATC1441 format without encryption
            # https://github.com/atc1441/ATC_MiThermometer
            firmware = "ATC (atc1441)"
            atc_mac = data[0:6]

            if sys.platform == "darwin":
                "Use MAC address from data on macOS"
                source_mac = atc_mac

            if sys.platform != "darwin" and atc_mac != source_mac:
                _LOGGER.debug(
                    "MAC address doesn't match data frame. Expected: %s, Got: %s)",
                    to_mac(atc_mac),
                    to_mac(source_mac),
                )
                return False

            (temp, hum, bat, volt, packet_id) = unpack(">hBBHB", data[6:])

            self.update_predefined_sensor(SensorLibrary.TEMPERATURE__CELSIUS, temp / 10)
            self.update_predefined_sensor(SensorLibrary.HUMIDITY__PERCENTAGE, hum)
            self.update_predefined_sensor(
                SensorLibrary.VOLTAGE__ELECTRIC_POTENTIAL_VOLT, volt / 1000
            )
            self.update_predefined_sensor(SensorLibrary.BATTERY__PERCENTAGE, bat)

        elif msg_length == 11:
            # Parse BLE message in ATC pvvx/custom format with encryption
            # https://github.com/pvvx/ATC_MiThermometer
            if sys.platform == "darwin":
                _LOGGER.warning(
                    "Encrypted ATC pvvx/custom format is not supported on macOS, "
                    "use another advertising format"
                )
                return False

            self.mac_known = True
            atc_mac = source_mac
            # packet_id = data[4]
            firmware = "ATC (pvvx encrypted)"
            self.encrypted = True
            decrypted_data = self._decrypt_atc(data, atc_mac)
            if decrypted_data is None:
                return False
            else:
                (temp, hum, bat, trg) = unpack("<hHBB", decrypted_data)  # noqa: F841
                self.update_predefined_sensor(
                    SensorLibrary.TEMPERATURE__CELSIUS, temp / 100
                )
                self.update_predefined_sensor(
                    SensorLibrary.HUMIDITY__PERCENTAGE, hum / 100
                )
                self.update_predefined_sensor(SensorLibrary.BATTERY__PERCENTAGE, bat)

                # ToDo: binary sensors
                # result = {
                #     "switch": (trg >> 1) & 1,
                #     "opening": (~trg ^ 1) & 1,
                #     "status": "opened",
                # }
        elif msg_length == 8:
            # Parse BLE message in atc1441 format with encryption
            # https://github.com/atc1441/ATC_MiThermometer
            if sys.platform == "darwin":
                _LOGGER.warning(
                    "Encrypted ATC atc1441 format is not supported on macOS, "
                    "use another advertising format"
                )
                return False

            self.mac_known = True
            atc_mac = source_mac
            # packet_id = data[4]
            firmware = "ATC (atc1441 encrypted)"
            self.encrypted = True
            decrypted_data = self._decrypt_atc(data, atc_mac)
            if decrypted_data is None:
                return False
            else:
                temp = decrypted_data[0] / 2 - 40.0
                hum = decrypted_data[1] / 2
                bat = decrypted_data[2] & 0x7F
                trg = decrypted_data[2] >> 7  # noqa: F841
                if bat > 100:
                    bat = 100
                self.update_predefined_sensor(SensorLibrary.TEMPERATURE__CELSIUS, temp)
                self.update_predefined_sensor(SensorLibrary.HUMIDITY__PERCENTAGE, hum)
                self.update_predefined_sensor(SensorLibrary.BATTERY__PERCENTAGE, bat)

                # ToDo: binary sensors
                # result = {"switch": trg}
        else:
            return False
        identifier = service_info.address
        self.set_title(f"{name} ({identifier})")
        self.set_device_name(f"{name} ({identifier})")
        self.set_device_sw_version(firmware)
        return True

    def _decrypt_atc(self, data: bytes, atc_mac: bytes) -> bytes | None:
        """Decrypt ATC BLE encrypted advertisements"""
        if not self.bindkey:
            self.bindkey_verified = False
            _LOGGER.debug("Encryption key not set and adv is encrypted")
            return None

        if not self.bindkey or len(self.bindkey) != 16:
            self.bindkey_verified = False
            _LOGGER.error("Encryption key should be 16 bytes (32 characters) long")
            return None

        # prepare the data for decryption
        len_byte = (len(data) + 3).to_bytes(1, "little")
        uuid_bytes = b"\x16\x1a\x18"
        nonce = b"".join([atc_mac[::-1], len_byte, uuid_bytes, data[:1]])
        cipherpayload = data[1:-4]

        aad = b"\x11"
        token = data[-4:]
        cipher = AES.new(self.bindkey, AES.MODE_CCM, nonce=nonce, mac_len=4)
        cipher.update(aad)
        # decrypt the data
        try:
            decrypted_payload = cipher.decrypt_and_verify(cipherpayload, token)
        except ValueError as error:
            _LOGGER.warning("Decryption failed: %s", error)
            _LOGGER.debug("token: %s", token.hex())
            _LOGGER.debug("nonce: %s", nonce.hex())
            _LOGGER.debug("encrypted_payload: %s", cipherpayload.hex())
            return None
        if decrypted_payload is None:
            self.bindkey_verified = False
            _LOGGER.warning(
                "Decryption failed for %s, decrypted payload is None",
                to_mac(atc_mac),
            )
            return None
        self.bindkey_verified = True

        return decrypted_payload
