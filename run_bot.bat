@echo off
title Prasads PCX Ticket Watcher - Avengers Doomsday
color 0A

echo ======================================================================
echo    PRASADS MULTIPLEX PCX TICKET WATCHER (Avengers: Doomsday)
echo ======================================================================
echo  Monitors BookMyShow independently on your desktop.
echo  Handles internet drops automatically.
echo  Sends WhatsApp / SMS alerts directly to your phone.
echo ======================================================================
echo.

python "%~dp0pcx_ticket_bot.py"

pause
