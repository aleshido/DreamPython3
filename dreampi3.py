#!/usr/bin/env python3

import serial
import os
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

MODEM_DEVICE = "ttyACM0" 

def runMgetty():
    subprocess.Popen(['sudo', '/sbin/mgetty', '-s', '115200', '-D', '/dev/'+MODEM_DEVICE],
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
                    runMgetty()
                    time.sleep(4)
                    killMgetty()
                    logging.info("Call answered!")
                    for line in sh.tail("-f", "/var/log/syslog", "-n", "1", _iter=True):
                        logging.info("line")
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