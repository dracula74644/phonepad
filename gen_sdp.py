#!/usr/bin/env python3
"""Generate the HID report descriptor and the BlueZ SDP record for a
Bluetooth keyboard. Pointing is out of scope: use a mouse on the phone.

A mouse collection was added on 2026-10-29 and removed again the same day:
the cursor did not behave properly on the phone. The host caches this
descriptor when it bonds, so changing it means unpairing and pairing again."""

KEYBOARD = [
    0x05, 0x01,              # Usage Page (Generic Desktop)
    0x09, 0x06,              # Usage (Keyboard)
    0xA1, 0x01,              # Collection (Application)
    0x85, 0x01,              #   Report ID (1)
    0x05, 0x07,              #   Usage Page (Keyboard/Keypad)
    0x19, 0xE0,              #   Usage Minimum (LeftControl)
    0x29, 0xE7,              #   Usage Maximum (Right GUI)
    0x15, 0x00,              #   Logical Minimum (0)
    0x25, 0x01,              #   Logical Maximum (1)
    0x75, 0x01,              #   Report Size (1)
    0x95, 0x08,              #   Report Count (8)
    0x81, 0x02,              #   Input (Data,Var,Abs)   -- modifier byte
    0x95, 0x01,              #   Report Count (1)
    0x75, 0x08,              #   Report Size (8)
    0x81, 0x01,              #   Input (Const)          -- reserved byte
    0x95, 0x06,              #   Report Count (6)
    0x75, 0x08,              #   Report Size (8)
    0x15, 0x00,              #   Logical Minimum (0)
    0x26, 0xFF, 0x00,        #   Logical Maximum (255)
    0x05, 0x07,              #   Usage Page (Keyboard/Keypad)
    0x19, 0x00,              #   Usage Minimum (0)
    0x29, 0xFF,              #   Usage Maximum (255)
    0x81, 0x00,              #   Input (Data,Array)     -- 6 key slots
    0xC0,                    # End Collection
]

DESC = KEYBOARD
HEX = "".join("%02x" % b for b in DESC)

RECORD = f"""<?xml version="1.0" encoding="UTF-8" ?>
<record>
  <!-- ServiceClassIDList: Human Interface Device -->
  <attribute id="0x0001">
    <sequence><uuid value="0x1124" /></sequence>
  </attribute>
  <!-- ProtocolDescriptorList: L2CAP PSM 0x0011 (HID control) over HIDP -->
  <attribute id="0x0004">
    <sequence>
      <sequence>
        <uuid value="0x0100" />
        <uint16 value="0x0011" />
      </sequence>
      <sequence><uuid value="0x0011" /></sequence>
    </sequence>
  </attribute>
  <!-- BrowseGroupList: PublicBrowseRoot -->
  <attribute id="0x0005">
    <sequence><uuid value="0x1002" /></sequence>
  </attribute>
  <!-- LanguageBaseAttributeIDList: en, UTF-8, base 0x0100 -->
  <attribute id="0x0006">
    <sequence>
      <uint16 value="0x656e" />
      <uint16 value="0x006a" />
      <uint16 value="0x0100" />
    </sequence>
  </attribute>
  <!-- BluetoothProfileDescriptorList: HID v1.0 -->
  <attribute id="0x0009">
    <sequence>
      <sequence>
        <uuid value="0x1124" />
        <uint16 value="0x0100" />
      </sequence>
    </sequence>
  </attribute>
  <!-- AdditionalProtocolDescriptorLists: L2CAP PSM 0x0013 (HID interrupt) -->
  <attribute id="0x000d">
    <sequence>
      <sequence>
        <sequence>
          <uuid value="0x0100" />
          <uint16 value="0x0013" />
        </sequence>
        <sequence><uuid value="0x0011" /></sequence>
      </sequence>
    </sequence>
  </attribute>
  <attribute id="0x0100"><text value="Phonepad" /></attribute>
  <attribute id="0x0101"><text value="Laptop keyboard" /></attribute>
  <attribute id="0x0102"><text value="phonepad" /></attribute>
  <!-- HIDDeviceReleaseNumber -->
  <attribute id="0x0200"><uint16 value="0x0100" /></attribute>
  <!-- HIDParserVersion -->
  <attribute id="0x0201"><uint16 value="0x0111" /></attribute>
  <!-- HIDDeviceSubclass: 0x40 = keyboard -->
  <attribute id="0x0202"><uint8 value="0x40" /></attribute>
  <!-- HIDCountryCode: not localised -->
  <attribute id="0x0203"><uint8 value="0x00" /></attribute>
  <!-- HIDVirtualCable -->
  <attribute id="0x0204"><boolean value="true" /></attribute>
  <!-- HIDReconnectInitiate: we re-dial the host ourselves after a restart -->
  <attribute id="0x0205"><boolean value="true" /></attribute>
  <!-- HIDDescriptorList: 0x22 = report descriptor -->
  <attribute id="0x0206">
    <sequence>
      <sequence>
        <uint8 value="0x22" />
        <text encoding="hex" value="{HEX}" />
      </sequence>
    </sequence>
  </attribute>
  <!-- HIDLANGIDBaseList: en-US -->
  <attribute id="0x0207">
    <sequence>
      <sequence>
        <uint16 value="0x0409" />
        <uint16 value="0x0100" />
      </sequence>
    </sequence>
  </attribute>
  <!-- HIDSDPDisable -->
  <attribute id="0x0208"><boolean value="false" /></attribute>
  <!-- HIDBatteryPower -->
  <attribute id="0x0209"><boolean value="true" /></attribute>
  <!-- HIDRemoteWake: false, so typing does not light up the phone's own
       screen while DeX is running on the external monitor -->
  <attribute id="0x020a"><boolean value="false" /></attribute>
  <!-- HIDProfileVersion -->
  <attribute id="0x020b"><uint16 value="0x0100" /></attribute>
  <!-- HIDSupervisionTimeout -->
  <attribute id="0x020c"><uint16 value="0x0c80" /></attribute>
  <!-- HIDNormallyConnectable -->
  <attribute id="0x020d"><boolean value="true" /></attribute>
  <!-- HIDBootDevice -->
  <attribute id="0x020e"><boolean value="true" /></attribute>
</record>
"""

if __name__ == "__main__":
    with open("sdp_record.xml", "w") as fh:
        fh.write(RECORD)
    print("report descriptor: %d bytes" % len(DESC))
    print("keyboard report: report id + 8 bytes")
    print("wrote sdp_record.xml")
