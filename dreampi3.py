#!/usr/bin/env python3

import serial
import os
import logging
import subprocess
import time
import sys
import sh
import argparse
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

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--device', default="ttyACM0", help='Modem device (e.g., ttyACM0)')
    parser.add_argument('--ring-timeout', type=int, default=2, help='Time to wait after ring before answering')
    return parser.parse_args()

def runMgetty(device):
    result = subprocess.Popen(['sudo', '/sbin/mgetty', '-s', '115200', '-D', f'/dev/{device}'], capture_output=True)
    if result.returncode != 0:
        logging.error(f"Erro ao iniciar mgetty: {result.stderr.decode()}")

def send_command(modem, command):
    final_command = (f"{command}\r\n").encode('utf-8')
    modem.write(final_command)
    logging.info(f"Sent: {command}")

    while True:
        line = modem.readline().decode('utf-8', errors='ignore')
        if any(status in line for status in ["OK", "ERROR", "CONNECT"]):
            logging.info(f"Modem Response: {line.strip()}")
            break

def killMgetty():
    subprocess.Popen(['sudo', 'killall', '-USR1', 'mgetty'])

def modemConnect(device):
    logging.info("Connecting to modem...")
    return serial.Serial(f"/dev/{device}", 460800, timeout=0)

def initModem(device):
    with modemConnect(device) as modem:
        send_command(modem, "ATZE1")  # Reset
        send_command(modem, "AT+FCLASS=8")  # Voice mode
        send_command(modem, "AT+VLS=1")  # Go online

        if "--enable-dial-tone" in sys.argv:
            print("Dial tone enabled, starting transmission...")
            send_command(modem, "AT+VTX=1")

        logging.info("Setup complete, listening...")
    return modem

def main():
    args = parse_args()
    graphic()

    device = args.device
    ring_timeout = args.ring_timeout

    modem = modemConnect(device)
    timeSinceDigit = None
    mode = "LISTENING"

    while True:
        if mode == "LISTENING":
            if timeSinceDigit:
                now = datetime.now()
                if (now - timeSinceDigit).total_seconds() > ring_timeout:
                    logging.info("Answering call...")
                    runMgetty(device)
                    time.sleep(4)
                    killMgetty()
                    logging.info("Call answered!")

                    for line in sh.tail("-f", "/var/log/syslog", "-n", "1", _iter=True):
                        logging.info(line.strip())
                        if "remote IP address" in line:
                            logging.info("Connected!")
                            mode = "CONNECTED"
                        if "Modem hangup" in line:
                            logging.info("Detected modem hang up, resetting...")
                            time.sleep(10)
                            timeSinceDigit = None
                            mode = "LISTENING"
                            modem.close()
                            modem = modemConnect(device)
                            break

            char = modem.read(1)
            if not char:
                continue

            if ord(char) == 16:  # DTMF signal detected
                try:
                    char = modem.read(1)
                    digit = int(char.decode('utf-8', errors='ignore'))
                    timeSinceDigit = datetime.now()
                    print(f"Received digit: {digit}")
                except (TypeError, ValueError):
                    pass

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    sys.exit(main())
