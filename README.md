# LegoMac — LEGO Smista PRO MASTER

Applicazione Mac/Python che gestisce i set LEGO e comunica via LAN con **LEGO Vision** su iPhone.

Versione sorgente iniziale: **v11.5-MASTER-A4-REMOTE**.

## Funzioni principali

- gestione set, pezzi mancanti/completati e storico;
- server MASTER locale sulla porta 8765 con PIN generato localmente;
- pairing iPhone tramite QR;
- comandi remoti verso LEGO Vision;
- pannello **Calibrazione iPhone A4** per comandare la calibrazione dal Mac;
- riconoscimento e filtro dei pezzi mancanti;
- immagini e dati Rebrickable.

## Installazione

Richiede Python 3 con Tkinter.

```bash
python3 -m pip install -r requirements.txt
cp local_config.example.json local_config.json
```

Inserire la propria API key Rebrickable in `local_config.json`, quindi:

```bash
python3 legofinderv7.py
```

In alternativa si può impostare la variabile d'ambiente `REBRICKABLE_API_KEY`.

## Sicurezza / file locali

`local_config.json`, `master_config.json`, database LEGO, immagini, backup e cache sono esclusi da Git tramite `.gitignore`. Il PIN MASTER viene creato al primo avvio e rimane solo sul Mac.

## iPhone

La MASTER è progettata per lavorare con il branch iPhone `v13-vision-brickognize` del repository `ivangilli/Lego`.
