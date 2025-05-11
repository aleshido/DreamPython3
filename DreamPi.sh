#!/bin/bash
sudo apt-get install -y mgetty ppp python3-pip


sudo rm /etc/ppp/options
sudo touch /etc/ppp/options

sudo bash -c "cat > /etc/ppp/options" <<EOF
debug
login
require-pap
ms-dns 8.8.8.8
proxyarp
ktune
EOF

echo "Detectando modem..."
MODEM_TTY=$(sudo dmesg | grep -oP 'tty(ACM|USB)[0-9]+' | tail -n1)
echo "Modem detectado: $MODEM_TTY"

if [ -z "$MODEM_TTY" ]; then
    echo "Erro: Modem não detectado. Saindo do script."
    exit 1
fi

DC_IP=$(hostname -I | awk '{print $1}')
NETMASK=255.255.255.0

sudo bash -c "cat > /etc/ppp/options.$MODEM_TTY" <<EOF
$DC_IP:$DC_IP
netmask $NETMASK
EOF

if ! grep -q '^dreams \* dreamcast \*' /etc/ppp/pap-secrets; then
    sudo bash -c "echo 'dreams * dreamcast *' >> /etc/ppp/pap-secrets"
fi
if ! grep -q '^900418F2C6AC@uk\.dkey \* jamie \*' /etc/ppp/pap-secrets; then
    sudo bash -c "echo '900418F2C6AC@uk.dkey * jamie *' >> /etc/ppp/pap-secrets"
fi

sudo rm /etc/mgetty/mgetty.config
sudo touch /etc/mgetty/mgetty.config
sudo bash -c "cat > /etc/mgetty/mgetty.config" <<EOF
debug 4
fax-id
speed 115200
port $MODEM_TTY
data-only y
issue-file /etc/issue.mgetty
EOF

if [ -f ./dreampi3.py ]; then
    echo "Iniciando dreampi3.py..."
    chmod +x ./dreampi3.py
    if ! command -v python3 &> /dev/null; then
        echo "python3 não encontrado. Instalando..."
        sudo apt-get install -y python3
    fi
    if ! dpkg -s python3-sh &> /dev/null; then
        echo "python3-sh não encontrado. Instalando..."
        sudo apt-get install -y python3-sh
    fi
    sudo python3 ./dreampi3.py --device $MODEM_TTY &
else
    echo "Script dreampi3.py não encontrado."
fi

# sudo useradd -G dialout,dip,users -c "Dreamcast user" -d /home/dreams -g users -s /usr/sbin/pppd dreams
# sudo passwd dreams
# sudo passwd 900418F2C6AC@uk.dkey
