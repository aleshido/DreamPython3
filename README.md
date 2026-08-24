# DreamPython3

Script that automates the modem configuration and makes the listener compatible with Python 3. Information about the initial version of the script, made by Petri Trebilcock, can be found at http://www.dreamcast-talk.com/forum/viewtopic.php?f=3&t=7598.

## Important Note
This script is designed to run on modern Linux systems as an alternative to DreamPi, which currently has compatibility issues with Raspberry Pi 5. This is an experimental solution that has worked in my specific use case. Please use at your own discretion, as consistent support cannot be guaranteed.

## Supported Distributions
Both scripts detect their platform at startup, so the same code runs on Debian/Ubuntu and Fedora-family distributions (Fedora, Nobara, RHEL derivatives).

Detection probes for **commands and files** rather than reading the `ID` field of `/etc/os-release`, because derivatives report their own ID (Nobara reports `ID=nobara`, not `fedora`) and an ID-based check would fail on them.

| Concern | Debian/Ubuntu | Fedora family |
|---|---|---|
| Package manager | `apt-get` / `dpkg -s` | `dnf` / `rpm -q` |
| Package names | `ppp mgetty` | identical |
| mgetty config dir | `/etc/mgetty` | `/etc/mgetty+sendfax` |
| System log | `/var/log/syslog` | `/var/log/messages` (or `journalctl`) |
| `mgetty` / `pppd` | `/usr/sbin/...` | `/usr/bin/...` |

`dreampi3.py` detects independently of `DreamPi.sh`, so it can be run on its own. Override any value with environment variables:

```bash
sudo MODEM_TTY=ttyACM1 MGETTY_BIN=/usr/bin/mgetty python3 dreampi3.py
```

## Installation & Usage
1. Clone the repository:
   ```bash
   git clone https://github.com/aleshido/DreamPython3.git
   ```
2. Make the script executable:
   ```bash
   chmod +x DreamPi.sh
   ```
3. Run the script:
   ```bash
   sh ./DreamPi.sh
   ```

## Technical Specifications
- Modem: Tested with a Conexant CX93001 USB modem on ttyACM0 (auto-detected)
- DNS: Uses Google DNS (8.8.8.8)
- Network: Sets IP to 192.168.1.20:192.168.1.200 in /etc/ppp/options.<tty> - you may need to change these if the addresses are already in use on your network.
- Authentication: any username/password is accepted - see "Dreamcast Settings" below.

## Dreamcast Settings

Two console-side settings matter, and both can silently prevent a connection.

### Dial mode: use `ATX3`

Add `ATX3` to the Dreamcast's modem AT init string. `ATXn` controls call progress
monitoring:

| Setting | Waits for dial tone | Detects busy |
|---|---|---|
| `X0` | no (blind) | no |
| `X1` | no (blind) | no |
| `X2` | **yes** | no |
| `X3` | **no (blind)** | yes |
| `X4` | **yes** | yes |

The USB modem does not generate a dial tone (see Known Issues), so a console left on
`X2`/`X4` waits, hears silence, and hangs up after ~2 seconds - producing no DTMF at
all. `X3` makes it dial immediately. If `X3` still aborts calls, try `X0`, which also
disables busy detection.

### Credentials

**Enter anything.** The console requires a username and password, but the PC accepts
whatever it sends - `dreams` / `dreamcast` is the suggested convention, and works
because everything works.

This matters because the credentials a console sends are not reliably under your
control. Many ship with the stock pair `profile` / `eliforp` ("profile" reversed), the
ISP sign-up flow authenticates as `signup`, and a console typically will not let you
save new settings until it has validated a connection at least once - so you cannot
simply correct them.

`DreamPi.sh` therefore writes a single wildcard line to `/etc/ppp/pap-secrets`:

```
*	*	""	*
```

A `*` client name matches any name, a `""` secret matches any password, and `*` in the
address field allows any address. `login` is deliberately absent from
`/etc/ppp/options`, so no PAM check and no system account is involved.

> **Do not add a more specific line alongside it.** pppd selects the match with the
> fewest wildcards, so an exact entry such as `dreams * dreamcast *` would shadow the
> wildcard and make `dreams` the only username that can be *rejected*.

To see what your console actually sends - rarely necessary now, but useful when
debugging - add `show-password` to `/etc/ppp/options` temporarily and dial. PAP is
cleartext, so the password appears in the log instead of `<hidden>`.

## mgetty AutoPPP (required)

When mgetty receives an LCP configure request it looks up the magic `/AutoPPP/` user
in `login.config`. **Every distribution ships that line commented out**, so the call
falls through to the `*` catch-all, lands in `/bin/login`, and stalls - the console
connects at carrier level and then goes nowhere.

