#!/bin/bash

# ---------------------------------------------------------------------------
# 0. Platform detection - phase A (package tooling)
#
# Probe for commands and files rather than parsing /etc/os-release: derivatives
# report their own ID (Nobara reports ID=nobara, not fedora), so an ID check
# would fail on the very systems we want to support.
# ---------------------------------------------------------------------------
if command -v apt-get >/dev/null 2>&1; then
    PKG_CHECK="dpkg -s"
    PKG_INSTALL="apt-get install -y"
    PKG_UPDATE="apt-get update"
elif command -v dnf >/dev/null 2>&1; then
    PKG_CHECK="rpm -q"
    PKG_INSTALL="dnf install -y"
    PKG_UPDATE="dnf makecache"
else
    echo "Error: no supported package manager found (apt-get or dnf). Exiting."
    exit 1
fi

# 1. Dependency Checks
# The apt timestamp is Debian-only; on other distros just refresh metadata.
if [ "$PKG_CHECK" = "dpkg -s" ] && [ -f /var/lib/apt/periodic/update-success-stamp ]; then
    : # package lists were refreshed recently
else
    echo "Refreshing package metadata..."
    sudo $PKG_UPDATE
fi

# Package names happen to be identical on Debian and Fedora family distros.
for pkg in ppp mgetty; do
    if ! $PKG_CHECK "$pkg" &> /dev/null; then
        echo "Installing missing package: $pkg"
        sudo $PKG_INSTALL "$pkg"
    fi
done

# ---------------------------------------------------------------------------
# 1b. Platform detection - phase B (paths)
#
# Must run AFTER installation: mgetty's config directory only exists once the
# package is on disk, so probing earlier would always miss it.
# ---------------------------------------------------------------------------
for d in /etc/mgetty+sendfax /etc/mgetty; do
    if [ -d "$d" ]; then
        MGETTY_CONF_DIR="$d"
        break
    fi
done
MGETTY_CONF_DIR="${MGETTY_CONF_DIR:-/etc/mgetty}"
sudo mkdir -p "$MGETTY_CONF_DIR"

PPPD_BIN="$(command -v pppd 2>/dev/null)"
if [ -z "$PPPD_BIN" ]; then
    for p in /usr/sbin/pppd /sbin/pppd /usr/bin/pppd; do
        if [ -x "$p" ]; then
            PPPD_BIN="$p"
            break
        fi
    done
fi

if [ -z "$PPPD_BIN" ]; then
    echo "Error: pppd not found after install. Exiting."
    exit 1
fi

echo "Using mgetty config dir: $MGETTY_CONF_DIR"
echo "Using pppd:              $PPPD_BIN"

# 2. PPP Configuration
# 'lock' is shipped by the distro default and is worth keeping.
sudo rm -f /etc/ppp/options
sudo touch /etc/ppp/options
sudo bash -c "cat > /etc/ppp/options" <<EOF
lock
debug
require-pap
ms-dns 8.8.8.8
proxyarp
ktune
EOF

# 3. Network Configuration
# Probe the device nodes that actually exist. Grepping dmesg history picks the
# most recently *mentioned* tty, which may be a device that has since vanished.
echo "Detecting modem..."
MODEM_TTY=""
for d in /dev/ttyACM* /dev/ttyUSB*; do
    if [ -e "$d" ]; then
        MODEM_TTY="$(basename "$d")"
        break
    fi
done

if [ -z "$MODEM_TTY" ]; then
    echo "Error: Modem not detected. Exiting."
    exit 1
fi
echo "Modem detected: $MODEM_TTY"

PC_IP=192.168.1.20
DC_IP=192.168.1.200
NETMASK=255.255.255.0

sudo bash -c "cat > /etc/ppp/options.$MODEM_TTY" <<EOF
$PC_IP:$DC_IP
netmask $NETMASK
EOF

# 4. PAP Secrets Configuration
# Accept whatever credentials the Dreamcast sends. The console always supplies a
# username and password, but which ones is not reliably under the user's control
# (consoles ship with defaults such as profile/eliforp, and the sign-up flow uses
# a different name again). A '*' client matches any name and a "" secret matches
# any password, so no per-user setup is needed.
#
# Note: do not add a more specific line alongside this one. pppd picks the match
# with the fewest wildcards, so an exact entry would shadow this and become the
# only username that can be rejected.
if ! sudo grep -qE '^\*[[:space:]]+\*' /etc/ppp/pap-secrets; then
    sudo bash -c "printf '%s\t%s\t%s\t%s\n' '*' '*' '\"\"' '*' >> /etc/ppp/pap-secrets"
fi

# 5. mgetty Configuration
sudo rm -f "$MGETTY_CONF_DIR/mgetty.config"
sudo touch "$MGETTY_CONF_DIR/mgetty.config"
sudo bash -c "cat > $MGETTY_CONF_DIR/mgetty.config" <<EOF
debug 4
fax-id
speed 115200
port $MODEM_TTY
data-only y
issue-file /etc/issue.mgetty
EOF

# 5b. mgetty AutoPPP Configuration
# When mgetty sees an LCP configure request it looks up the magic '/AutoPPP/'
# user in login.config. Distros ship that entry commented out, so the call
# falls through to the '*' catch-all and lands in /bin/login, which cannot read
# PPP frames - the Dreamcast connects at carrier level and then stalls.
LOGIN_CONF="$MGETTY_CONF_DIR/login.config"
AUTOPPP_LINE="/AutoPPP/ - a_ppp $PPPD_BIN auth -chap +pap debug"
if [ -f "$LOGIN_CONF" ]; then
    if ! sudo grep -qE '^[[:space:]]*/AutoPPP/' "$LOGIN_CONF"; then
        echo "Enabling AutoPPP in $LOGIN_CONF"
        # mgetty uses the FIRST matching rule, so this must be inserted above
        # the '*' catch-all - appending it would leave it unreachable.
        if sudo grep -qE '^\*' "$LOGIN_CONF"; then
            sudo sed -i "0,/^\*/s|^\*|$AUTOPPP_LINE\n*|" "$LOGIN_CONF"
        else
            sudo bash -c "printf '%s\n' '$AUTOPPP_LINE' >> '$LOGIN_CONF'"
        fi
    fi
else
    echo "Warning: $LOGIN_CONF not found, skipping AutoPPP setup."
fi

# 6. Start Python Script if available
if [ -f ./dreampi3.py ]; then
    echo "Starting dreampi3.py..."
    sudo python3 dreampi3.py
else
    echo "dreampi3.py not found."
fi
