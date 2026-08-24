#!/usr/bin/env python3

import serial
import os
import glob
import shutil
import logging
import subprocess
import time
import sys
import select
import math
from datetime import datetime

def graphic():
    print("     ____                            ____  _    ___  ")
    print("    / __ \\________  ____  ____ ___  / __ \\(_)  /__ ")
    print("   / / / / ___/ _ \\/ __ `/ __ `__ \\ /_/ / /   __/ / ")
    print("  / /_/ / /  /  __/ /_/ / / / / / / ____/ /   / __/  ")
    print(" /_____/_/   \\___/\\__,_/_/ /_/ /_/_/   /_/   /____/  ")
    print(" RaspberryPi PC-DC Server Helper by Petri Trebilcock ")
    print("        Original idea/code by Luke Benstead          ")
    print("")

# ---------------------------------------------------------------------------
# Platform detection
#
# Values are probed from the running system so the same script works on
# Debian/Ubuntu and Fedora/Nobara without edits. Each one can be overridden
# with an environment variable, and the script stays runnable standalone.
# ---------------------------------------------------------------------------

def _first_dev(*patterns):
    """Return the basename of the first serial device that actually exists."""
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            return os.path.basename(path)
    return "ttyACM0"

def _first_exec(*candidates):
    """Return the first candidate found on PATH or executable at a full path."""
    for candidate in candidates:
        if os.path.sep in candidate:
            if os.access(candidate, os.X_OK):
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return "/usr/sbin/mgetty"

def _detect_log_follow():
    """Command used to follow the system log.

    journalctl is preferred because it captures every priority. Typical rsyslog
    rules are '*.info', which drop LOG_DEBUG - if a message we wait for were
    logged at debug level the state machine would block forever.

    Output is filtered to pppd and mgetty. Everything the state machine looks
    for comes from pppd, and mgetty supplies the answer/carrier context; without
    the filter roughly two thirds of the lines printed during a call are
    unrelated system noise. The syslog fallback below cannot filter this way.
    """
    if shutil.which("journalctl"):
        return ["journalctl", "-f", "-n", "0", "-o", "cat",
                "-t", "pppd", "-t", "mgetty"]
    for path in ("/var/log/syslog", "/var/log/messages"):
        if os.path.exists(path):
            return ["tail", "-f", "-n", "1", path]
    return ["tail", "-f", "-n", "1", "/var/log/syslog"]

MODEM_DEVICE = os.environ.get("MODEM_TTY") or _first_dev("/dev/ttyACM*", "/dev/ttyUSB*")
MGETTY_BIN = os.environ.get("MGETTY_BIN") or _first_exec(
    "mgetty", "/usr/sbin/mgetty", "/sbin/mgetty", "/usr/bin/mgetty")
LOG_FOLLOW = _detect_log_follow()

# --- Dial tone -------------------------------------------------------------
#
# The modem produces no dial tone of its own. A console that waits for one
# (the default on most, and not always configurable per game) hears silence,
# gives up after ~2 seconds and never sends any DTMF. Synthesising a real
# 350+440 Hz tone removes the need for any console-side workaround such as
# ATX3 blind dialling.
#
# This needs full duplex (AT+VTR): the modem must keep decoding DTMF while we
# are transmitting. AT+VTX is transmit-only, which is why the original
# --enable-dial-tone flag could never have worked.

SAMPLE_RATE = 8000            # AT+VSM=1,8000 - 8-bit unsigned PCM
CHUNK_MS = 20                 # transmit granularity
CHUNK = SAMPLE_RATE * CHUNK_MS // 1000
DLE, ETX = 0x10, 0x03

def _renderDialTone():
    """One second of 350+440 Hz, 8-bit unsigned PCM (128 = silence)."""
    buf = bytearray()
    for n in range(SAMPLE_RATE):
        t = n / SAMPLE_RATE
        v = 128 + int(38 * (math.sin(2 * math.pi * 350 * t) +
                            math.sin(2 * math.pi * 440 * t)))
        buf.append(max(1, min(254, v)))   # keep clear of 0x00/0xff rails
    return bytes(buf)

DIAL_TONE = _renderDialTone()
SILENCE = bytes([128]) * CHUNK

def _esc(buf):
    """A 0x10 byte inside voice data must be doubled, or it reads as DLE."""
    return buf.replace(b"\x10", b"\x10\x10")

def runMgetty():
    subprocess.Popen(['sudo', MGETTY_BIN, '-s', '115200', '-D', '/dev/'+MODEM_DEVICE],
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)