`DreamPi.sh` now enables it automatically. To do it by hand, add this **above** the
`*` catch-all (mgetty uses the first matching rule, so appending it does nothing):

```
/AutoPPP/ - a_ppp /usr/bin/pppd auth -chap +pap debug
```

The file lives at `/etc/mgetty+sendfax/login.config` on Fedora-family systems and
`/etc/mgetty/login.config` on Debian/Ubuntu.

## Network Setup (manual)

`proxyarp` places the Dreamcast on your LAN so the rest of your network can reach it.

**IP forwarding needs no action** - the `ktune` option in `/etc/ppp/options` enables
`net.ipv4.ip_forward` automatically when the link comes up.

The firewall does need one step. On firewalld systems the ordering matters, because
`--reload` discards runtime changes:

```bash
# permanent first, then reload, then runtime - in that order
sudo firewall-cmd --permanent --zone=trusted --add-interface=ppp0
sudo firewall-cmd --reload
sudo firewall-cmd --zone=trusted --add-interface=ppp0
```

On Ubuntu:

```bash
sudo ufw allow in on ppp0
```

Also make sure your router does **not** hand out `192.168.1.20` or `192.168.1.200`
from its DHCP pool, since both are assigned statically to the PPP link.

## Verifying a connection

```bash
journalctl -f -t pppd            # watch negotiation
ip -brief addr show ppp0         # expect: 192.168.1.20 peer 192.168.1.200/32
ping 192.168.1.200               # the console itself
ip neigh show proxy              # expect: 192.168.1.200 dev <lan-if> proxy
sudo tail -f /var/log/mgetty.tty*.log
```

A healthy sequence in the pppd log ends with:

```
PAP peer authentication succeeded for <user>
local  IP address 192.168.1.20
remote IP address 192.168.1.200
```

## Known Issues / To-Do

- **No dial tone is generated.** `AT+VTX=1` puts the modem into transmit mode but never
  streams any audio, and it is half-duplex so the modem cannot detect DTMF while
  transmitting. Setting the console to blind dial (`ATX3`, above) avoids the problem
  entirely and is the recommended fix. If you need a real dial tone, it requires
  full-duplex `AT+VTR` with a synthesised 350+440 Hz tone streamed at 8 kHz
  (DLE-escaping `0x10` in the payload) and the tone cut on the first detected digit.

- **Fixed: `Connected!` was not always reported.** The log follow was started after
  mgetty with `-n 0` (new entries only), so pppd's `remote IP address` could be logged
  before anything was watching for it. The follow now starts *before* mgetty.

- **Fixed: the listener blocked after answering.** The follow loop only exited on
  `remote IP address` or `Modem hangup`, so a call that failed mid-PPP stranded the
  script and it stopped watching the modem entirely. `waitForLink()` is now bounded by
  `CONNECT_TIMEOUT` (90s) and also recognises pppd's failure messages, so the listener
  always returns to listening on its own.

  Note that `Modem hangup` never actually appears - pppd logs `Connection terminated`.
  End of a live session is now detected by checking whether pppd is still running
  (`linkIsUp()`) rather than by matching a log string.

- **Fixed: idle CPU spin.** The serial port was opened with `timeout=0`, so `read(1)`
  returned instantly and the listen loop consumed ~100% of a core. It now uses
  `timeout=0.1`, which drops idle usage to near zero - worth having on a Raspberry Pi.

- **Authentication is deliberately open.** `/etc/ppp/pap-secrets` accepts any username
  and password from any address, and `login` is not set, so nothing is checked against
  the system. Reaching that line needs physical access to the modem's phone line, but
  note that `proxyarp` places the connected peer on your LAN. If you want it narrowed,
  replace the trailing `*` with the peer address (`192.168.1.200` by default) so a
  client may only claim that one address.

- **ModemManager may claim the modem.** It probes `ttyACM*` devices (`ID_MM_CANDIDATE=1`)
  and can hijack the port mid-call. To exclude the modem, create
  `/etc/udev/rules.d/99-dreampi.rules` with your modem's USB IDs (`lsusb`):
  ```
  SUBSYSTEM=="tty", ATTRS{idVendor}=="0572", ATTRS{idProduct}=="1340", ENV{ID_MM_DEVICE_IGNORE}="1"
  ```
  Then `sudo udevadm control --reload-rules && sudo udevadm trigger`.

- **Fixed: serial port contention.** The listener used to keep `/dev/<tty>` open while
  mgetty opened the same device, leaving the modem in voice mode and off-hook. `ATA`
  then never produced `CONNECT`. `releaseModem()` now sends `AT+VLS=0` and `ATZ` and
  closes the port before mgetty starts.

## Credits
- Original script by Petri Trebilcock
- Python 3 port and improved logging in collaboration with [Paula Fleck](https://github.com/paulakfleck)
