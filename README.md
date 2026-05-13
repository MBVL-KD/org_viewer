# Damclubs Scraper en Viewer

## Installatie

1. Ga naar de map:
   ```bash
   cd /Users/maartenvanleenen/Desktop/Draughts4All/Clubs
   ```
2. Installeer dependencies:
   ```bash
   python3 -m pip install -r requirements.txt
   ```

## Data importeren

Start de scraper:

```bash
python3 scraper.py
```

De scraper leest `MONGO_URI` automatisch uit je `.env` en slaat de clubs op in MongoDB.

## Viewer starten

```bash
streamlit run viewer.py
```

De viewer toont het cluboverzicht, een kaart van gecodeerde clubs en een formulier om gegevens bij te werken.