def send_command(modem, command):
    final_command = ("%s\r\n" % command).encode('utf-8')
    modem.write(final_command)
    logging.info(command)

    line = modem.readline().decode('utf-8', errors='ignore')
    while True:
        if "OK" in line or "ERROR" in line or "CONNECT" in line:
            logging.info(line.strip())
            break

        line = modem.readline().decode('utf-8', errors='ignore')

def killMgetty():
    subprocess.Popen(['sudo', 'killall', '-USR1', 'mgetty'])

def releaseModem(modem):
    """Hand the modem over to mgetty.

    mgetty answers with ATA, which only negotiates a data carrier once the
    modem has left voice mode. Holding the port open here would also leave
    two processes sharing the tty while mgetty resets DTR, so release it.
    """
    try:
        # Leave the voice stream first; AT commands are not parsed until
        # the DLE ETX terminator ends VTR mode.
        modem.write(bytes([DLE, ETX]))
        modem.flush()
        time.sleep(0.4)
        for _ in range(10):
            if not modem.read(8192):
                break
        modem.write(b"AT+VLS=0\r\n")  # back on-hook
        modem.flush()
        time.sleep(0.3)
        modem.write(b"ATZ\r\n")       # leave voice mode
        modem.flush()
        time.sleep(0.5)
    except Exception as e:
        logging.info("Modem release failed: %s" % e)
    finally:
        try:
            modem.close()
        except Exception:
            pass

def modemConnect():
    logging.info("Connecting to modem...:")
    dev = serial.Serial("/dev/" + MODEM_DEVICE, 460800, timeout=0.1)
    logging.info("Connected.")
    return dev

def initModem():
    """Bring the modem up in voice mode.

    Returns (modem, duplex). duplex is True when AT+VTR was accepted, in
    which case main() streams a dial tone while listening. Pass
    --no-dial-tone to force the old half-duplex behaviour, which relies on
    the console being set to blind dial (ATX3).
    """
    modem = modemConnect()

    send_command(modem, "ATZE1") # RESET
    send_command(modem, "AT+FCLASS=8")  # Switch to Voice mode

    if "--no-dial-tone" in sys.argv:
        send_command(modem, "AT+VLS=1") # Go online
        logging.info("Dial tone disabled, listening...")
        return modem, False

    send_command(modem, "AT+VSM=1,8000")  # 8-bit unsigned PCM @ 8kHz
    send_command(modem, "AT+VGT=200")     # transmit gain
    send_command(modem, "AT+VLS=1")       # Go online

    # AT+VTR is full duplex: transmit the tone and decode DTMF at once.
    modem.write(b"AT+VTR\r\n")
    modem.flush()
    time.sleep(1.2)
    reply = modem.read(4096).decode("utf-8", errors="ignore")
    logging.info("AT+VTR -> %s" % reply.strip().replace("\r\n", " ")[:40])

    if "CONNECT" not in reply:
        logging.info("Full duplex refused, falling back to half duplex.")
        logging.info("Set the console to blind dial (ATX3) or it will hang up.")
        logging.info("Setup complete, listening...")
        return modem, False

    logging.info("Setup complete, dial tone live, listening...")
    return modem, True

CONNECT_TIMEOUT = 90          # seconds to wait for PPP to come up
LINK_FAILED = ("Authentication failed", "Connection terminated", "LCP TermReq")

def followLog():
    """Start following the system log, line-buffered."""
    return subprocess.Popen(LOG_FOLLOW, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)

def waitForLink(proc, timeout=CONNECT_TIMEOUT):
    """Watch the log for the outcome of a call.

    Returns "CONNECTED", "FAILED" or "TIMEOUT". Bounded, so a call that never
    negotiates cannot strand the listener.
    """
    deadline = time.time() + timeout
    fd = proc.stdout.fileno()
    buf = ""
    while time.time() < deadline:
        # Read the raw fd: a buffered readline() could pull several lines into
        # Python's buffer, and select() would then not report the fd readable
        # again, hiding a line we are waiting for.
        ready, _, _ = select.select([fd], [], [], 1.0)
        if not ready:
            continue
        data = os.read(fd, 4096)
        if not data:
            break
        buf += data.decode("utf-8", errors="ignore")
        lines = buf.split("\n")
        buf = lines.pop()          # keep any partial trailing line
        for line in lines:
            logging.info(line)
            if "remote IP address" in line:
                return "CONNECTED"
            if any(s in line for s in LINK_FAILED):
                return "FAILED"
    return "TIMEOUT"

