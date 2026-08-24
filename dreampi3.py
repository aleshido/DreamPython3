#!/usr/bin/env python3

import serial
import os
import glob
import shutil
import logging
import subprocess
import time
import sys
import sh
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
    """
    if shutil.which("journalctl"):
        return ["journalctl", "-f", "-n", "0", "-o", "cat"]
    for path in ("/var/log/syslog", "/var/log/messages"):
        if os.path.exists(path):
            return ["tail", "-f", "-n", "1", path]
    return ["tail", "-f", "-n", "1", "/var/log/syslog"]

MODEM_DEVICE = os.environ.get("MODEM_TTY") or _first_dev("/dev/ttyACM*", "/dev/ttyUSB*")
MGETTY_BIN = os.environ.get("MGETTY_BIN") or _first_exec(
    "mgetty", "/usr/sbin/mgetty", "/sbin/mgetty", "/usr/bin/mgetty")
LOG_FOLLOW = _detect_log_follow()

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
    dev = serial.Serial("/dev/" + MODEM_DEVICE, 460800, timeout=0)
    logging.info("Connected.")
    return dev

def initModem():
    modem = modemConnect()

    send_command(modem, "ATZE1") # RESET
    send_command(modem, "AT+FCLASS=8")  # Switch to Voice mode
    send_command(modem, "AT+VLS=1") # Go online

    if "--enable-dial-tone" in sys.argv:
        print("Dial tone enabled, starting transmission...")
        send_command(modem, "AT+VTX=1") # Transmit audio (for dial tone)

    logging.info("Setup complete, listening...")
    return modem

def main():
    graphic()
    logging.info("Modem device: /dev/%s" % MODEM_DEVICE)
    logging.info("mgetty binary: %s" % MGETTY_BIN)
    logging.info("Log source: %s" % " ".join(LOG_FOLLOW))

    modem = initModem()
    timeSinceDigit = None
    mode = "LISTENING"

    while True:
        if mode == "LISTENING":
            if timeSinceDigit is not None:
                now = datetime.now()
                delta = (now - timeSinceDigit).total_seconds()
                if delta > 2:
                    logging.info("Answering call...")
                    releaseModem(modem)
                    runMgetty()
                    time.sleep(4)
                    killMgetty()
                    logging.info("Call answered!")
                    for line in sh.Command(LOG_FOLLOW[0])(*LOG_FOLLOW[1:], _iter=True):
                        logging.info(line)
                        if "remote IP address" in line:
                            logging.info("Connected!")
                            mode = "CONNECTED"
                        if "Modem hangup" in line:
                            logging.info("Detected modem hang up, going back to listening")
                            time.sleep(10)
                            timeSinceDigit = None
                            mode = "LISTENING"
                            modem.close()
                            modem = initModem()
                            break

            char = modem.read(1)
            if not char:
                continue

            if ord(char) == 16:
                try:
                    char = modem.read(1)
                    digit = int(char.decode('utf-8', errors='ignore'))
                    timeSinceDigit = datetime.now()
                    print("%s" % digit)
                except (TypeError, ValueError):
                    pass

    return 0

if __name__ == '__main__':
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger().addHandler(logging.StreamHandler())
    sys.exit(main())
