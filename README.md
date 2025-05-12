# DreamPython3

Script that automates the modem configuration and makes the listener compatible with Python 3. Information about the initial version of the script, made by Petri Trebilcock, can be found at http://www.dreamcast-talk.com/forum/viewtopic.php?f=3&t=7598.

## Important Note
This script is designed to run on modern Ubuntu OS systems as an alternative to DreamPi, which currently has compatibility issues with Raspberry Pi 5. This is an experimental solution that has worked in my specific use case. Please use at your own discretion, as consistent support cannot be guaranteed.

## Technical Specifications
- Modem: Tested only with USB modem on ttyACM0
- DNS: Uses Google DNS (8.8.8.8)
- Network: Automatically detects IP (tested only on 192.168.1.x networks)
- Authentication:
  - Username: dreams
  - Password: dreamcast
  - These credentials should be entered in your Dreamcast network settings

## Credits
- Original script by Petri Trebilcock
- Python 3 port and improved logging in collaboration with [Paula Fleck](https://github.com/paulakfleck)


## Ubuntu Security Note
Due to Ubuntu security constraints, you might need to manually create the 'dreams' user and set its password using the following commands:
```bash
sudo useradd -G dialout,dip,users -c "Dreamcast user" -d /home/dreams -g users -s /usr/sbin/pppd dreams
sudo passwd dreams
```
Set the password to: dreamcast

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
   ./DreamPi.sh
   ```
