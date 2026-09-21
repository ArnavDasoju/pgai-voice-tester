#!/bin/bash
source venv/bin/activate
python agent.py dev &
sleep 10
python make_call.py
wait
