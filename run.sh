#!/bin/bash
cd /root/alpaca-bot
export PYTHONPATH="/usr/lib/python3/dist-packages:/usr/local/lib/python3.12/dist-packages"
/usr/bin/python3 bot.py "$@"
