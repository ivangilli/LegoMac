# LegoMac — LEGO Smista PRO MASTER

Applicazione Mac/Python che gestisce i set LEGO e comunica via LAN con **LEGO Vision** su iPhone.

Versione corrente: **v11.7-MASTER-iPHONE-B17**.

## Funzioni principali

- gestione set, pezzi mancanti/completati e storico;
- server MASTER locale sulla porta 8765 con PIN generato localmente;
- pairing iPhone tramite QR;
- comandi remoti verso LEGO Vision;
- pannello **Calibrazione iPhone A4** compatibile con LEGO Vision v13.1 build 17;
- zoom automatico 1×–3× quando il foglio A4 è troppo lontano;
- comandi `a4_start`, `a4_plane`, `a4_reference`, `a4_status`, `a4_close`;
- lettura dello stato calibrazione restituito dall’iPhone;
- riconoscimento e filtro dei pezzi mancanti;
- immagini e dati Rebrickable.

## Primo avvio dopo il clone

Dopo aver clonato il repository esegui una volta:

```bash
python3 restore_source.py
```

Il sorgente completo è già tracciato direttamente da Git. Il comando verifica la presenza del supporto **v11.7 / iPhone build 17** e controlla sintatticamente sia l’app sia il server MASTER. Non usa bundle Base64.

Poi installa le dipendenze:

```bash
python3 -m pip install -r requirements.txt
cp local_config.example.json local_config.json
```

Inserisci la tua API key Rebrickable in `local_config.json`, quindi avvia:

```bash
python3 legofinderv7.py
```

In alternativa la API key può essere fornita tramite variabile d'ambiente `REBRICKABLE_API_KEY`.

## Calibrazione iPhone build 17 dal Mac

Nel pannello **Calibrazione iPhone A4** puoi:

1. aprire la guida A4 sull’iPhone;
2. calibrare il piano vuoto;
3. avviare la verifica della Plate rossa 2×4;
4. leggere lo stato corrente della calibrazione dall’iPhone;
5. chiudere la guida e tornare al riconoscimento.

La MASTER mostra anche le risposte restituite da LEGO Vision build 17 e mantiene lo stato A4 ricevuto.

## Sicurezza / file locali

`local_config.json`, `master_config.json`, database LEGO, immagini, backup e cache sono esclusi da Git tramite `.gitignore`. Il PIN MASTER viene creato al primo avvio e rimane solo sul Mac. Nessuna API key personale viene pubblicata nel repository.

## iPhone

La MASTER è progettata per lavorare con **LEGO Vision v13.1 build 17** sul branch `v13-vision-brickognize` del repository `ivangilli/Lego`.
