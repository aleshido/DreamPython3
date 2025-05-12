#!/bin/bash

# 1. Dependency Checks
if [ ! -f /var/lib/apt/periodic/update-success-stamp ]; then
    echo "Running apt-get update..."
    sudo apt-get update
fi

for pkg in ppp mgetty python3-sh; do
    if ! dpkg -s $pkg &> /dev/null; then
        echo "Installing missing package: $pkg"
        sudo apt-get install -y $pkg
    fi
done

# 2. PPP Configuration
sudo rm -f /etc/ppp/options
sudo touch /etc/ppp/options
sudo bash -c "cat > /etc/ppp/options" <<EOF
debug
login
require-pap
ms-dns 8.8.8.8
proxyarp
ktune
EOF

# 3. Network Configuration
echo "Detecting modem..."
MODEM_TTY=$(sudo dmesg | grep -oP 'tty(ACM|USB)[0-9]+' | tail -n1)
echo "Modem detected: $MODEM_TTY"

if [ -z "$MODEM_TTY" ]; then
    echo "Error: Modem not detected. Exiting."
    exit 1
fi

PC_IP=192.168.1.20
DC_IP=192.168.1.200
NETMASK=255.255.255.0

sudo bash -c "cat > /etc/ppp/options.$MODEM_TTY" <<EOF
$PC_IP:$DC_IP
netmask $NETMASK
EOF

# 4. PAP Secrets Configuration
if ! grep -q '^dreams \* dreamcast \*' /etc/ppp/pap-secrets; then
    sudo bash -c "echo 'dreams * dreamcast *' >> /etc/ppp/pap-secrets"
fi

# 5. mgetty Configuration
sudo rm -f /etc/mgetty/mgetty.config
sudo touch /etc/mgetty/mgetty.config
sudo bash -c "cat > /etc/mgetty/mgetty.config" <<EOF
debug 4
fax-id
speed 115200
port $MODEM_TTY
data-only y
issue-file /etc/issue.mgetty
EOF

# 6. User Creation
if ! id "dreams" &>/dev/null; then
    sudo useradd -G dialout,dip,users -c "Dreamcast user" -d /home/dreams -g users -s /usr/sbin/pppd dreams
    echo "dreams:dreamcast" | sudo chpasswd
fi

# 7. Start Python Script if available
if [ -f ./dreampi3.py ]; then
    echo "Starting dreampi3.py..."
    sudo python3 dreampi3.py
else
    echo "dreampi3.py not found."
fi