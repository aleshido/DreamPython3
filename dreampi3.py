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

# --debug-line reports what the modem hears: DLE-shielded call-progress
# events, and a periodic level meter of the audio coming back from the line.
# Useful when a console refuses to dial and you need to know whether it ever
# went off-hook at all.
DEBUG_LINE = "--debug-line" in sys.argv
TX_GAIN = os.environ.get("TX_GAIN", "200")

# The tone desensitises the modem's DTMF detector, so it has to stop as soon as
# the console starts dialling - not once a digit has been decoded, which would
# deadlock: tone blocks detection, so nothing is decoded, so the tone never
# stops. A real exchange cuts dial tone on the first dialling energy it sees.
# Idle level (our own echo) measures ~12; a console off-hook pushes it past 25.
# Threshold for deciding the console is on the line. Auto-calibrated by
# default: the idle level is our own tone echoing back, and it scales with
# TX_GAIN (~12 at gain 128, ~24 at gain 200) and drifts by 30% or more over
# minutes. A fixed number is therefore only ever right for one gain on one
# modem. Set TONE_CUT_LEVEL to pin it manually instead.
TONE_CUT_LEVEL = os.environ.get("TONE_CUT_LEVEL")   # None = auto
TONE_CUT_FACTOR = float(os.environ.get("TONE_CUT_FACTOR", "1.7"))
BASELINE_ALPHA = 0.15        # EMA smoothing for the idle baseline
BASELINE_WARMUP = 2.0        # seconds of samples before the threshold is trusted
# If the line lifts clearly above the baseline but never clears the threshold,
# the console is probably dialling and we cannot see it - the usual cause is a
# transmit gain too low for the echo and the console signal to be separable.
# Quake III at TX_GAIN=128 sat at 18.4 against a baseline of 12.5, a ratio of
# 1.47, and simply never triggered. Warn instead of failing silently.
NEAR_MISS_FACTOR = 1.2
TONE_RESUME_AFTER = float(os.environ.get("TONE_RESUME_AFTER", "6.0"))
# Hold the tone for a moment AFTER the console picks up. Quake III needs to
# hear a tone once it is off-hook before it will dial; PSO needs the tone gone
# before it dials, because it deafens the DTMF detector. Cutting on off-hook
# serves PSO and starves Quake, so hold briefly, then cut.
TONE_HOLD = float(os.environ.get("TONE_HOLD", "1.5"))
VOICE_EVENTS = {
    "R": "RING", "b": "BUSY tone", "d": "DIAL TONE detected",
    "o": "OVERRUN", "s": "SILENCE", "q": "QUIET", "c": "FAX CNG",
    "e": "LINE ERROR", "h": "FAR-END ON-HOOK", "X": "DTMF start",
}

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
    # Transmit gain. The modem default of 128 is too quiet for some consoles:
    # Quake III Arena reports no dial tone at that level. 200 works for both
    # titles tested. The returning echo clips at this gain, which is ugly but
    # has caused no failure - and TONE_CUT_LEVEL must be scaled alongside it,
    # since the idle echo level tracks the transmit gain.
    send_command(modem, "AT+VGT=%s" % TX_GAIN)
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
    now = time.time()
    return {"pos": 0, "next_tx": now, "on": True, "pending": False,
            "lvl_n": 0, "lvl_sum": 0, "lvl_peak": 0, "lvl_at": now,
            "log_at": now, "cut_at": 0.0, "got_digit": False,
            "busy_at": 0.0, "baseline": None, "base_at": now,
            "near": 0, "warned_at": 0.0}

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
                elif DEBUG_LINE and c in VOICE_EVENTS:
                    logging.info("LINE: %s" % VOICE_EVENTS[c])
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
                elif DEBUG_LINE and c in VOICE_EVENTS:
                    logging.info("LINE: %s" % VOICE_EVENTS[c])
            i += 2
            continue
        dev = abs(b - 128)            # audio sample; 128 is silence
        state["lvl_sum"] += dev
        state["lvl_peak"] = max(state["lvl_peak"], dev)
        state["lvl_n"] += 1
        i += 1

    if digits:
        state["got_digit"] = True

    now = time.time()
    if state["lvl_n"] and now - state["lvl_at"] >= 0.4:
        avg = state["lvl_sum"] / state["lvl_n"]
        peak = state["lvl_peak"]
        state["lvl_n"] = state["lvl_sum"] = state["lvl_peak"] = 0
        state["lvl_at"] = now

        if state["baseline"] is None:
            state["baseline"] = avg

        # Derive the threshold from the EXISTING baseline before deciding, so a
        # sample that turns out to be the console cannot first inflate the
        # threshold it then has to clear.
        if TONE_CUT_LEVEL is not None:
            threshold = float(TONE_CUT_LEVEL)
        else:
            threshold = state["baseline"] * TONE_CUT_FACTOR
        warm = (now - state["base_at"]) >= BASELINE_WARMUP
        quiet = avg <= threshold

        # Learn only from samples that look like an idle line. Judge that
        # against the BASELINE, not the threshold: a threshold set too high
        # makes every sample look quiet, so the console's own signal would be
        # learned - which is exactly the case the near-miss warning exists for.
        idle_like = avg <= state["baseline"] * NEAR_MISS_FACTOR
        if duplex and state["on"] and not state["busy_at"] and idle_like:
            state["baseline"] += BASELINE_ALPHA * (avg - state["baseline"])

        # Elevated but not enough to trigger - count it and say so.
        if duplex and warm and state["on"] and quiet and state["baseline"]:
            if avg > state["baseline"] * NEAR_MISS_FACTOR:
                state["near"] += 1
                if state["near"] >= 3 and now - state["warned_at"] > 30:
                    state["warned_at"] = now
                    logging.info("LINE: activity at %.1f never reaches the cut "
                                 "threshold %.1f (baseline %.1f). The console may "
                                 "be dialling unheard - try a higher TX_GAIN."
                                 % (avg, threshold, state["baseline"]))
            else:
                state["near"] = 0

        if duplex and warm and state["on"] and not quiet:
            if not state["busy_at"]:
                state["busy_at"] = now
                if DEBUG_LINE:
                    logging.info("LINE: off-hook (avg=%.1f), holding tone %.1fs"
                                 % (avg, TONE_HOLD))
            elif now - state["busy_at"] >= TONE_HOLD:
                state["on"] = False   # tone heard; free the DTMF detector
                state["cut_at"] = now
                if DEBUG_LINE:
                    logging.info("LINE: hold elapsed, cutting dial tone")
        elif (duplex and not state["on"] and not state["got_digit"]
              and now - state["cut_at"] > TONE_RESUME_AFTER):
            state["on"] = True        # false alarm - the console still needs it
            state["busy_at"] = 0.0
            if DEBUG_LINE:
                logging.info("LINE: nothing dialled, resuming dial tone")

        if DEBUG_LINE and now - state["log_at"] >= 2.0:
            logging.info("LINE: level avg=%5.1f peak=%3d  base=%4.1f cut=%4.1f%s" % (
                avg, peak, state["baseline"], threshold,
                "" if warm else "  (warming up)"))
            state["log_at"] = now

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
