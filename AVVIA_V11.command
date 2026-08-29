#!/bin/bash
cd "$(dirname "$0")" || exit 1
PYTHON_BIN="$HOME/.pyenv/versions/3.12.6/bin/python3"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3)"
fi

if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
    echo "ERRORE: Python 3 non è installato."
    read -r -p "Premi Invio per chiudere..."
    exit 1
fi

if ! "$PYTHON_BIN" -c "import tkinter"; then
    echo "ERRORE: questo Python non include Tkinter, necessario per la finestra dell'app."
    echo "Python usato: $PYTHON_BIN"
    read -r -p "Premi Invio per chiudere..."
    exit 1
fi

if ! "$PYTHON_BIN" -c "import PIL, requests, qrcode"; then
    echo "Installazione delle dipendenze mancanti..."
    if ! "$PYTHON_BIN" -m pip install -r requirements_v11.txt; then
        echo "ERRORE: installazione delle dipendenze non riuscita."
        read -r -p "Premi Invio per chiudere..."
        exit 1
    fi
fi

"$PYTHON_BIN" -u legofinderv7.py
status=$?
if [ "$status" -ne 0 ]; then
    echo ""
    echo "L'app si è chiusa con errore (codice $status)."
    read -r -p "Premi Invio per chiudere..."
fi
exit "$status"