def linkIsUp():
    """True while a pppd session owns the modem.

    pppd liveness is used instead of matching a log string: the "Modem hangup"
    message the original code waited for is not what pppd actually emits.
    """
    return subprocess.call(["pgrep", "-x", "pppd"],
                           stdout=subprocess.DEVNULL) == 0

def newToneState():
    return {"pos": 0, "next_tx": time.time(), "on": True, "pending": False}

def pumpModem(modem, duplex, state):
    """One pass of the listen loop; returns any DTMF digits seen.

    In duplex mode the dial tone has to keep flowing at 8 kHz while we decode
    the returning stream, so the read waits only until the next 20ms chunk is
    due. A plain blocking read could overshoot that deadline and make the tone
    stutter.
    """
    if duplex:
        now = time.time()
        if now >= state["next_tx"]:
            if state["on"]:
                end = state["pos"] + CHUNK
                if end <= SAMPLE_RATE:
                    chunk = DIAL_TONE[state["pos"]:end]
                else:
                    chunk = DIAL_TONE[state["pos"]:] + DIAL_TONE[:end - SAMPLE_RATE]
                state["pos"] = end % SAMPLE_RATE
            else:
                chunk = SILENCE
            try:
                modem.write(_esc(chunk))
                modem.flush()
            except Exception as e:
                logging.info("Tone write failed: %s" % e)
            state["next_tx"] += CHUNK_MS / 1000.0
            if state["next_tx"] < now:
                state["next_tx"] = now + CHUNK_MS / 1000.0

        wait = max(0.0, state["next_tx"] - time.time())
        ready, _, _ = select.select([modem.fileno()], [], [], wait)
        # Read only what is buffered. A fixed-size read would block on the
        # port timeout and overshoot the next chunk deadline, starving the
        # tone (measured: 1.7 kB/s emitted instead of the required 8 kB/s).
        data = modem.read(min(modem.in_waiting, 4096)) if ready else b""
    else:
        data = modem.read(1)

    if not data:
        return []

    digits = []
    i = 0
    while i < len(data):
        b = data[i]
        if state["pending"]:
            state["pending"] = False
            if b != DLE:               # DLE DLE is a literal 0x10 sample
                c = chr(b) if 32 <= b < 127 else ""
                if c.isdigit():
                    digits.append(c)
            i += 1
            continue
        if b == DLE:
            if i + 1 >= len(data):
                state["pending"] = True
                i += 1
                continue
            nb = data[i + 1]
            if nb != DLE:
                c = chr(nb) if 32 <= nb < 127 else ""
                if c.isdigit():
                    digits.append(c)
            i += 2
            continue
        i += 1
    return digits

def main():
    graphic()
    logging.info("Modem device: /dev/%s" % MODEM_DEVICE)
    logging.info("mgetty binary: %s" % MGETTY_BIN)
    logging.info("Log source: %s" % " ".join(LOG_FOLLOW))

    modem, duplex = initModem()
    tone = newToneState()
    timeSinceDigit = None
    mode = "LISTENING"

    while True:
        if mode == "LISTENING":
            if timeSinceDigit is not None:
                now = datetime.now()
                delta = (now - timeSinceDigit).total_seconds()
                if delta > 2:
                    logging.info("Answering call...")
                    # Started before mgetty so pppd's "remote IP address" cannot
                    # be logged before we are watching for it.
                    follower = followLog()
                    try:
                        releaseModem(modem)
                        runMgetty()
                        time.sleep(4)
                        killMgetty()
                        logging.info("Call answered!")

                        result = waitForLink(follower)
                        if result == "CONNECTED":
                            logging.info("Connected!")
                            mode = "CONNECTED"
                            while linkIsUp():   # a session may last hours
                                time.sleep(2)
                            logging.info("Link closed, going back to listening")
                        else:
                            logging.info("Call did not establish (%s)" % result)
                            time.sleep(10)
                    finally:
                        follower.terminate()
                        try:
                            follower.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            follower.kill()

                    timeSinceDigit = None
                    mode = "LISTENING"
                    modem, duplex = initModem()   # releaseModem() closed it
                    tone = newToneState()

            for digit in pumpModem(modem, duplex, tone):
                tone["on"] = False    # a real exchange cuts the tone on dialling
                timeSinceDigit = datetime.now()
                print("%s" % digit)

    return 0

if __name__ == '__main__':
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger().addHandler(logging.StreamHandler())
    sys.exit(main())
